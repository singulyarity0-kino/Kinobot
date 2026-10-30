"""Umumiy yordamchi funksiyalar: majburiy obuna tekshiruvi va h.k."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Dict, Optional

from aiogram import BaseMiddleware, Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, Message, TelegramObject, User as TgUser

import config
import keyboards as kb
from database import Database
from referral import process_referral_reward
from states import VipPaymentStates


class IsAdminFilter(BaseFilter):
    """Faqat is_admin=1 bo'lgan foydalanuvchilarga ruxsat beradi."""

    async def __call__(self, event: TelegramObject, db: Database) -> bool:
        user = event.from_user
        if user is None:
            return False
        return await db.is_admin(user.id)


async def is_subscribed_to_all(bot: Bot, db: Database, telegram_id: int) -> list:
    """
    Obuna bo'lmagan (yoki holatini ANIQLAB BO'LMAGAN) kanallar ro'yxatini qaytaradi
    (bo'sh ro'yxat = hammasiga obuna tasdiqlangan).

    Agar bot shu kanal/guruhda admin qilib qo'shilmagan bo'lsa (yoki chat topilmasa),
    foydalanuvchining haqiqiy obunasini TASDIQLAB bo'lmaydi — xavfsizlik uchun bu
    holat ham "obuna emas" deb hisoblanadi (silently o'tkazib yuborilmaydi), va
    natijadagi qatorga "_bot_not_admin" belgisi qo'shiladi — shu orqali chaqiruvchi
    kod (build_subscribe_prompt) foydalanuvchiga aniq sabab ko'rsata oladi.
    """
    channels = await db.list_required_channels()
    missing = []
    for ch in channels:
        try:
            member = await bot.get_chat_member(chat_id=ch["chat_id"], user_id=telegram_id)
            if member.status in ("left", "kicked"):
                missing.append(ch)
        except (TelegramBadRequest, TelegramForbiddenError):
            # Odatda bu "bot kanalda admin emas" yoki "chat topilmadi" degani.
            row = dict(ch)
            row["_bot_not_admin"] = True
            missing.append(row)
        except Exception:
            # Kutilmagan/noma'lum xato — avvalgidek, foydalanuvchini bloklamaymiz.
            continue
    return missing


def build_subscribe_prompt(missing: list) -> tuple[str, "kb.InlineKeyboardMarkup"]:
    """
    is_subscribed_to_all() natijasidan foydalanuvchiga ko'rsatiladigan matn va
    klaviaturani tayyorlaydi. Agar kanallardan birortasida bot admin emasligi
    aniqlangan bo'lsa (ko'rinishidan sabab shu), aniq diagnostika xabari chiqadi.
    """
    bot_issue = any(isinstance(ch, dict) and ch.get("_bot_not_admin") for ch in missing)
    if bot_issue:
        text = (
            "⚠️ Bot kanal/guruhda admin emas. Kanal sozlamalaridan botga admin huquqi bering.\n\n"
            "Shundan keyin ✅ Tekshirish tugmasini qayta bosing."
        )
    else:
        text = (
            "📢 Davom etish uchun quyidagi majburiy kanal(lar)ga obuna bo'ling, "
            "so'ng ✅ Tekshirish tugmasini bosing:"
        )
    return text, kb.subscribe_keyboard(missing)


CAPTION_LIMIT = 960  # Telegram rasm izohi chegarasi 1024 belgi — zaxira bilan (kunlik limit qatori uchun ham joy)


def _shorten_html(text: str, limit: int) -> str:
    """Escape qilingan matnni so'z chegarasida qisqartiradi (&amp; kabi bo'laklar kesilib qolmasin)."""
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    amp = cut.rfind("&")
    if amp != -1 and ";" not in cut[amp:]:
        cut = cut[:amp]
    return cut.rstrip(" ,.;:") + "…"


