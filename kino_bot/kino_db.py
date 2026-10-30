"""
Kino bot uchun qo'shimcha baza funksiyalari (Database sinfiga mixin sifatida ulanadi).

  * yangi ustunlar: kino turi, rejissyor, aktyorlar, davomiylik, yosh chegarasi, IMDb, treyler, sifat, TMDB ID
  * yangi jadvallar: versiyalar (til/sifat), kino so'rovlari, muammo xabarlari, to'plamlar, VIP eslatmalari
  * VIP voronka: kunlik bepul limit, bepul sinov, qayta xarid chegirmasi, tugash eslatmalari
  * aqlli qidiruv: xato yozilgan nomni ham topadi, aktyor/rejissyor bo'yicha qidiradi
"""
from __future__ import annotations

import datetime
import difflib
import re
from typing import Any, Optional

import aiosqlite

import config

KINO_COLUMNS = {
    "kind": "TEXT NOT NULL DEFAULT 'movie'",   # 'movie' | 'series'
    "director": "TEXT",
    "actors": "TEXT",
    "duration": "TEXT",                         # masalan "2 soat 15 daqiqa"
    "age_rating": "TEXT",                       # 6+ / 12+ / 16+ / 18+
    "imdb_rating": "TEXT",                      # masalan "8.1"
    "trailer_url": "TEXT",
    "quality": "TEXT",                          # 480p / 720p / 1080p
    "tmdb_id": "INTEGER",
}

KINO_SCHEMA = """
CREATE TABLE IF NOT EXISTS movie_versions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    anime_id         INTEGER NOT NULL REFERENCES anime(id) ON DELETE CASCADE,
    label            TEXT NOT NULL,          -- tugmada ko'rinadigan nom: "🇺🇿 O'zbekcha · 1080p"
    video_file_id    TEXT NOT NULL,
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_versions_anime ON movie_versions(anime_id);

CREATE TABLE IF NOT EXISTS movie_requests (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id  INTEGER NOT NULL,
    text         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'new',   -- new | done | rejected
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS movie_reports (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id  INTEGER NOT NULL,
    anime_id     INTEGER,
    text         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'new',   -- new | fixed
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS collections (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    emoji       TEXT NOT NULL DEFAULT '🎞',
    title       TEXT NOT NULL UNIQUE,
    sort_order  INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS collection_items (
    collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    anime_id      INTEGER NOT NULL REFERENCES anime(id) ON DELETE CASCADE,
    PRIMARY KEY (collection_id, anime_id)
);

CREATE TABLE IF NOT EXISTS vip_reminders (
    telegram_id INTEGER NOT NULL,
    vip_until   TEXT NOT NULL,
    kind        TEXT NOT NULL,                  -- '3d' | '1d'
    sent_at     TEXT NOT NULL,
    PRIMARY KEY (telegram_id, vip_until, kind)
);
"""


def _now() -> str:
    return datetime.datetime.utcnow().isoformat(timespec="seconds")


def _local_midnight_utc_iso() -> str:
    now_local = datetime.datetime.now(config.LOCAL_TZ)
    start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = start_local.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return start_utc.isoformat(timespec="seconds")


def _norm(text: str) -> str:
    """Qidiruv uchun nomni soddalashtiradi: kichik harf, apostrof/tinish belgilarsiz."""
    text = (text or "").lower()
    text = re.sub(r"[ʻʼ`´‘’']", "", text)
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


