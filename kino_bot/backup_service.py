"""
Avtomatik zaxira nusxa xizmati.

  * Har N soatda (standart: 2 soat) bazaning to'liq nusxasi olinadi — foydalanuvchilar, VIP, referal,
    animelar, sozlamalar, hammasi bitta faylda.
  * Anime yoki qismlar saqlanganda ham nusxa olinadi: oxirgi o'zgarishdan 1 daqiqa keyin BITTA nusxa
    (ketma-ket ko'p o'zgarish bo'lsa ham nusxalar ko'payib ketmaydi).
  * Nusxa zip qilib siqiladi va SUPER_ADMIN_IDS ga hamda (ulangan bo'lsa) zaxira kanaliga yuboriladi.
  * Oxirgi nusxa vaqti bazada saqlanadi — bot qayta ishga tushsa ham hisob buzilmaydi.
"""
from __future__ import annotations

import asyncio
import datetime
import glob
import html
import logging
import os
import shutil
import time
import zipfile
from typing import Optional

import alerts
import archive
import config
from tg_utils import call_with_retry

logger = logging.getLogger("anime_bot.backup")

TG_FILE_LIMIT = 49 * 1024 * 1024      # Telegram bot orqali yuboriladigan fayl chegarasi (50 MB) dan sal kam
DEBOUNCE_SECONDS = 60

_service: Optional["BackupService"] = None


def init(bot, db) -> "BackupService":
    global _service
    _service = BackupService(bot, db)
    return _service


def get() -> Optional["BackupService"]:
    return _service


def request_backup(reason: str) -> None:
    """Handlerlardan chaqiriladi: o'zgarish bo'ldi — 1 daqiqa tinchlikdan keyin nusxa olinadi."""
    if _service is not None:
        _service.request(reason)


def extract_db_from_zip(zip_path: str, dest_path: str) -> None:
    """Zaxira zip ichidan .db faylni xavfsiz chiqarib oladi."""
    with zipfile.ZipFile(zip_path) as zf:
        members = [i for i in zf.infolist() if not i.is_dir() and i.filename.lower().endswith(".db")]
        if not members:
            raise ValueError("zip ichida .db fayl topilmadi")
        info = members[0]
        if info.file_size > 4 * 1024 ** 3:
            raise ValueError("zip ichidagi fayl juda katta")
        with zf.open(info) as src, open(dest_path, "wb") as out:
            shutil.copyfileobj(src, out)


def _human_size(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.1f} MB"
    return f"{max(1, n // 1024)} KB"


