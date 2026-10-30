"""
🎯 Kashf qilish (foydalanuvchi paneli):
  * kayfiyat tanlanadi (😂 😭 🔥 💕 😱 🎲) -> bot mos kino kartasini ko'rsatadi
  * ❤️ Ko'raman  -> «📌 Keyin ko'raman» ro'yxatiga tushadi, shu janrlar ko'proq chiqadi
  * 👎 Qiziq emas -> shu janrlar kamroq chiqadi, kino qayta ko'rsatilmaydi
  * ⏭ Keyingisi  -> shunchaki o'tkazib yuboradi
  * 🎬 Ko'rish    -> oddiy kino sahifasi (VIP tekshiruvi o'sha yerda)
Tanlash mantig'i database.next_discover_anime da.
"""
from __future__ import annotations

import html
import logging
import math
import re

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import config
import keyboards as kb
from database import Database
from handlers.common import has_vip_access
from handlers.user import show_anime_detail

logger = logging.getLogger("anime_bot.discover")
router = Router(name="discover")

LIKED_PER_PAGE = 8
MENU_TEXT = (
    "🎯 <b>Kashf qilish</b>\n\n"
    "Bugun kayfiyatingiz qanday? Tanlang, men sizga mos kinolarni ko'rsataman.\n\n"
    "❤️ bosganlaringiz «📌 Keyin ko'raman» ro'yxatiga tushadi va shunga o'xshashlar ko'proq chiqadi, "
    "👎 bosganlaringizga o'xshashlar esa kamroq."
)


def _mood(key: str):
    for k, emoji, label, genres in config.MOODS:
        if k == key:
            return emoji, label, genres
    return None


async def build_discover_caption(db: Database, anime) -> str:
    genres = ", ".join(g["name"] for g in await db.get_anime_genres(anime["id"])) or "—"
    avg, cnt = await db.anime_avg_rating(anime["id"])
    eps = await db.count_episodes(anime["id"])
    seasons = await db.list_seasons(anime["id"])
    title = f"🎬 <b>{anime['title']}</b>" + (" 💎" if anime["is_vip"] else "")
    lines = [title, f"🎭 {genres}"]
    meta = []
    if anime["release_year"]:
        meta.append(f"📅 {anime['release_year']}")
    if anime["country"]:
        meta.append(f"🌍 {anime['country']}")
    if meta:
        lines.append(" • ".join(meta))
    if seasons:
        lines.append(f"🎬 {len(seasons)} fasl • {eps} qism")
    elif eps == 1:
        lines.append("🎬 Film")
    elif eps > 1:
        lines.append(f"🎬 {eps} qism")
    lines.append(f"⭐ Reyting: {avg}/10" if cnt else "⭐ Reyting: hali baholanmagan")
    desc = re.sub(r"<[^>]*>", "", anime["description"] or "").strip()
    if desc:
        if len(desc) > 300:
            desc = desc[:299].rstrip() + "…"
        lines += ["", f"📖 {html.escape(desc, quote=False)}"]
    return "\n".join(lines)


async def _safe_delete(message: Message) -> None:
    try:
        await message.delete()
    except TelegramBadRequest:
        pass


async def _send_menu(bot: Bot, chat_id: int, db: Database, telegram_id: int) -> None:
    liked = await db.count_discover_liked(telegram_id)
    await bot.send_message(chat_id, MENU_TEXT, reply_markup=kb.discover_menu_keyboard(liked))


async def _send_next_card(bot: Bot, chat_id: int, db: Database, telegram_id: int, mood_key: str) -> None:
    mood = _mood(mood_key)
    if not mood:
        await _send_menu(bot, chat_id, db, telegram_id)
        return
    emoji, label, genres = mood
    anime = await db.next_discover_anime(telegram_id, genres, include_vip=await has_vip_access(db, telegram_id))
    if not anime:
        liked = await db.count_discover_liked(telegram_id)
        await bot.send_message(
            chat_id,
            f"{emoji} <b>{label}</b> uchun hozircha ko'rsatadigan yangi kino qolmadi 🎉\n\n"
            "«Qaytadan boshlash» bosilsa, o'tkazib yuborgan kinolaringiz yana chiqadi. "
            "Yoki boshqa kayfiyatni tanlang.",
            reply_markup=kb.discover_empty_keyboard(mood_key, liked),
        )
        return
    caption = await build_discover_caption(db, anime)
    markup = kb.discover_card_keyboard(anime["id"], mood_key)
    if anime["poster_file_id"]:
        try:
            await bot.send_photo(chat_id, anime["poster_file_id"], caption=caption, reply_markup=markup)
            return
        except TelegramBadRequest:
            pass  # poster yaroqsiz bo'lsa, matn ko'rinishida yuboramiz
    await bot.send_message(chat_id, caption, reply_markup=markup)