class KinoDBMixin:
    conn: Optional[aiosqlite.Connection]

    # ------------------------------------------------------------------ #
    # Migratsiya
    # ------------------------------------------------------------------ #
    async def _migrate_kino(self) -> None:
        cur = await self.conn.execute("PRAGMA table_info(anime)")
        cols = {row[1] for row in await cur.fetchall()}
        for name, ddl in KINO_COLUMNS.items():
            if name not in cols:
                await self.conn.execute(f"ALTER TABLE anime ADD COLUMN {name} {ddl}")

        cur = await self.conn.execute("PRAGMA table_info(users)")
        ucols = {row[1] for row in await cur.fetchall()}
        if "trial_used" not in ucols:
            await self.conn.execute("ALTER TABLE users ADD COLUMN trial_used INTEGER NOT NULL DEFAULT 0")
            # Eski foydalanuvchilar sinovni qayta olmasin — faqat yangi kelganlar oladi
            await self.conn.execute("UPDATE users SET trial_used=1")
        await self.conn.executescript(KINO_SCHEMA)
        await self.conn.commit()

    # ------------------------------------------------------------------ #
    # 💎 VIP voronka
    # ------------------------------------------------------------------ #
    async def count_free_views_today(self, telegram_id: int) -> int:
        """Bugun nechta TURLI kino ochilgan (bir kinoni qayta ko'rish limitga qo'shilmaydi)."""
        cur = await self.conn.execute(
            "SELECT COUNT(DISTINCT h.anime_id) FROM watch_history h JOIN users u ON u.id=h.user_id "
            "WHERE u.telegram_id=? AND h.watched_at >= ?",
            (telegram_id, _local_midnight_utc_iso()),
        )
        (n,) = await cur.fetchone()
        return n

    async def has_viewed_today(self, telegram_id: int, anime_id: int) -> bool:
        cur = await self.conn.execute(
            "SELECT 1 FROM watch_history h JOIN users u ON u.id=h.user_id "
            "WHERE u.telegram_id=? AND h.anime_id=? AND h.watched_at >= ? LIMIT 1",
            (telegram_id, anime_id, _local_midnight_utc_iso()),
        )
        return (await cur.fetchone()) is not None

    async def grant_trial_if_new(self, telegram_id: int, days: int) -> Optional[datetime.datetime]:
        """Yangi foydalanuvchiga bir martalik bepul VIP sinov. Allaqachon olgan bo'lsa None."""
        if days <= 0:
            return None
        cur = await self.conn.execute(
            "UPDATE users SET trial_used=1 WHERE telegram_id=? AND trial_used=0", (telegram_id,)
        )
        await self.conn.commit()
        if cur.rowcount != 1:
            return None
        return await self.extend_vip_days(telegram_id, days)

    async def has_paid_before(self, telegram_id: int) -> bool:
        """Foydalanuvchi ilgari VIP xarid qilganmi (to'lov yoki admin bergan) — qayta xarid chegirmasi uchun."""
        cur = await self.conn.execute(
            "SELECT 1 FROM vip_purchases p JOIN users u ON u.id=p.user_id WHERE u.telegram_id=? LIMIT 1",
            (telegram_id,),
        )
        return (await cur.fetchone()) is not None

    async def due_vip_reminders(self) -> list[tuple[int, str, str]]:
        """
        Eslatma yuborilishi kerak bo'lgan VIP foydalanuvchilar: [(telegram_id, vip_until, kind)].
        kind '3d' — 3 kundan kam, 1 kundan ko'p qolgan; '1d' — 1 kundan kam qolgan.
        Faqat pul to'lab/admin orqali VIP olganlarga (bepul sinov va referal VIP'ga yuborilmaydi).
        """
        now = datetime.datetime.utcnow()
        iso = lambda dt: dt.isoformat(timespec="seconds")  # noqa: E731
        out: list[tuple[int, str, str]] = []
        for kind, lo, hi in (
            ("1d", now, now + datetime.timedelta(days=1)),
            ("3d", now + datetime.timedelta(days=1), now + datetime.timedelta(days=3)),
        ):
            cur = await self.conn.execute(
                "SELECT u.telegram_id, u.vip_until FROM users u "
                "WHERE u.vip_until IS NOT NULL AND u.vip_until > ? AND u.vip_until <= ? "
                "AND EXISTS (SELECT 1 FROM vip_purchases p WHERE p.user_id=u.id) "
                "AND NOT EXISTS (SELECT 1 FROM vip_reminders r WHERE r.telegram_id=u.telegram_id "
                "AND r.vip_until=u.vip_until AND r.kind IN (?, '1d'))",
                (iso(lo), iso(hi), kind),
            )
            for r in await cur.fetchall():
                out.append((r["telegram_id"], r["vip_until"], kind))
        return out

    async def mark_vip_reminder(self, telegram_id: int, vip_until: str, kind: str) -> None:
        await self.conn.execute(
            "INSERT OR IGNORE INTO vip_reminders (telegram_id, vip_until, kind, sent_at) VALUES (?,?,?,?)",
            (telegram_id, vip_until, kind, _now()),
        )
        await self.conn.commit()

    async def vip_sales_stats(self) -> dict[str, Any]:
        """Daromad: bugun / 7 kun / 30 kun / jami (faqat 'payment' manbali xaridlar)."""
        today = _local_midnight_utc_iso()
        week = (datetime.datetime.utcnow() - datetime.timedelta(days=7)).isoformat(timespec="seconds")
        month = (datetime.datetime.utcnow() - datetime.timedelta(days=30)).isoformat(timespec="seconds")

        async def one(since: Optional[str]) -> tuple[int, int]:
            sql = "SELECT COUNT(*), COALESCE(SUM(price),0) FROM vip_purchases WHERE source='payment'"
            args: tuple = ()
            if since:
                sql += " AND purchased_at >= ?"
                args = (since,)
            cur = await self.conn.execute(sql, args)
            n, total = await cur.fetchone()
            return n, total

        return {"today": await one(today), "week": await one(week), "month": await one(month), "all": await one(None)}

    # ------------------------------------------------------------------ #
    # Versiyalar (til / sifat bo'yicha alohida video)
    # ------------------------------------------------------------------ #
    async def add_version(self, anime_id: int, label: str, video_file_id: str) -> int:
        cur = await self.conn.execute(
            "INSERT INTO movie_versions (anime_id, label, video_file_id, created_at) VALUES (?,?,?,?)",
            (anime_id, label, video_file_id, _now()),
        )
        await self.conn.commit()
        return cur.lastrowid

    async def list_versions(self, anime_id: int) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            "SELECT * FROM movie_versions WHERE anime_id=? ORDER BY id", (anime_id,)
        )
        return await cur.fetchall()

    async def get_version(self, version_id: int) -> Optional[aiosqlite.Row]:
        cur = await self.conn.execute("SELECT * FROM movie_versions WHERE id=?", (version_id,))
        return await cur.fetchone()

    async def delete_version(self, version_id: int) -> None:
        await self.conn.execute("DELETE FROM movie_versions WHERE id=?", (version_id,))
        await self.conn.commit()

    # ------------------------------------------------------------------ #
    # Kino so'rovlari va muammo xabarlari
    # ------------------------------------------------------------------ #
    async def create_movie_request(self, telegram_id: int, text: str) -> int:
        cur = await self.conn.execute(
            "INSERT INTO movie_requests (telegram_id, text, created_at) VALUES (?,?,?)",
            (telegram_id, text[:500], _now()),
        )
        await self.conn.commit()
        return cur.lastrowid

    async def count_requests_today(self, telegram_id: int) -> int:
        cur = await self.conn.execute(
            "SELECT COUNT(*) FROM movie_requests WHERE telegram_id=? AND created_at >= ?",
            (telegram_id, _local_midnight_utc_iso()),
        )
        (n,) = await cur.fetchone()
        return n

    async def list_movie_requests(self, status: str = "new", limit: int = 20) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            "SELECT * FROM movie_requests WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit)
        )
        return await cur.fetchall()

    async def get_movie_request(self, req_id: int) -> Optional[aiosqlite.Row]:
        cur = await self.conn.execute("SELECT * FROM movie_requests WHERE id=?", (req_id,))
        return await cur.fetchone()

    async def set_request_status(self, req_id: int, status: str) -> None:
        await self.conn.execute("UPDATE movie_requests SET status=? WHERE id=?", (status, req_id))
        await self.conn.commit()

    async def count_new_requests(self) -> int:
        cur = await self.conn.execute("SELECT COUNT(*) FROM movie_requests WHERE status='new'")
        (n,) = await cur.fetchone()
        return n

    async def create_report(self, telegram_id: int, anime_id: Optional[int], text: str) -> int:
        cur = await self.conn.execute(
            "INSERT INTO movie_reports (telegram_id, anime_id, text, created_at) VALUES (?,?,?,?)",
            (telegram_id, anime_id, text[:500], _now()),
        )
        await self.conn.commit()
        return cur.lastrowid

    async def list_reports(self, status: str = "new", limit: int = 20) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            "SELECT r.*, a.title AS anime_title, a.anime_code AS anime_code FROM movie_reports r "
            "LEFT JOIN anime a ON a.id=r.anime_id WHERE r.status=? ORDER BY r.id DESC LIMIT ?",
            (status, limit),
        )
        return await cur.fetchall()

    async def get_report(self, report_id: int) -> Optional[aiosqlite.Row]:
        cur = await self.conn.execute("SELECT * FROM movie_reports WHERE id=?", (report_id,))
        return await cur.fetchone()

    async def set_report_status(self, report_id: int, status: str) -> None:
        await self.conn.execute("UPDATE movie_reports SET status=? WHERE id=?", (status, report_id))
        await self.conn.commit()

    async def count_new_reports(self) -> int:
        cur = await self.conn.execute("SELECT COUNT(*) FROM movie_reports WHERE status='new'")
        (n,) = await cur.fetchone()
        return n

    # ------------------------------------------------------------------ #
    # To'plamlar (Oskar g'oliblari, Marvel, Oilaviy kinolar ...)
    # ------------------------------------------------------------------ #
    async def list_collections(self) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            "SELECT c.*, (SELECT COUNT(*) FROM collection_items i JOIN anime a ON a.id=i.anime_id "
            "WHERE i.collection_id=c.id AND a.is_published=1) AS item_count "
            "FROM collections c ORDER BY c.sort_order, c.id"
        )
        return await cur.fetchall()

    async def get_collection(self, collection_id: int) -> Optional[aiosqlite.Row]:
        cur = await self.conn.execute("SELECT * FROM collections WHERE id=?", (collection_id,))
        return await cur.fetchone()

    async def create_collection(self, title: str, emoji: str = "🎞") -> Optional[int]:
        try:
            cur = await self.conn.execute(
                "INSERT INTO collections (emoji, title, sort_order) "
                "VALUES (?, ?, (SELECT COALESCE(MAX(sort_order),0)+1 FROM collections))",
                (emoji, title.strip()),
            )
            await self.conn.commit()
            return cur.lastrowid
        except aiosqlite.IntegrityError:
            return None

    async def delete_collection(self, collection_id: int) -> None:
        await self.conn.execute("DELETE FROM collections WHERE id=?", (collection_id,))
        await self.conn.commit()

    async def collection_items(self, collection_id: int, page: int = 0, per_page: int = 10) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            "SELECT a.* FROM collection_items i JOIN anime a ON a.id=i.anime_id "
            "WHERE i.collection_id=? AND a.is_published=1 ORDER BY a.created_at DESC LIMIT ? OFFSET ?",
            (collection_id, per_page, page * per_page),
        )
        return await cur.fetchall()

    async def count_collection_items(self, collection_id: int) -> int:
        cur = await self.conn.execute(
            "SELECT COUNT(*) FROM collection_items i JOIN anime a ON a.id=i.anime_id "
            "WHERE i.collection_id=? AND a.is_published=1",
            (collection_id,),
        )
        (n,) = await cur.fetchone()
        return n

    async def anime_collection_ids(self, anime_id: int) -> set[int]:
        cur = await self.conn.execute(
            "SELECT collection_id FROM collection_items WHERE anime_id=?", (anime_id,)
        )
        return {r["collection_id"] for r in await cur.fetchall()}

    async def toggle_collection_item(self, collection_id: int, anime_id: int) -> bool:
        """Qo'shilgan bo'lsa olib tashlaydi (False), aks holda qo'shadi (True)."""
        cur = await self.conn.execute(
            "SELECT 1 FROM collection_items WHERE collection_id=? AND anime_id=?", (collection_id, anime_id)
        )
        if await cur.fetchone():
            await self.conn.execute(
                "DELETE FROM collection_items WHERE collection_id=? AND anime_id=?", (collection_id, anime_id)
            )
            await self.conn.commit()
            return False
        await self.conn.execute(
            "INSERT OR IGNORE INTO collection_items (collection_id, anime_id) VALUES (?,?)",
            (collection_id, anime_id),
        )
        await self.conn.commit()
        return True

    # ------------------------------------------------------------------ #
    # Premyeralar / aqlli qidiruv
    # ------------------------------------------------------------------ #
    async def list_premieres(self, limit: int = 30) -> list[aiosqlite.Row]:
        """Oxirgi 30 kunda qo'shilgan kinolar."""
        since = (datetime.datetime.utcnow() - datetime.timedelta(days=30)).isoformat(timespec="seconds")
        cur = await self.conn.execute(
            "SELECT * FROM anime WHERE is_published=1 AND created_at >= ? ORDER BY created_at DESC LIMIT ?",
            (since, limit),
        )
        return await cur.fetchall()

    async def all_admin_ids(self) -> list[int]:
        cur = await self.conn.execute("SELECT telegram_id FROM users WHERE is_admin=1")
        return [r["telegram_id"] for r in await cur.fetchall()]

    async def top_viewed(self, limit: int = 10, days: int = 30) -> list[aiosqlite.Row]:
        """Oxirgi N kunda eng ko'p ko'rilgan kinolar (tomosha soni bilan)."""
        since = (datetime.datetime.utcnow() - datetime.timedelta(days=days)).isoformat(timespec="seconds")
        cur = await self.conn.execute(
            "SELECT a.*, COUNT(*) AS view_count FROM watch_history h JOIN anime a ON a.id=h.anime_id "
            "WHERE a.is_published=1 AND h.watched_at >= ? GROUP BY a.id "
            "ORDER BY view_count DESC, a.created_at DESC LIMIT ?",
            (since, limit),
        )
        return await cur.fetchall()

    async def smart_search(self, query: str, limit: int = 15) -> list[aiosqlite.Row]:
        """
        Nom, rejissyor va aktyorlar bo'yicha qidiradi. Avval oddiy moslik (katta-kichik harf va
        apostrofga e'tiborsiz), topilmasa — o'xshash nomlar (xato yozilgan bo'lsa ham).
        """
        q = _norm(query)
        if not q:
            return []
        cur = await self.conn.execute("SELECT * FROM anime WHERE is_published=1 ORDER BY title")
        rows = await cur.fetchall()
        exact: list[aiosqlite.Row] = []
        by_people: list[aiosqlite.Row] = []
        for r in rows:
            title = _norm(r["title"])
            if q in title:
                exact.append(r)
                continue
            people = _norm(f"{r['director'] or ''} {r['actors'] or ''}")
            if people and q in people:
                by_people.append(r)
        result = exact + by_people
        if result:
            return result[:limit]
        # Xato yozilgan nom: eng o'xshash nomlar
        scored = []
        q_words = len(q.split())
        for r in rows:
            title = _norm(r["title"])
            words = title.split()
            # butun nom, alohida so'zlar va so'rov uzunligidagi so'z oynalari bilan solishtiramiz
            parts = [title] + words
            if q_words > 1:
                parts += [" ".join(words[i:i + q_words]) for i in range(0, max(1, len(words) - q_words + 1))]
            ratio = max(difflib.SequenceMatcher(None, q, p).ratio() for p in parts)
            if ratio >= 0.72:
                scored.append((ratio, r))
        scored.sort(key=lambda x: -x[0])
        return [r for _, r in scored[:limit]]