async def build_anime_card_text(db: Database, anime, locked: bool = False) -> str:
    """
    Kino haqida avtomatik, bir xil ko'rinishdagi matn tayyorlaydi — admin buni
    qo'lda yozmaydi, database ma'lumotlaridan hosil bo'ladi. Shu funksiya HAM
    kino sahifasida (user.py), HAM kanalga e'lon qilishda (admin.py) bir xil
    ko'rinishni ta'minlash uchun qayta ishlatiladi.
    locked=True — VIP bo'lmagan foydalanuvchiga ko'rsatilayotgan qulflangan karta.
    """
    keys = anime.keys()

    def val(name: str) -> str:
        return (anime[name] or "") if name in keys else ""

    genres = await db.get_anime_genres(anime["id"])
    genre_txt = ", ".join(g["name"] for g in genres) or "—"
    seasons = await db.list_seasons(anime["id"])
    ep_count = await db.count_episodes(anime["id"])
    avg, rated_count = await db.anime_avg_rating(anime["id"])
    versions = await db.list_versions(anime["id"])
    is_series = val("kind") == "series"

    year = val("release_year")
    title_line = f"🎬 <b>{anime['title']}</b>" + (f" ({year})" if year else "")
    lines = [
        title_line,
        f"🆔 Kino ID: <code>{anime['anime_code']}</code>",
        "",
        f"🎭 Janr: {genre_txt}",
    ]
    meta = []
    if val("country"):
        meta.append(f"🌍 {val('country')}")
    if val("duration"):
        meta.append(f"⏱ {val('duration')}")
    if val("age_rating"):
        meta.append(f"🔞 {val('age_rating')}")
    if meta:
        lines.append(" · ".join(meta))
    if val("director"):
        lines.append(f"🎬 Rejissyor: {val('director')}")
    if val("actors"):
        lines.append(f"👥 Aktyorlar: {val('actors')}")
    if seasons:
        lines.append(f"📺 {len(seasons)} fasl • {ep_count} qism")
    elif is_series or ep_count > 1:
        lines.append(f"📺 {ep_count} qism")
    if versions:
        lines.append(f"🎙 {len(versions)} xil variant (til/sifat) mavjud")
    else:
        if val("language"):
            lines.append(f"🎙 Til: {val('language')}")
        if val("quality"):
            lines.append(f"📀 Sifat: {val('quality')}")
    if val("subtitles"):
        lines.append(f"💬 Subtitr: {val('subtitles')}")
    ratings = []
    if val("imdb_rating"):
        ratings.append(f"🌟 TMDB: {val('imdb_rating')}")
    ratings.append(f"⭐ Reyting: {avg}/10" if rated_count else "⭐ Reyting: hali baholanmagan")
    lines.append(" · ".join(ratings))

    tail = []
    if anime["is_vip"]:
        tail.append("\n💎 VIP kino")
    if locked:
        tail.append("🔒 Ko'rish uchun VIP kerak")
    base = "\n".join(lines + tail)

    desc = anime["description"] or ""
    if desc:
        room = CAPTION_LIMIT - len(base) - 6
        if room > 80:
            desc = _shorten_html(desc, room)
            lines += ["", f"📖 {desc}"]
    return "\n".join(lines + tail)


async def has_vip_access(db: Database, telegram_id: int) -> bool:
    """
    Foydalanuvchi VIP kontentga kira oladimi: bot egasi/admin bo'lsa, o'zi haqiqiy VIP bo'lsa,
    YOKI hozir admin yoqqan vaqtinchalik "hammaga bepul" VIP rejimi
    faol bo'lsa — True qaytaradi.
    """
    # Bot egasi (SUPER_ADMIN_IDS) va adminlar VIP kontentni doim bepul ko'ra oladi
    if telegram_id in config.SUPER_ADMIN_IDS or await db.is_admin(telegram_id):
        return True
    if await db.is_vip(telegram_id):
        return True
    return await db.is_vip_free_mode()


