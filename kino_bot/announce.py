"""
📣 E'lon xizmati: bir nechta kanalga yangi kino e'loni, har kanalga o'z kunlik random kinosi,
rejalashtirilgan postlar va janr obunachilariga xabar.

  * Yangi kino / «Kanalga e'lon» — barcha YOQILGAN kanallarga boradi.
  * Kunlik random — har kanalning o'z vaqti bor; bir kunda ikki kanalga bir xil kino chiqmaydi,
    har kanalga oxirgi 30 kunda yuborilgan kino qaytmaydi.
  * Rejalashtirilgan post — belgilangan vaqtda barcha kanallarga chiqadi.
  * Janr obunasi — yangi kino chiqqanda VIP obunachilarga darhol, oddiylarga kechikish tugagach xabar.
"""
from __future__ import annotations

import asyncio
import datetime
import html
import logging
import random
import re
from dataclasses import dataclass, field
from typing import Optional

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import alerts
import config
import funnel
import gemini
from database import Database
from handlers.common import build_announcement_markup, build_anime_card_text, parse_hhmm

logger = logging.getLogger("kino_bot.announce")

NO_REPEAT_DAYS = 30
SEND_PAUSE = 0.4  # kanallar orasidagi pauza (Telegram limiti uchun)


@dataclass
class PostResult:
    sent: list[str] = field(default_factory=list)      # muvaffaqiyatli kanal nomlari
    failed: list[tuple[str, str]] = field(default_factory=list)  # (kanal nomi, xato)

    @property
    def ok(self) -> bool:
        return bool(self.sent)


def _hashtags(genres: list[str], year: Optional[str]) -> str:
    tags = []
    for g in genres[:4]:
        t = re.sub(r"[^\w]", "", g.lower(), flags=re.UNICODE)
        if t:
            tags.append(f"#{t}")
    if year and str(year)[:4].isdigit():
        tags.append(f"#{str(year)[:4]}")
    return " ".join(tags)


async def _hook_for(db: Database, anime) -> Optional[str]:
    """Kanal posti uchun Gemini jumlasi (bir marta yaratilib saqlanadi)."""
    if await db.get_setting("ai_channel_hook", "1") != "1":
        return None
    if anime["ai_hook"]:
        return anime["ai_hook"]  # oldin yaratilgan jumla — kalit bo'lmasa ham ishlataveramiz
    if not gemini.is_configured():
        return None
    desc = html.unescape(anime["description"] or "")
    hook = await gemini.hook_line(anime["title"], desc)
    if hook:
        hook = html.escape(hook, quote=False)
        await db.set_ai_hook(anime["id"], hook)
    return hook


async def build_post(bot: Bot, db: Database, anime) -> tuple[str, InlineKeyboardMarkup]:
    """Kanal posti: [jozibali jumla] + kino kartasi + hashtaglar + «Kinoni ko'rish» tugmasi."""
    hook = await _hook_for(db, anime)
    genres = [g["name"] for g in await db.get_anime_genres(anime["id"])]
    tags = _hashtags(genres, anime["release_year"])
    head = f"🔥 <b>{hook}</b>\n\n" if hook else ""
    tail = f"\n\n{tags}" if tags else ""
    reserve = len(head) + len(tail)
    body = await build_anime_card_text(db, anime, reserve=reserve)
    return f"{head}{body}{tail}", await build_announcement_markup(bot, anime)


async def _send_one(bot: Bot, db: Database, ch, anime, caption: str, markup) -> None:
    if anime["poster_file_id"]:
        sent = await bot.send_photo(ch["chat_id"], anime["poster_file_id"], caption=caption, reply_markup=markup)
        has_photo = True
    else:
        sent = await bot.send_message(ch["chat_id"], caption, reply_markup=markup)
        has_photo = False
    await db.save_announcement(anime["id"], ch["chat_id"], sent.message_id, has_photo)


