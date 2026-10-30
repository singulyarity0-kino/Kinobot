"""
Video zaxira kanali.

Nima uchun kerak: Telegramda video `file_id` si faqat SHU bot uchun ishlaydi. Token almashsa yoki yangi bot
ochilsa, bazadagi barcha `file_id` lar ishlamay qolishi mumkin. Videolarning haqiqiy nusxasi yopiq kanalda
tursa, ularni har doim qayta tiklash mumkin:

  * yangi video saqlanganda u avtomatik kanalga ham joylanadi;
  * eski videolarni admin paneldan bir tugma bilan kanalga ko'chirish mumkin;
  * `file_id` ishlamay qolsa, foydalanuvchiga video kanaldan nusxalanib yuboriladi;
  * yangi bot uchun barcha `file_id` larni kanaldan qayta olish mumkin ("♻️ file_id'larni yangilash").
"""
from __future__ import annotations

import asyncio
import html
import logging
from typing import Optional, Union

from aiogram.exceptions import TelegramBadRequest

import alerts
import config
from tg_utils import call_with_retry

logger = logging.getLogger("anime_bot.archive")

SEND_DELAY = 1.2          # kanalga ketma-ket yuborishda pauza (Telegram limiti uchun)
_tasks: set[asyncio.Task] = set()
_busy = False             # ko'chirish / yangilash jarayoni ketayotganini bildiradi

ChatRef = Union[int, str]


def parse_chat_ref(raw: str) -> Optional[ChatRef]:
    raw = (raw or "").strip()
    if not raw:
        return None
    if raw.lstrip("-").isdigit():
        return int(raw)
    if raw.startswith("https://t.me/") or raw.startswith("http://t.me/") or raw.startswith("t.me/"):
        name = raw.rstrip("/").split("/")[-1]
        if name and not name.startswith("+") and name != "joinchat":
            return "@" + name.lstrip("@")
        return None
    if raw.startswith("@"):
        return raw
    return "@" + raw


async def get_storage_chat(db) -> Optional[ChatRef]:
    raw = await db.get_setting("storage_channel_id", "") or config.BACKUP_CHANNEL
    return parse_chat_ref(raw)


def is_busy() -> bool:
    return _busy


async def verify_channel(bot, ref: ChatRef) -> tuple[bool, object, str]:
    """Bot kanalga xabar yubora olishini tekshiradi. (ok, chat, xato_matni)"""
    try:
        chat = await bot.get_chat(ref)
    except Exception as e:
        return False, None, f"Kanal topilmadi yoki bot unga kira olmayapti ({e})."
    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(chat.id, me.id)
    except Exception as e:
        return False, chat, f"Botning kanaldagi holatini aniqlab bo'lmadi ({e})."
    status = getattr(member, "status", "")
    if status == "creator":
        return True, chat, ""
    if status != "administrator":
        return False, chat, "Bot bu kanalda admin emas. Botni kanalga ADMIN qilib qo'shing."
    if getattr(chat, "type", "") == "channel" and getattr(member, "can_post_messages", True) is False:
        return False, chat, "Bot kanalda admin, lekin «Xabar yuborish» ruxsati yo'q."
    return True, chat, ""


def _caption(info) -> str:
    season = f" {info['season_number']}-fasl," if info["season_number"] else ""
    return (
        f"🎬 {html.escape(info['title'])} —{season} {info['episode_number']}-qism\n"
        f"#A{info['anime_code']} #E{info['id']}"
    )


async def archive_episode(bot, db, episode_id: int) -> bool:
    """Bitta qism videosini zaxira kanalga joylaydi. Allaqachon saqlangan bo'lsa — hech narsa qilmaydi."""
    chat = await get_storage_chat(db)
    if chat is None:
        return False
    info = await db.get_episode_archive_info(episode_id)
    if not info or info["is_filler"] or not info["video_file_id"]:
        return True
    if info["archive_message_id"]:
        return True
    msg = await call_with_retry(
        lambda: bot.send_video(chat, info["video_file_id"], caption=_caption(info), disable_notification=True)
    )
    await db.set_episode_archive(episode_id, msg.chat.id, msg.message_id)
    return True


def schedule_archive(bot, db, episode_ids: list[int]) -> None:
    """Yangi qo'shilgan qismlarni fonda (admin kutib qolmasdan) kanalga saqlaydi."""
    if not episode_ids:
        return
    task = asyncio.create_task(_archive_batch(bot, db, list(episode_ids)))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _archive_batch(bot, db, ids: list[int]) -> None:
    try:
        if await get_storage_chat(db) is None:
            return
        failures = 0
        for eid in ids:
            try:
                await archive_episode(bot, db, eid)
                failures = 0
            except Exception as e:
                failures += 1
                logger.exception("Qismni zaxira kanalga saqlab bo'lmadi: %s", eid)
                if failures >= 3:
                    await alerts.notify_admins(
                        bot,
                        "⚠️ <b>Videolarni zaxira kanalga saqlab bo'lmayapti.</b>\n"
                        f"<code>{html.escape(str(e)[:200], quote=False)}</code>\n\n"
                        "Bot kanalda admin ekanini tekshiring: 💾 Zaxira nusxa → 📦 Zaxira kanali.",
                        key="archive_fail",
                        cooldown=3600,
                    )
                    return
            await asyncio.sleep(SEND_DELAY)
    except Exception as e:
        await alerts.report(bot, "archive_batch", e)


