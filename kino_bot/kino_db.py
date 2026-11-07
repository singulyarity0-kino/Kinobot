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
    "open_to_all": "INTEGER NOT NULL DEFAULT 0",   # 1 = erta kirish kechikishi qo'llanmaydi (hammaga darhol ochiq)
    "released_at": "TEXT",                          # e'lon qilingan vaqt (erta kirish hisobi shundan boshlanadi)
    "ai_hook": "TEXT",                              # Gemini yozgan qisqa jozibali jumla (kanal posti uchun)
    "notified_vip": "INTEGER NOT NULL DEFAULT 0",   # janr obunachilari (VIP) xabardor qilinganmi
    "notified_free": "INTEGER NOT NULL DEFAULT 0",  # janr obunachilari (oddiy) xabardor qilinganmi
}

KINO_SCHEMA = """
CREATE TABLE IF NOT EXISTS movie_versions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    anime_id         INTEGER NOT NULL REFERENCES anime(id) ON DELETE CASCADE,
    label            TEXT NOT NULL,          -- tugmada ko'rinadigan nom: "🇺🇿 O'zbekcha · 1080p"
    video_file_id    TEXT NOT NULL,
    quality          INTEGER,                -- 360 / 480 / 720 / 1080 / 2160 (video o'lchamidan aniqlanadi)
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

CREATE TABLE IF NOT EXISTS view_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL,
    anime_id    INTEGER NOT NULL,
    version_id  INTEGER,
    quality     INTEGER,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_view_events_anime ON view_events(anime_id);
CREATE INDEX IF NOT EXISTS idx_view_events_created ON view_events(created_at);

CREATE TABLE IF NOT EXISTS announce_channels (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id          TEXT NOT NULL UNIQUE,
    title            TEXT NOT NULL,
    enabled          INTEGER NOT NULL DEFAULT 1,      -- yangi kino e'lonlari shu kanalga boradimi
    daily_enabled    INTEGER NOT NULL DEFAULT 1,      -- kunlik random kino e'loni
    daily_time       TEXT NOT NULL DEFAULT '12:00',   -- O'zbekiston vaqti
    last_daily_date  TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS announcements (
    anime_id    INTEGER NOT NULL REFERENCES anime(id) ON DELETE CASCADE,
    chat_id     TEXT NOT NULL,
    message_id  INTEGER NOT NULL,
    has_photo   INTEGER NOT NULL DEFAULT 0,
    posted_at   TEXT NOT NULL,
    PRIMARY KEY (anime_id, chat_id)
);
CREATE TABLE IF NOT EXISTS channel_daily_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    TEXT NOT NULL,
    anime_id   INTEGER NOT NULL,
    posted_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_daily_hist ON channel_daily_history(chat_id, posted_at);
CREATE TABLE IF NOT EXISTS scheduled_posts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    anime_id    INTEGER NOT NULL REFERENCES anime(id) ON DELETE CASCADE,
    run_at      TEXT NOT NULL,                    -- UTC
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending | done | cancelled
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS genre_subs (
    telegram_id INTEGER NOT NULL,
    genre_id    INTEGER NOT NULL REFERENCES genres(id) ON DELETE CASCADE,
    PRIMARY KEY (telegram_id, genre_id)
);
CREATE TABLE IF NOT EXISTS vip_triggers (
    telegram_id INTEGER PRIMARY KEY,
    anime_id    INTEGER,
    reason      TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS auto_payments (
    charge_id    TEXT PRIMARY KEY,          -- Telegram to'lov ID'si: bir to'lov ikki marta hisoblanmasin
    telegram_id  INTEGER NOT NULL,
    months       INTEGER NOT NULL,
    amount       INTEGER NOT NULL,          -- eng kichik birlikda (UZS: tiyin, XTR: yulduz)
    currency     TEXT NOT NULL,
    method       TEXT NOT NULL,             -- card | stars
    created_at   TEXT NOT NULL
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


def detect_quality(width: Optional[int], height: Optional[int]) -> int:
    """
    Video o'lchamidan sifatni aniqlaydi. Uzun tomon bo'yicha (kinematografik 1920x800 ham 1080p chiqadi):
    ≥3200→2160, ≥1700→1080, ≥1150→720, ≥700→480, aks holda 360.
    """
    long_side = max(width or 0, height or 0)
    if long_side >= 3200:
        return 2160
    if long_side >= 1700:
        return 1080
    if long_side >= 1150:
        return 720
    if long_side >= 700:
        return 480
    return 360


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
        if "released_at" not in cols:
            # Eski kinolar yaratilgan vaqtidan "chiqqan" hisoblanadi — kechikish ularga ta'sir qilmaydi
            await self.conn.execute("UPDATE anime SET released_at=created_at WHERE released_at IS NULL")
        if "notified_vip" not in cols:
            # Eski kinolar uchun janr obunachilariga xabar yuborilmaydi
            await self.conn.execute("UPDATE anime SET notified_vip=1, notified_free=1")

        cur = await self.conn.execute("PRAGMA table_info(users)")
        ucols = {row[1] for row in await cur.fetchall()}
        if "trial_used" not in ucols:
            await self.conn.execute("ALTER TABLE users ADD COLUMN trial_used INTEGER NOT NULL DEFAULT 0")
            # Eski foydalanuvchilar sinovni qayta olmasin — faqat yangi kelganlar oladi
            await self.conn.execute("UPDATE users SET trial_used=1")
        await self.conn.executescript(KINO_SCHEMA)

        cur = await self.conn.execute("PRAGMA table_info(movie_versions)")
        vcols = {row[1] for row in await cur.fetchall()}
        if "quality" not in vcols:
            await self.conn.execute("ALTER TABLE movie_versions ADD COLUMN quality INTEGER")
        cur = await self.conn.execute("PRAGMA table_info(vip_purchases)")
        pcols = {row[1] for row in await cur.fetchall()}
        if "trigger_anime_id" not in pcols:
            await self.conn.execute("ALTER TABLE vip_purchases ADD COLUMN trigger_anime_id INTEGER")

        # Eski bitta MAIN_CHANNEL ni e'lon kanallari ro'yxatiga bir marta ko'chiramiz
        cur = await self.conn.execute("SELECT COUNT(*) FROM announce_channels")
        (n_ch,) = await cur.fetchone()
        if n_ch == 0 and config.MAIN_CHANNEL:
            daily_time = await self.get_setting("daily_post_time", "12:00")
            daily_on = await self.get_setting("daily_post_enabled", "1")
            await self.conn.execute(
                "INSERT OR IGNORE INTO announce_channels (chat_id, title, enabled, daily_enabled, daily_time, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (config.MAIN_CHANNEL, "Asosiy kanal", 1, 1 if daily_on == "1" else 0, daily_time or "12:00", _now()),
            )
            await self.conn.execute(
                "INSERT OR IGNORE INTO announcements (anime_id, chat_id, message_id, has_photo, posted_at) "
                "SELECT id, ?, channel_message_id, "
                "CASE WHEN poster_file_id IS NOT NULL AND poster_file_id<>'' THEN 1 ELSE 0 END, created_at "
                "FROM anime WHERE channel_message_id IS NOT NULL",
                (config.MAIN_CHANNEL,),
            )
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

    async def record_auto_payment(self, charge_id: str, telegram_id: int, months: int, amount: int,
                                  currency: str, method: str) -> bool:
        """Avtomatik to'lovni yozadi. True — yangi; False — bu to'lov allaqachon qayta ishlangan."""
        try:
            await self.conn.execute(
                "INSERT INTO auto_payments (charge_id, telegram_id, months, amount, currency, method, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (charge_id, telegram_id, months, amount, currency, method, _now()),
            )
            await self.conn.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

    async def delete_auto_payment(self, charge_id: str) -> None:
        await self.conn.execute("DELETE FROM auto_payments WHERE charge_id=?", (charge_id,))
        await self.conn.commit()

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
            sql = "SELECT COUNT(*), COALESCE(SUM(price),0) FROM vip_purchases WHERE source IN ('payment','auto','stars')"
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
    async def add_version(self, anime_id: int, label: str, video_file_id: str, quality: Optional[int] = None) -> int:
        cur = await self.conn.execute(
            "INSERT INTO movie_versions (anime_id, label, video_file_id, quality, created_at) VALUES (?,?,?,?,?)",
            (anime_id, label, video_file_id, quality, _now()),
        )
        await self.conn.commit()
        return cur.lastrowid

    async def list_versions(self, anime_id: int) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            "SELECT * FROM movie_versions WHERE anime_id=? ORDER BY COALESCE(quality, 0), id", (anime_id,)
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

    # ------------------------------------------------------------------ #
    # ⏳ Erta kirish (yangi kino VIP'ga darhol, oddiylarga N soatdan keyin)
    # ------------------------------------------------------------------ #
    @staticmethod
    def early_unlock_at(anime, hours: int) -> Optional[datetime.datetime]:
        """Oddiy foydalanuvchiga qachon ochilishi (UTC). Kechikish yo'q yoki o'tib ketgan bo'lsa None."""
        keys = anime.keys()
        if hours <= 0 or ("open_to_all" in keys and anime["open_to_all"]):
            return None
        base = (anime["released_at"] if "released_at" in keys else None) or anime["created_at"]
        try:
            unlock = datetime.datetime.fromisoformat(base) + datetime.timedelta(hours=hours)
        except (TypeError, ValueError):
            return None
        return unlock if unlock > datetime.datetime.utcnow() else None

    async def mark_released(self, anime_id: int, hours: int) -> None:
        """
        E'lon qilinganda chaqiriladi. Kino endigina qo'shilgan bo'lsa, kechikish hisobi hozirdan boshlanadi;
        eski kino (kechikish davridan o'tgan) qayta qulflanmaydi.
        """
        cur = await self.conn.execute("SELECT created_at, released_at FROM anime WHERE id=?", (anime_id,))
        row = await cur.fetchone()
        if not row or row["released_at"]:
            return
        created = datetime.datetime.fromisoformat(row["created_at"])
        fresh = (datetime.datetime.utcnow() - created) < datetime.timedelta(hours=max(hours, 24))
        await self.conn.execute(
            "UPDATE anime SET released_at=? WHERE id=?", (_now() if fresh else row["created_at"], anime_id)
        )
        await self.conn.commit()

    async def toggle_open_to_all(self, anime_id: int) -> bool:
        cur = await self.conn.execute("SELECT open_to_all FROM anime WHERE id=?", (anime_id,))
        row = await cur.fetchone()
        new = 0 if (row and row["open_to_all"]) else 1
        await self.conn.execute("UPDATE anime SET open_to_all=? WHERE id=?", (new, anime_id))
        await self.conn.commit()
        return bool(new)

    async def set_ai_hook(self, anime_id: int, text: Optional[str]) -> None:
        await self.conn.execute("UPDATE anime SET ai_hook=? WHERE id=?", (text, anime_id))
        await self.conn.commit()

    # ------------------------------------------------------------------ #
    # Statistika: ko'rishlar, sifat, VIP'ga olib kelgan kinolar
    # ------------------------------------------------------------------ #
    async def log_view_event(self, telegram_id: int, anime_id: int, version_id: Optional[int], quality: Optional[int]) -> None:
        await self.conn.execute(
            "INSERT INTO view_events (telegram_id, anime_id, version_id, quality, created_at) VALUES (?,?,?,?,?)",
            (telegram_id, anime_id, version_id, quality, _now()),
        )
        await self.conn.commit()

    async def quality_stats(self, days: int = 30) -> list[tuple[Optional[int], int]]:
        since = (datetime.datetime.utcnow() - datetime.timedelta(days=days)).isoformat(timespec="seconds")
        cur = await self.conn.execute(
            "SELECT quality, COUNT(*) AS n FROM view_events WHERE created_at >= ? AND quality IS NOT NULL "
            "GROUP BY quality ORDER BY quality",
            (since,),
        )
        return [(r["quality"], r["n"]) for r in await cur.fetchall()]

    async def set_vip_trigger(self, telegram_id: int, anime_id: Optional[int], reason: str) -> None:
        """Foydalanuvchi qaysi kino sabab VIP taklifini ko'rganini eslab qoladi (keyin xarid shunga bog'lanadi)."""
        if not anime_id:
            return
        await self.conn.execute(
            "INSERT INTO vip_triggers (telegram_id, anime_id, reason, created_at) VALUES (?,?,?,?) "
            "ON CONFLICT(telegram_id) DO UPDATE SET anime_id=excluded.anime_id, reason=excluded.reason, "
            "created_at=excluded.created_at",
            (telegram_id, anime_id, reason, _now()),
        )
        await self.conn.commit()

    async def attribute_last_purchase(self, telegram_id: int) -> None:
        """Oxirgi VIP xaridni, oxirgi 7 kundagi taklif ko'rsatilgan kinoga bog'laydi."""
        since = (datetime.datetime.utcnow() - datetime.timedelta(days=7)).isoformat(timespec="seconds")
        cur = await self.conn.execute(
            "SELECT anime_id FROM vip_triggers WHERE telegram_id=? AND created_at >= ?", (telegram_id, since)
        )
        trig = await cur.fetchone()
        if not trig:
            return
        await self.conn.execute(
            "UPDATE vip_purchases SET trigger_anime_id=? WHERE id=("
            "SELECT p.id FROM vip_purchases p JOIN users u ON u.id=p.user_id WHERE u.telegram_id=? "
            "ORDER BY p.id DESC LIMIT 1)",
            (trig["anime_id"], telegram_id),
        )
        await self.conn.execute("DELETE FROM vip_triggers WHERE telegram_id=?", (telegram_id,))
        await self.conn.commit()

    async def vip_trigger_stats(self, limit: int = 10) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            "SELECT a.title AS title, a.anime_code AS code, COUNT(*) AS sales, COALESCE(SUM(p.price),0) AS total "
            "FROM vip_purchases p JOIN anime a ON a.id=p.trigger_anime_id "
            "WHERE p.trigger_anime_id IS NOT NULL GROUP BY a.id ORDER BY sales DESC, total DESC LIMIT ?",
            (limit,),
        )
        return await cur.fetchall()

    # ------------------------------------------------------------------ #
    # 📣 E'lon kanallari
    # ------------------------------------------------------------------ #
    async def list_announce_channels(self, only_enabled: bool = False) -> list[aiosqlite.Row]:
        sql = "SELECT * FROM announce_channels"
        if only_enabled:
            sql += " WHERE enabled=1"
        cur = await self.conn.execute(sql + " ORDER BY id")
        return await cur.fetchall()

    async def get_announce_channel(self, channel_id: int) -> Optional[aiosqlite.Row]:
        cur = await self.conn.execute("SELECT * FROM announce_channels WHERE id=?", (channel_id,))
        return await cur.fetchone()

    async def add_announce_channel(self, chat_id: str, title: str, daily_time: str = "12:00") -> Optional[int]:
        try:
            cur = await self.conn.execute(
                "INSERT INTO announce_channels (chat_id, title, daily_time, created_at) VALUES (?,?,?,?)",
                (chat_id, title, daily_time, _now()),
            )
            await self.conn.commit()
            return cur.lastrowid
        except aiosqlite.IntegrityError:
            return None

    _CHANNEL_FIELDS = {"title", "enabled", "daily_enabled", "daily_time", "last_daily_date"}

    async def update_announce_channel(self, channel_id: int, **fields: Any) -> None:
        for k in fields:
            if k not in self._CHANNEL_FIELDS:
                raise ValueError(f"Ruxsat etilmagan maydon: {k}")
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        await self.conn.execute(f"UPDATE announce_channels SET {sets} WHERE id=?", (*fields.values(), channel_id))
        await self.conn.commit()

    async def delete_announce_channel(self, channel_id: int) -> None:
        await self.conn.execute("DELETE FROM announce_channels WHERE id=?", (channel_id,))
        await self.conn.commit()

    async def save_announcement(self, anime_id: int, chat_id: str, message_id: int, has_photo: bool) -> None:
        await self.conn.execute(
            "INSERT INTO announcements (anime_id, chat_id, message_id, has_photo, posted_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(anime_id, chat_id) DO UPDATE SET message_id=excluded.message_id, "
            "has_photo=excluded.has_photo, posted_at=excluded.posted_at",
            (anime_id, chat_id, message_id, 1 if has_photo else 0, _now()),
        )
        await self.conn.commit()

    async def list_announcements(self, anime_id: int) -> list[aiosqlite.Row]:
        cur = await self.conn.execute("SELECT * FROM announcements WHERE anime_id=?", (anime_id,))
        return await cur.fetchall()

    async def daily_candidates(self, chat_id: str, exclude_ids: set[int], days: int = 30) -> list[aiosqlite.Row]:
        """
        Kunlik random uchun nomzodlar: videosi bor, shu kanalga oxirgi N kunda yuborilmagan,
        bugun boshqa kanalga tanlanmagan kinolar. Hammasi tugasa — takrorlash tarixi hisobga olinmaydi.
        """
        since = (datetime.datetime.utcnow() - datetime.timedelta(days=days)).isoformat(timespec="seconds")
        base = (
            "SELECT a.* FROM anime a WHERE a.is_published=1 "
            "AND (EXISTS (SELECT 1 FROM episodes e WHERE e.anime_id=a.id) "
            "     OR EXISTS (SELECT 1 FROM movie_versions v WHERE v.anime_id=a.id))"
        )
        ex_ids = list(exclude_ids)
        ex_sql = f" AND a.id NOT IN ({','.join('?' * len(ex_ids))})" if ex_ids else ""
        cur = await self.conn.execute(
            base + ex_sql
            + " AND a.id NOT IN (SELECT anime_id FROM channel_daily_history WHERE chat_id=? AND posted_at >= ?)",
            (*ex_ids, chat_id, since),
        )
        rows = await cur.fetchall()
        if rows:
            return rows
        cur = await self.conn.execute(base + ex_sql, tuple(ex_ids))
        return await cur.fetchall()

    async def record_daily(self, chat_id: str, anime_id: int) -> None:
        await self.conn.execute(
            "INSERT INTO channel_daily_history (chat_id, anime_id, posted_at) VALUES (?,?,?)",
            (chat_id, anime_id, _now()),
        )
        await self.conn.commit()

    async def today_daily_anime_ids(self) -> set[int]:
        cur = await self.conn.execute(
            "SELECT DISTINCT anime_id FROM channel_daily_history WHERE posted_at >= ?", (_local_midnight_utc_iso(),)
        )
        return {r["anime_id"] for r in await cur.fetchall()}

    # ------------------------------------------------------------------ #
    # 🕐 Rejalashtirilgan postlar
    # ------------------------------------------------------------------ #
    async def add_scheduled_post(self, anime_id: int, run_at_utc: datetime.datetime) -> int:
        cur = await self.conn.execute(
            "INSERT INTO scheduled_posts (anime_id, run_at, created_at) VALUES (?,?,?)",
            (anime_id, run_at_utc.isoformat(timespec="seconds"), _now()),
        )
        await self.conn.commit()
        return cur.lastrowid

    async def due_scheduled_posts(self) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            "SELECT * FROM scheduled_posts WHERE status='pending' AND run_at <= ? ORDER BY run_at", (_now(),)
        )
        return await cur.fetchall()

    async def list_scheduled_posts(self) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            "SELECT s.*, a.title AS title, a.anime_code AS code FROM scheduled_posts s "
            "JOIN anime a ON a.id=s.anime_id WHERE s.status='pending' ORDER BY s.run_at"
        )
        return await cur.fetchall()

    async def set_scheduled_status(self, sched_id: int, status: str) -> None:
        await self.conn.execute("UPDATE scheduled_posts SET status=? WHERE id=?", (status, sched_id))
        await self.conn.commit()

    # ------------------------------------------------------------------ #
    # 🔔 Janrga obuna
    # ------------------------------------------------------------------ #
    async def toggle_genre_sub(self, telegram_id: int, genre_id: int) -> bool:
        cur = await self.conn.execute(
            "SELECT 1 FROM genre_subs WHERE telegram_id=? AND genre_id=?", (telegram_id, genre_id)
        )
        if await cur.fetchone():
            await self.conn.execute(
                "DELETE FROM genre_subs WHERE telegram_id=? AND genre_id=?", (telegram_id, genre_id)
            )
            await self.conn.commit()
            return False
        await self.conn.execute(
            "INSERT OR IGNORE INTO genre_subs (telegram_id, genre_id) VALUES (?,?)", (telegram_id, genre_id)
        )
        await self.conn.commit()
        return True

    async def user_genre_subs(self, telegram_id: int) -> set[int]:
        cur = await self.conn.execute("SELECT genre_id FROM genre_subs WHERE telegram_id=?", (telegram_id,))
        return {r["genre_id"] for r in await cur.fetchall()}

    async def genre_subscribers_for_anime(self, anime_id: int) -> list[int]:
        cur = await self.conn.execute(
            "SELECT DISTINCT s.telegram_id FROM genre_subs s JOIN anime_genres ag ON ag.genre_id=s.genre_id "
            "WHERE ag.anime_id=?",
            (anime_id,),
        )
        return [r["telegram_id"] for r in await cur.fetchall()]

    async def set_notified(self, anime_id: int, vip: Optional[bool] = None, free: Optional[bool] = None) -> None:
        if vip is not None:
            await self.conn.execute("UPDATE anime SET notified_vip=? WHERE id=?", (1 if vip else 0, anime_id))
        if free is not None:
            await self.conn.execute("UPDATE anime SET notified_free=? WHERE id=?", (1 if free else 0, anime_id))
        await self.conn.commit()

    async def anime_pending_free_notify(self) -> list[aiosqlite.Row]:
        cur = await self.conn.execute("SELECT * FROM anime WHERE notified_vip=1 AND notified_free=0 AND is_published=1")
        return await cur.fetchall()