class SubscriptionMiddleware(BaseMiddleware):
    """
    Har bir foydalanuvchi harakatida (matn buyruq yoki inline tugma bosilganda)
    majburiy kanal(lar)ga obuna HALI HAM davom etayotganini qayta tekshiradi.

    Muammo: avvalgi kodda bu tekshiruv faqat /start bosilganda bo'lardi — foydalanuvchi
    bir marta obuna bo'lib, keyin kanaldan chiqib ketsa ham bot buni bilmasdi.
    Endi har bir harakatda (admin va /start/✅ Tekshirishdan tashqari) tekshiriladi.
    """

    SKIP_CALLBACKS = {"check_subs", "vip_open"}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        tg_user: Optional[TgUser] = data.get("event_from_user")
        if tg_user is None:
            return await handler(event, data)

        if isinstance(event, Message):
            if event.text and event.text.startswith("/start"):
                return await handler(event, data)
        elif isinstance(event, CallbackQuery):
            if event.data in self.SKIP_CALLBACKS or (event.data and event.data.startswith("vip_buy:")):
                return await handler(event, data)
        else:
            return await handler(event, data)

        db: Database = data["db"]
        bot: Bot = data["bot"]

        # Adminlar bloklanmaydi — ular botni boshqarishda davom etishi kerak.
        if await db.is_admin(tg_user.id):
            return await handler(event, data)

        # "💎 VIP olish" majburiy obuna oynasidan ham chaqirilishi mumkin (talab
        # qilingan) — shuning uchun to'lov skrinshotini kutish bosqichida ham
        # obuna tekshiruvi bloklamasligi kerak, aks holda oqim yarim yo'lda to'xtab qoladi.
        state = data.get("state")
        if state is not None:
            current = await state.get_state()
            if current == VipPaymentStates.waiting_screenshot.state:
                return await handler(event, data)

        missing = await is_subscribed_to_all(bot, db, tg_user.id)
        if missing:
            text, markup = build_subscribe_prompt(missing)
            if isinstance(event, CallbackQuery):
                await event.answer("❗ Majburiy kanal(lar)ga obuna talab qilinadi.", show_alert=True)
                try:
                    await event.message.answer(text, reply_markup=markup)
                except Exception:
                    pass
            else:
                await event.answer(text, reply_markup=markup)
            return  # handlerga o'tkazmaymiz — harakat shu yerda to'xtaydi

        # Obunadan o'tgan foydalanuvchi: kutilayotgan referal mukofoti bo'lsa, shu yerda beriladi
        await process_referral_reward(bot, db, tg_user.id)
        return await handler(event, data)


async def ensure_user(db: Database, tg_user: TgUser):
    user = await db.get_user(tg_user.id)
    if user is None:
        user = await db.create_user(tg_user.id, tg_user.full_name, tg_user.username)
    else:
        await db.touch_user(tg_user.id, tg_user.full_name, tg_user.username)
    return user


def fmt_number(n: int | str) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return str(n)
    return f"{n:,}".replace(",", " ")


# ------------------------------------------------------------------ #
# Filler qismlar matni
# ------------------------------------------------------------------ #
def format_filler_ranges(ranges: list[tuple[int, int]]) -> str:
    """[(5, 10), (15, 15)] -> '• 5-qismdan 10-qismgacha — filler\n• 15-qism — filler'"""
    lines = []
    for a, b in ranges:
        if a == b:
            lines.append(f"• {a}-qism — filler")
        else:
            lines.append(f"• {a}-qismdan {b}-qismgacha — filler")
    return "\n".join(lines)


# ------------------------------------------------------------------ #
# Kanaldagi e'lonni (reyting o'zgarganda) yangilash
# ------------------------------------------------------------------ #
_log = logging.getLogger("anime_bot.common")
_REFRESH_COOLDOWN = 90.0  # bir anime e'lonini ko'pi bilan 90 soniyada bir marta tahrirlaymiz (Telegram limiti uchun)
_last_refresh: dict[int, float] = {}
_pending_refresh: set[int] = set()
_bg_tasks: set = set()


async def build_announcement_markup(bot: Bot, anime):
    """Kanal e'lonidagi '🎬 Kinoni ko'rish' tugmasi (botga deep-link)."""
    bot_info = await bot.get_me()
    return kb.InlineKeyboardMarkup(inline_keyboard=[[
        kb.InlineKeyboardButton(
            text="🎬 Kinoni ko'rish",
            url=f"https://t.me/{bot_info.username}?start=anime_{anime['anime_code']}",
        )
    ]])