async def migrate_old(bot, db, admin_chat_id: int) -> None:
    """Hali kanalga saqlanmagan barcha eski videolarni kanalga ko'chiradi (qayta ishga tushirsa davom etadi)."""
    global _busy
    if _busy:
        await bot.send_message(admin_chat_id, "⏳ Boshqa zaxira jarayoni hozir ketmoqda. Tugashini kuting.")
        return
    _busy = True
    try:
        chat = await get_storage_chat(db)
        if chat is None:
            await bot.send_message(admin_chat_id, "❗ Avval zaxira kanalini ulang.")
            return
        rows = await db.list_episodes_without_archive()
        total = len(rows)
        if total == 0:
            await bot.send_message(admin_chat_id, "✅ Hamma videolar allaqachon zaxira kanalda.")
            return
        progress = await bot.send_message(admin_chat_id, f"📤 Ko'chirish boshlandi: {total} ta video...")
        done = failed = consecutive = 0
        for i, row in enumerate(rows, 1):
            try:
                await archive_episode(bot, db, row["id"])
                done += 1
                consecutive = 0
            except Exception as e:
                failed += 1
                consecutive += 1
                logger.warning("Ko'chirib bo'lmadi (qism %s): %s", row["id"], e)
                if consecutive >= 5 and done == 0:
                    await bot.send_message(
                        admin_chat_id,
                        "❌ Ketma-ket 5 ta video ko'chmadi, jarayon to'xtatildi.\n"
                        f"Oxirgi xato: <code>{html.escape(str(e)[:200], quote=False)}</code>\n\n"
                        "Bot kanalda admin ekanini tekshiring.",
                    )
                    return
            await asyncio.sleep(SEND_DELAY)
            if i % 15 == 0:
                try:
                    await progress.edit_text(f"📤 Ko'chirilmoqda: {i} / {total}  (xato: {failed})")
                except Exception:
                    pass
        text = f"✅ Ko'chirish tugadi: {done} ta saqlandi"
        if failed:
            text += f", {failed} tasi ko'chmadi (eski/yaroqsiz video bo'lishi mumkin). Qayta bossangiz, qolganlari urinib ko'riladi."
        await bot.send_message(admin_chat_id, text)
    except Exception as e:
        await alerts.report(bot, "archive_migrate", e)
    finally:
        _busy = False


async def resync_file_ids(bot, db, admin_chat_id: int) -> None:
    """
    Yangi bot (yoki yangi token) uchun: kanaldagi har bir videodan yangi `file_id` olib bazani yangilaydi.
    Bot videoni vaqtincha adminga forward qiladi va darrov o'chirib tashlaydi.
    """
    global _busy
    if _busy:
        await bot.send_message(admin_chat_id, "⏳ Boshqa zaxira jarayoni hozir ketmoqda. Tugashini kuting.")
        return
    _busy = True
    try:
        rows = await db.list_archived_episodes()
        if not rows:
            await bot.send_message(admin_chat_id, "ℹ️ Kanalga saqlangan video yo'q — yangilanadigan narsa yo'q.")
            return
        progress = await bot.send_message(admin_chat_id, f"♻️ file_id'lar yangilanmoqda: {len(rows)} ta video...")
        updated = failed = 0
        for i, row in enumerate(rows, 1):
            try:
                fwd = await call_with_retry(
                    lambda r=row: bot.forward_message(
                        admin_chat_id, r["archive_chat_id"], r["archive_message_id"], disable_notification=True
                    )
                )
                new_id = None
                if getattr(fwd, "video", None):
                    new_id = fwd.video.file_id
                elif getattr(fwd, "document", None):
                    new_id = fwd.document.file_id
                try:
                    await bot.delete_message(admin_chat_id, fwd.message_id)
                except Exception:
                    pass
                if new_id:
                    if new_id != row["video_file_id"]:
                        await db.update_episode_file_id(row["id"], new_id)
                    updated += 1
                else:
                    failed += 1
            except Exception as e:
                failed += 1
                logger.warning("file_id yangilanmadi (qism %s): %s", row["id"], e)
            await asyncio.sleep(0.8)
            if i % 20 == 0:
                try:
                    await progress.edit_text(f"♻️ Yangilanmoqda: {i} / {len(rows)}  (xato: {failed})")
                except Exception:
                    pass
        await bot.send_message(admin_chat_id, f"✅ Tayyor: {updated} ta video yangilandi, {failed} tasi xato.")
    except Exception as e:
        await alerts.report(bot, "archive_resync", e)
    finally:
        _busy = False


async def send_episode_video(bot, chat_id: int, episode, caption: str, protect: bool) -> None:
    """
    Videoni foydalanuvchiga yuboradi. `file_id` ishlamasa va video zaxira kanalda bo'lsa,
    kanaldan nusxalab yuboradi (foydalanuvchi hech narsani sezmaydi).
    """
    try:
        await bot.send_video(chat_id=chat_id, video=episode["video_file_id"], caption=caption, protect_content=protect)
        return
    except TelegramBadRequest as e:
        keys = episode.keys()
        a_chat = episode["archive_chat_id"] if "archive_chat_id" in keys else None
        a_msg = episode["archive_message_id"] if "archive_message_id" in keys else None
        if not (a_chat and a_msg):
            raise
        logger.warning("file_id ishlamadi (%s) — zaxira kanaldan nusxalanmoqda", e)
        await bot.copy_message(
            chat_id=chat_id, from_chat_id=a_chat, message_id=a_msg, caption=caption, protect_content=protect
        )
        await alerts.notify_admins(
            bot,
            "ℹ️ Ba'zi videolarning <code>file_id</code> si ishlamayapti, shuning uchun ular zaxira kanaldan yuborildi.\n"
            "Doimiy tuzatish: 💾 Zaxira nusxa → 📦 Zaxira kanali → ♻️ file_id'larni yangilash.",
            key="file_id_stale",
            cooldown=24 * 3600,
        )