async def post_anime(bot: Bot, db: Database, anime_id: int, channels: Optional[list] = None,
                     is_release: bool = True) -> PostResult:
    """
    Kinoni kanallarga yuboradi (channels berilmasa — barcha yoqilgan kanallarga).
    is_release=True: bu "yangi kino chiqdi" voqeasi — erta kirish hisobi boshlanadi va
    janr obunachilariga xabar yuboriladi. Kunlik random post uchun False.
    """
    result = PostResult()
    anime = await db.get_anime(anime_id)
    if not anime:
        return result
    if channels is None:
        channels = await db.list_announce_channels(only_enabled=True)
    if not channels:
        return result

    hours = await funnel.int_setting(db, "early_access_hours", 0)
    if is_release:
        await db.mark_released(anime_id, hours)
        anime = await db.get_anime(anime_id)

    caption, markup = await build_post(bot, db, anime)
    for i, ch in enumerate(channels):
        if i:
            await asyncio.sleep(SEND_PAUSE)
        try:
            await _send_one(bot, db, ch, anime, caption, markup)
            result.sent.append(ch["title"])
        except Exception as e:  # noqa: BLE001 — bir kanal yiqilsa qolganlariga yuboraveramiz
            logger.warning("Kanalga yuborilmadi (%s): %s", ch["chat_id"], e)
            result.failed.append((ch["title"], str(e)[:120]))

    if is_release and result.sent:
        try:
            await notify_release(bot, db, anime_id)
        except Exception:  # noqa: BLE001
            logger.exception("Janr obunachilariga xabar yuborishda xato")
    return result


# ------------------------------------------------------------------ #
# 🎲 Kunlik random (har kanalga o'zi)
# ------------------------------------------------------------------ #
async def pick_daily(db: Database, ch) -> Optional[object]:
    exclude = await db.today_daily_anime_ids()
    rows = await db.daily_candidates(ch["chat_id"], exclude, days=NO_REPEAT_DAYS)
    return random.choice(rows) if rows else None


async def post_daily(bot: Bot, db: Database, ch, record: bool = True) -> Optional[str]:
    """Kanalga random kino yuboradi; kino nomini qaytaradi (yuborilmasa None)."""
    anime = await pick_daily(db, ch)
    if not anime:
        return None
    res = await post_anime(bot, db, anime["id"], channels=[ch], is_release=False)
    if not res.ok:
        raise RuntimeError(res.failed[0][1] if res.failed else "yuborilmadi")
    if record:
        await db.record_daily(ch["chat_id"], anime["id"])
    return anime["title"]


async def daily_announcer(bot: Bot, db: Database) -> None:
    """
    Har 30 soniyada tekshiradi. Har kanalning o'z vaqti: kuniga bir marta; bot belgilangan vaqtdan
    keyin 3 soat ichida uyg'onsa ham yuboradi (Render uxlab qolgan holat uchun).
    """
    while True:
        await asyncio.sleep(30)
        try:
            now = datetime.datetime.now(config.LOCAL_TZ)
            today = now.strftime("%Y-%m-%d")
            for ch in await db.list_announce_channels(only_enabled=True):
                if not ch["daily_enabled"] or ch["last_daily_date"] == today:
                    continue
                hhmm = parse_hhmm(ch["daily_time"]) or "12:00"
                scheduled = now.replace(hour=int(hhmm[:2]), minute=int(hhmm[3:]), second=0, microsecond=0)
                if now < scheduled or now - scheduled > datetime.timedelta(hours=3):
                    continue
                # Avval belgilaymiz — xato bo'lsa ham bir kunda qayta-qayta urinmaydi
                await db.update_announce_channel(ch["id"], last_daily_date=today)
                try:
                    title = await post_daily(bot, db, ch)
                    logger.info("Kunlik kino (%s): %s", ch["title"], title)
                except Exception as e:  # noqa: BLE001
                    await alerts.report(bot, f"daily_announcer:{ch['title']}", e)
                await asyncio.sleep(SEND_PAUSE)
        except Exception as e:  # noqa: BLE001
            await alerts.report(bot, "daily_announcer", e)