async def refresh_channel_post(bot: Bot, db: Database, anime_id: int) -> None:
    """Kanaldagi e'lon matnini (reytingni) yangi holatga keltiradi. Xatolik bo'lsa bot to'xtamaydi."""
    try:
        anime = await db.get_anime(anime_id)
        if not anime or not anime["channel_message_id"]:
            return  # bu anime e'loni kanalga yuborilmagan yoki xabar ID'si saqlanmagan (eski e'lon)
        last = _last_refresh.get(anime_id)
        if last is not None:
            wait = _REFRESH_COOLDOWN - (time.monotonic() - last)
            if wait > 0:
                if anime_id in _pending_refresh:
                    return  # allaqachon kutayotgan yangilash bor — u eng oxirgi reytingni oladi
                _pending_refresh.add(anime_id)
                try:
                    await asyncio.sleep(wait)
                finally:
                    _pending_refresh.discard(anime_id)
        _last_refresh[anime_id] = time.monotonic()

        anime = await db.get_anime(anime_id)  # kutish paytida o'zgargan bo'lishi mumkin
        if not anime or not anime["channel_message_id"]:
            return
        caption = await build_anime_card_text(db, anime)
        markup = await build_announcement_markup(bot, anime)
        try:
            if anime["poster_file_id"]:
                await bot.edit_message_caption(
                    chat_id=config.MAIN_CHANNEL, message_id=anime["channel_message_id"],
                    caption=caption, reply_markup=markup,
                )
            else:
                await bot.edit_message_text(
                    caption, chat_id=config.MAIN_CHANNEL, message_id=anime["channel_message_id"],
                    reply_markup=markup,
                )
        except TelegramBadRequest as e:
            if "not modified" in str(e).lower():
                return
            raise
    except Exception:
        _log.exception("Kanal e'lonini yangilashda xatolik (anime_id=%s)", anime_id)


def schedule_channel_post_refresh(bot: Bot, db: Database, anime_id: int) -> None:
    """Foydalanuvchiga javobni kechiktirmasdan, e'lonni fonda yangilaydi."""
    task = asyncio.create_task(refresh_channel_post(bot, db, anime_id))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


# ------------------------------------------------------------------ #
# Admin: anime tafsilot matni, vaqt formati
# ------------------------------------------------------------------ #
async def build_admin_anime_text(db: Database, anime_id: int) -> Optional[str]:
    """Admin uchun kino tafsilot matni (admin_anime_detail va tahrirlashdan keyin ishlatiladi)."""
    anime = await db.get_anime(anime_id)
    if not anime:
        return None
    genres = await db.get_anime_genres(anime_id)
    genre_txt = ", ".join(f"{g['emoji']} {g['name']}" for g in genres) or "—"
    ep_count = await db.count_episodes(anime_id)
    seasons = await db.list_seasons(anime_id)
    season_line = f"📁 Fasllar: {len(seasons)}\n" if seasons else ""
    extra = []
    if anime["country"]:
        extra.append(f"🌍 Davlat: {anime['country']}")
    if anime["release_year"]:
        extra.append(f"📅 Yil: {anime['release_year']}")
    if anime["language"]:
        extra.append(f"🎙 Til: {anime['language']}")
    if "subtitles" in anime.keys() and anime["subtitles"]:
        extra.append(f"💬 Subtitr: {anime['subtitles']}")
    for label, field in (
        ("🎬 Rejissyor", "director"), ("👥 Aktyorlar", "actors"), ("⏱ Davomiylik", "duration"),
        ("🔞 Yosh", "age_rating"), ("🌟 TMDB reyting", "imdb_rating"), ("📀 Sifat", "quality"),
        ("🎞 Treyler", "trailer_url"),
    ):
        if field in anime.keys() and anime[field]:
            extra.append(f"{label}: {anime[field]}")
    if "kind" in anime.keys():
        extra.append("📺 Turi: " + ("serial" if anime["kind"] == "series" else "kino"))
    versions = await db.list_versions(anime_id)
    if versions:
        extra.append(f"🎙 Variantlar (til/sifat): {len(versions)} ta")
    extra_txt = ("\n".join(extra) + "\n") if extra else ""
    poster_txt = "🖼 Poster: bor\n" if anime["poster_file_id"] else "🖼 Poster: yo'q\n"
    return (
        f"🎬 <b>{anime['title']}</b>\n"
        f"🆔 ID: <code>{anime['anime_code']}</code>\n"
        f"🎭 Janr: {genre_txt}\n"
        f"{extra_txt}"
        f"{poster_txt}"
        f"{season_line}"
        f"📼 Qismlar: {ep_count}\n"
        f"{'💎 VIP' if anime['is_vip'] else '🆓 Oddiy'}\n\n"
        f"{anime['description'] or ''}"
    )


def parse_hhmm(text: str) -> Optional[str]:
    """'9:30', '09.30', '21:05' -> '09:30' / '21:05'. Noto'g'ri bo'lsa None."""
    import re as _re

    m = _re.fullmatch(r"\s*(\d{1,2})\s*[:.]\s*(\d{2})\s*", text or "")
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return None
    return f"{h:02d}:{mi:02d}"