def _int(value: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


class BackupService:
    def __init__(self, bot, db):
        self.bot = bot
        self.db = db
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None
        self._last_request = 0.0
        self._dirty = False
        self._reasons: list[str] = []

    # ------------------------------------------------------------------ #
    # O'zgarishdan keyingi nusxa (debounce)
    # ------------------------------------------------------------------ #
    def request(self, reason: str) -> None:
        self._last_request = time.monotonic()
        self._dirty = True
        if reason and reason not in self._reasons and len(self._reasons) < 5:
            self._reasons.append(reason)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._debounced())

    async def _debounced(self) -> None:
        try:
            while True:
                while True:
                    wait = DEBOUNCE_SECONDS - (time.monotonic() - self._last_request)
                    if wait <= 0:
                        break
                    await asyncio.sleep(wait)
                self._dirty = False
                reason = ", ".join(self._reasons) or "o'zgarish"
                self._reasons = []
                await self.run(reason)
                if not self._dirty:
                    break
        except asyncio.CancelledError:
            raise
        except Exception as e:
            await alerts.report(self.bot, "backup (o'zgarishdan keyin)", e)

    # ------------------------------------------------------------------ #
    # Nusxa olish va yuborish
    # ------------------------------------------------------------------ #
    async def _db_is_empty(self) -> bool:
        """Baza "bo'sh" = animelar yo'q va super-adminlardan boshqa foydalanuvchi ham yo'q (ular har safar avto-yaratiladi)."""
        if (await self.db.stats())["anime"] != 0:
            return False
        return await self.db.count_users_except(list(config.SUPER_ADMIN_IDS)) == 0

    async def run(self, reason: str, force: bool = False) -> Optional[str]:
        """Nusxa oladi va yuboradi. force=True — sozlamalar (yoqilgan/o'chirilgan, bo'sh baza) hisobga olinmaydi."""
        async with self._lock:
            if not force:
                if await self.db.get_setting("backup_enabled", "1") != "1":
                    return None
                if await self._db_is_empty():
                    logger.info("Baza bo'sh — avto-zaxira o'tkazib yuborildi.")
                    return None
            path = await self.db.backup()
            size = os.path.getsize(path)
            stats = await self.db.stats()
            caption = self._caption(reason, stats, size)
            sent = await self._deliver(path, size, caption)
            if sent > 0:
                await self.db.set_setting("backup_last_ts", str(int(time.time())))
            await asyncio.to_thread(self._prune, _int(await self.db.get_setting("backup_keep", "10"), 10, 3, 100))
            return path

    def _caption(self, reason: str, stats: dict, size: int) -> str:
        now = datetime.datetime.now(config.LOCAL_TZ).strftime("%d.%m.%Y %H:%M")
        return (
            f"💾 <b>Avto-zaxira</b> — {html.escape(reason, quote=False)}\n"
            f"🕒 {now}\n"
            f"👥 {stats['users']} · 💎 {stats['vip_users']} · 🎬 {stats['anime']} anime · 📼 {stats['episodes']} qism\n"
            f"📦 {_human_size(size)}"
        )

    async def _deliver(self, path: str, size: int, caption: str) -> int:
        from aiogram.types import FSInputFile

        if size > TG_FILE_LIMIT:
            await alerts.notify_admins(
                self.bot,
                f"⚠️ Zaxira nusxa juda katta ({_human_size(size)}) — Telegram orqali yuborib bo'lmaydi.\n"
                f"Fayl serverda saqlandi: <code>{html.escape(path, quote=False)}</code>",
                key="backup_too_big",
                cooldown=6 * 3600,
            )
            return 0

        destinations: list = list(config.SUPER_ADMIN_IDS)
        channel = await archive.get_storage_chat(self.db)
        if channel is not None:
            destinations.append(channel)

        ok = 0
        channel_failed = False
        for dest in destinations:
            try:
                await call_with_retry(
                    lambda d=dest: self.bot.send_document(
                        d, FSInputFile(path), caption=caption, disable_notification=True
                    )
                )
                ok += 1
            except Exception:
                logger.exception("Zaxirani yuborib bo'lmadi: %s", dest)
                if dest == channel:
                    channel_failed = True
        if ok == 0:
            await alerts.notify_admins(
                self.bot,
                "⚠️ <b>Zaxira nusxani hech qayerga yuborib bo'lmadi.</b>\n"
                "SUPER_ADMIN_IDS to'g'riligini va botga /start bosilganini tekshiring.",
                key="backup_send_fail",
                cooldown=3600,
            )
        elif channel_failed:
            await alerts.notify_admins(
                self.bot,
                "⚠️ Zaxira nusxa zaxira kanaliga yuborilmadi (adminlarga yuborildi). "
                "Bot kanalda admin ekanini tekshiring: 💾 Zaxira nusxa → 📦 Zaxira kanali.",
                key="backup_channel_fail",
                cooldown=6 * 3600,
            )
        return ok

    def _prune(self, keep: int) -> None:
        """Serverdagi eski nusxalarni o'chiradi (oxirgi `keep` tasi qoladi)."""
        try:
            files = []
            for pattern in ("anime_backup_*.zip", "anime_[0-9]*.db", "anime_[0-9]*.zip"):
                files.extend(glob.glob(os.path.join(config.BACKUP_DIR, pattern)))
            files = sorted(set(files), key=os.path.getmtime, reverse=True)
            for old in files[keep:]:
                try:
                    os.remove(old)
                except OSError:
                    pass
            # tiklash paytida qolib ketgan vaqtinchalik fayllar
            for tmp in glob.glob(os.path.join(config.BACKUP_DIR, "_restore_*")):
                if time.time() - os.path.getmtime(tmp) > 24 * 3600:
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
        except Exception:
            logger.exception("Eski nusxalarni tozalashda xatolik")

    # ------------------------------------------------------------------ #
    # Rejali nusxa (har N soatda)
    # ------------------------------------------------------------------ #
    async def scheduler_loop(self) -> None:
        await asyncio.sleep(30)
        while True:
            try:
                if await self.db.get_setting("backup_enabled", "1") == "1":
                    hours = _int(await self.db.get_setting("backup_interval_hours", "2"), 2, 1, 168)
                    last = _int(await self.db.get_setting("backup_last_ts", "0"), 0, 0, 10 ** 12)
                    attempt = _int(await self.db.get_setting("backup_last_attempt_ts", "0"), 0, 0, 10 ** 12)
                    now = int(time.time())
                    if now - last >= hours * 3600 and now - attempt >= 600:
                        await self.db.set_setting("backup_last_attempt_ts", str(now))
                        await self.run(f"har {hours} soatlik")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                await alerts.report(self.bot, "backup (rejali)", e)
            await asyncio.sleep(60)

    async def startup_check(self) -> None:
        """Bot bo'sh baza bilan ishga tushsa (masalan hosting diski tozalangan bo'lsa), adminlarga xabar beradi."""
        try:
            await asyncio.sleep(5)
            if await self._db_is_empty():
                await alerts.notify_admins(
                    self.bot,
                    "⚠️ <b>Bot BO'SH baza bilan ishga tushdi.</b>\n\n"
                    "Agar bu botning birinchi ishga tushirilishi bo'lsa, e'tibor bermang.\n"
                    "Aks holda eski ma'lumotlarni tiklash uchun oxirgi zaxira faylini (.zip yoki .db) yuboring: "
                    "⚙️ Admin panel → 💾 Zaxira nusxa → 📥 Zaxiradan tiklash.",
                    key="empty_db_start",
                    cooldown=3600,
                )
        except Exception as e:
            await alerts.report(self.bot, "startup_check", e)