# ------------------------------------------------------------------ #
# 🕐 Rejalashtirilgan postlar
# ------------------------------------------------------------------ #
async def scheduled_poster(bot: Bot, db: Database) -> None:
    while True:
        await asyncio.sleep(30)
        try:
            for s in await db.due_scheduled_posts():
                await db.set_scheduled_status(s["id"], "done")  # avval belgilaymiz — takror yuborilmasin
                res = await post_anime(bot, db, s["anime_id"])
                if not res.ok:
                    for admin_id in await db.all_admin_ids():
                        try:
                            await bot.send_message(
                                admin_id,
                                f"⚠️ Rejalashtirilgan post yuborilmadi (kino ID {s['anime_id']}). "
                                "Kanallar ulanganini va bot admin ekanini tekshiring.",
                            )
                        except Exception:  # noqa: BLE001
                            pass
        except Exception as e:  # noqa: BLE001
            await alerts.report(bot, "scheduled_poster", e)


# ------------------------------------------------------------------ #
# 🔔 Janr obunachilariga xabar
# ------------------------------------------------------------------ #
def _notify_markup(anime_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🎬 Kinoni ochish", callback_data=f"anime:{anime_id}")
    ]])


async def _send_many(bot: Bot, ids: list[int], text: str, markup: InlineKeyboardMarkup) -> int:
    sent = 0
    for tg in ids:
        try:
            await bot.send_message(tg, text, reply_markup=markup)
            sent += 1
        except Exception:  # noqa: BLE001 — bot bloklangan bo'lishi mumkin
            pass
        await asyncio.sleep(0.05)
    return sent


async def notify_release(bot: Bot, db: Database, anime_id: int) -> None:
    """Yangi kino chiqdi: kechikish bo'lsa VIP obunachilarga darhol, bo'lmasa hammasiga."""
    anime = await db.get_anime(anime_id)
    if not anime or anime["notified_vip"]:
        return
    hours = await funnel.int_setting(db, "early_access_hours", 0)
    locked_for_free = db.early_unlock_at(anime, hours) is not None
    subs = await db.genre_subscribers_for_anime(anime_id)
    title = html.escape(anime["title"], quote=False)
    genres = ", ".join(g["name"] for g in await db.get_anime_genres(anime_id))
    base = f"🔔 <b>Yangi kino:</b> {title}\n🎭 {genres}"
    if locked_for_free:
        vips = [tg for tg in subs if await db.is_vip(tg)]
        await _send_many(bot, vips, base + "\n\n💎 VIP sifatida sizga darhol ochiq!", _notify_markup(anime_id))
        await db.set_notified(anime_id, vip=True, free=False)
    else:
        await _send_many(bot, subs, base, _notify_markup(anime_id))
        await db.set_notified(anime_id, vip=True, free=True)


async def unlock_notifier(bot: Bot, db: Database) -> None:
    """Kechikish tugagan kinolar haqida oddiy obunachilarga xabar (VIP'lar allaqachon olgan)."""
    while True:
        await asyncio.sleep(300)
        try:
            hours = await funnel.int_setting(db, "early_access_hours", 0)
            for anime in await db.anime_pending_free_notify():
                if db.early_unlock_at(anime, hours) is not None:
                    continue
                await db.set_notified(anime["id"], free=True)  # avval belgilaymiz
                subs = await db.genre_subscribers_for_anime(anime["id"])
                free_subs = [tg for tg in subs if not await db.is_vip(tg)]
                title = html.escape(anime["title"], quote=False)
                await _send_many(
                    bot, free_subs, f"🔓 <b>{title}</b> endi hamma uchun ochiq!", _notify_markup(anime["id"])
                )
        except Exception as e:  # noqa: BLE001
            await alerts.report(bot, "unlock_notifier", e)