# ---------------------------------------------------------------- menyu
@router.message(F.text == "🎯 Kashf qilish")
async def discover_open_text(message: Message, db: Database, bot: Bot, state: FSMContext):
    await state.clear()
    await _send_menu(bot, message.chat.id, db, message.from_user.id)


@router.callback_query(F.data == "disc:menu")
async def discover_open_cb(call: CallbackQuery, db: Database, bot: Bot):
    await call.answer()
    await _safe_delete(call.message)
    await _send_menu(bot, call.message.chat.id, db, call.from_user.id)


@router.callback_query(F.data.startswith("disc:go:"))
async def discover_go(call: CallbackQuery, db: Database, bot: Bot):
    mood_key = call.data.split(":")[2]
    await call.answer()
    await _safe_delete(call.message)
    await _send_next_card(bot, call.message.chat.id, db, call.from_user.id, mood_key)


# ---------------------------------------------------------------- kartadagi tugmalar
_ACTIONS = {"like": "like", "dis": "dislike", "skip": "skip"}


@router.callback_query(F.data.regexp(r"^disc:(like|dis|skip):\d+:\w+$"))
async def discover_action(call: CallbackQuery, db: Database, bot: Bot):
    _, act, anime_id_s, mood_key = call.data.split(":")
    anime_id = int(anime_id_s)
    is_new = await db.record_discover_action(call.from_user.id, anime_id, _ACTIONS[act])
    if not is_new:
        # shu karta uchun harakat allaqachon bajarilgan (ikki marta bosildi) — yangi karta yuborilmaydi
        await call.answer()
        return
    await call.answer("❤️ Ro'yxatga qo'shildi" if act == "like" else None)
    await _safe_delete(call.message)
    await _send_next_card(bot, call.message.chat.id, db, call.from_user.id, mood_key)


@router.callback_query(F.data.startswith("disc:watch:"))
async def discover_watch(call: CallbackQuery, db: Database):
    anime_id = int(call.data.split(":")[2])
    await call.answer()
    await show_anime_detail(call.message, db, anime_id, call.from_user.id)


@router.callback_query(F.data.startswith("disc:reset:"))
async def discover_reset(call: CallbackQuery, db: Database, bot: Bot):
    mood_key = call.data.split(":")[2]
    await db.reset_discover(call.from_user.id)
    await call.answer("🔄 Qaytadan boshlandi")
    await _safe_delete(call.message)
    await _send_next_card(bot, call.message.chat.id, db, call.from_user.id, mood_key)


# ---------------------------------------------------------------- «Keyin ko'raman»
async def _liked_screen(db: Database, telegram_id: int, page: int):
    total = await db.count_discover_liked(telegram_id)
    pages = max(1, math.ceil(total / LIKED_PER_PAGE))
    page = min(max(page, 0), pages - 1)
    animes = await db.list_discover_liked(telegram_id, LIKED_PER_PAGE, page * LIKED_PER_PAGE)
    if total:
        text = f"📌 <b>Keyin ko'raman</b> ({total} ta)\n\nKo'rmoqchi bo'lgan kinoni tanlang:"
    else:
        text = "📌 <b>Keyin ko'raman</b>\n\nHali hech narsa yo'q. Kashf qilishda ❤️ bosgan kinolaringiz shu yerga tushadi."
    return text, kb.discover_liked_keyboard(animes, page, pages)


@router.callback_query(F.data.startswith("disc:liked:"))
async def discover_liked(call: CallbackQuery, db: Database):
    page = int(call.data.split(":")[2])
    text, markup = await _liked_screen(db, call.from_user.id, page)
    try:
        await call.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        # xabar rasmli bo'lsa (karta) tahrirlab bo'lmaydi — o'chirib, yangisini yuboramiz
        await _safe_delete(call.message)
        await call.message.answer(text, reply_markup=markup)
    await call.answer()


@router.callback_query(F.data == "disc:clear")
async def discover_clear_ask(call: CallbackQuery):
    try:
        await call.message.edit_text(
            "🧹 «Keyin ko'raman» ro'yxatini butunlay tozalaysizmi?",
            reply_markup=kb.discover_clear_confirm_keyboard(),
        )
    except TelegramBadRequest:
        pass
    await call.answer()


@router.callback_query(F.data == "disc:clearok")
async def discover_clear_apply(call: CallbackQuery, db: Database):
    await db.clear_discover_likes(call.from_user.id)
    text, markup = await _liked_screen(db, call.from_user.id, 0)
    try:
        await call.message.edit_text("✅ Ro'yxat tozalandi.\n\n" + text, reply_markup=markup)
    except TelegramBadRequest:
        pass
    await call.answer("Tozalandi")
