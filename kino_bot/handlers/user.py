from __future__ import annotations

import datetime
import logging
import math
import random

from aiogram import Bot, F, Router
from aiogram.filters import CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.exceptions import TelegramBadRequest

import archive
import funnel
import keyboards as kb
from database import Database
from handlers.common import (
    build_anime_card_text,
    build_subscribe_prompt,
    ensure_user,
    fmt_number,
    format_filler_ranges,
    has_vip_access,
    is_subscribed_to_all,
    schedule_channel_post_refresh,
)
from referral import process_referral_reward, register_from_payload
from states import ReportStates, RequestStates, SearchStates, VipPaymentStates

logger = logging.getLogger("anime_bot.user")
router = Router(name="user")

PER_PAGE = 10


async def _single_episode_id(db: Database, anime_id: int, has_seasons: bool) -> int | None:
    """Agar kino fasllarsiz va atigi 1 qismdan iborat bo'lsa (film), o'sha qismning ID'sini qaytaradi."""
    if has_seasons:
        return None
    ep_count = await db.count_episodes(anime_id)
    if ep_count != 1:
        return None
    eps = await db.list_episodes_page(anime_id, 0, None)
    return eps[0]["id"] if eps and not eps[0]["is_filler"] else None


async def _vip_free_banner(db: Database) -> str:
    """Agar hozir vaqtinchalik 'VIP hammaga bepul' rejimi faol bo'lsa, banner matnini qaytaradi."""
    until = await db.get_vip_free_until()
    if until and until > datetime.datetime.utcnow():
        return (
            f"🎁 <b>E'lon:</b> hozirda VIP kinolarni ham BEPUL tomosha qilishingiz mumkin! "
            f"({until.strftime('%d.%m.%Y')} kungacha)\n\n"
        )
    return ""


# ------------------------------------------------------------------ #
# /start
# ------------------------------------------------------------------ #
@router.message(CommandStart())
async def cmd_start(message: Message, db: Database, state: FSMContext):
    await state.clear()
    is_new_user = await db.get_user(message.from_user.id) is None
    user = await ensure_user(db, message.from_user)

    args = message.text.split(maxsplit=1)
    payload = args[1].strip() if len(args) > 1 else ""
    # Referal havola: https://t.me/<bot>?start=ref_<taklif qiluvchi ID>. Faqat YANGI foydalanuvchi hisoblanadi
    # va taklif obuna tekshiruvidan OLDIN yoziladi (majburiy obuna oynasida havola yo'qolmasligi uchun).
    if is_new_user and payload.startswith("ref_"):
        await register_from_payload(db, message.from_user.id, payload)

    missing = await is_subscribed_to_all(message.bot, db, message.from_user.id)
    if missing:
        text, markup = build_subscribe_prompt(missing)
        await message.answer(text, reply_markup=markup)
        return

    # Obunadan o'tdi — referal mukofoti (agar taklif orqali kelgan bo'lsa) shu yerda beriladi
    await process_referral_reward(message.bot, db, message.from_user.id)
    # Yangi foydalanuvchiga bir martalik bepul VIP sinov (admin paneldan sozlanadi)
    await funnel.maybe_grant_trial(message.bot, db, message.from_user.id)

    # Kanaldagi "▶️ Animeni ko'rish" tugmasi orqali kirilgan bo'lishi mumkin:
    # https://t.me/<bot>?start=anime_<code>
    if payload.startswith("anime_"):
        code = payload[len("anime_"):]
        anime = await db.get_anime_by_code(code)
        if anime:
            await _send_start_message(message, db, is_admin=bool(user["is_admin"]))
            await show_anime_detail(message, db, anime["id"], message.from_user.id)
            return

    await _send_start_message(message, db, is_admin=bool(user["is_admin"]))
    await _maybe_show_continue_watching(message, db, message.from_user.id)


async def _maybe_show_continue_watching(message: Message, db: Database, telegram_id: int) -> None:
    """Agar foydalanuvchi ilgari biror kino ko'rgan bo'lsa, davom ettirish taklifini ko'rsatadi."""
    progress = await db.get_continue_watching(telegram_id)
    if not progress:
        return
    season_txt = f" {progress['season_number']}-fasl," if progress["season_number"] else ""
    text = (
        f"▶️ <b>Ko'rishni davom ettirish</b>\n\n"
        f"🎬 {progress['title']} —{season_txt} {progress['episode_number']}-qismdan keyin"
    )
    await message.answer(text, reply_markup=kb.continue_watching_keyboard(progress["id"]))


@router.callback_query(F.data == "noop")
async def noop(call: CallbackQuery):
    await call.answer()


async def _send_start_message(message: Message, db: Database, is_admin: bool):
    text = await db.get_setting("start_text")
    photo = await db.get_setting("start_photo_file_id")
    markup = kb.main_menu(is_admin=is_admin)
    if photo:
        try:
            await message.answer_photo(photo, caption=text, reply_markup=markup)
            return
        except TelegramBadRequest:
            pass
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "check_subs")
async def check_subs(call: CallbackQuery, db: Database):
    missing = await is_subscribed_to_all(call.bot, db, call.from_user.id)
    if missing:
        await call.answer("❗ Hali barcha kanallarga obuna bo'lmadingiz.", show_alert=True)
        return
    user = await ensure_user(db, call.from_user)
    await process_referral_reward(call.bot, db, call.from_user.id)
    await funnel.maybe_grant_trial(call.bot, db, call.from_user.id)
    await call.message.delete()
    await _send_start_message(call.message, db, is_admin=bool(user["is_admin"]))
    await _maybe_show_continue_watching(call.message, db, call.from_user.id)
    await call.answer("✅ Obuna tasdiqlandi!")


@router.message(F.text == "👤 Foydalanuvchi paneliga qaytish")
async def back_to_user_panel(message: Message, db: Database):
    user = await ensure_user(db, message.from_user)
    await message.answer("Bosh menyu:", reply_markup=kb.main_menu(is_admin=bool(user["is_admin"])))


# ------------------------------------------------------------------ #
# Qidiruv
# ------------------------------------------------------------------ #
@router.message(F.text == "🔎 Kino qidirish")
async def search_entry(message: Message, state: FSMContext, db: Database):
    await state.clear()
    banner = await _vip_free_banner(db)
    await message.answer(f"{banner}Qidiruv turini tanlang:", reply_markup=kb.search_menu())


@router.callback_query(F.data == "search:title")
async def search_by_title(call: CallbackQuery, state: FSMContext):
    await state.set_state(SearchStates.waiting_title)
    await call.message.answer("🔤 Kino nomini kiriting:", reply_markup=kb.cancel_menu())
    await call.answer()


@router.callback_query(F.data == "search:code")
async def search_by_code(call: CallbackQuery, state: FSMContext):
    await state.set_state(SearchStates.waiting_code)
    await call.message.answer("🆔 Kino ID (kod) ni kiriting:", reply_markup=kb.cancel_menu())
    await call.answer()


@router.callback_query(F.data == "search:top10")
async def search_top10(call: CallbackQuery, db: Database):
    rows = await db.list_top_rated(limit=10)
    if not rows:
        await call.answer("Hozircha baholangan kino yo'q.", show_alert=True)
        return
    lines = ["⭐ <b>Eng yuqori baholangan 10 ta kino:</b>\n"]
    for i, a in enumerate(rows, start=1):
        lines.append(f"{i}. {a['title']} — {a['avg_score']:.1f}⭐ ({a['rate_count']} baho)")
    await call.message.answer("\n".join(lines), reply_markup=kb.anime_list_keyboard(rows, 0, 1, "top10"))
    await call.answer()


@router.message(F.text == "❌ Bekor qilish")
async def cancel_any(message: Message, db: Database, state: FSMContext):
    await state.clear()
    user = await ensure_user(db, message.from_user)
    await message.answer("Bekor qilindi.", reply_markup=kb.main_menu(is_admin=bool(user["is_admin"])))


@router.message(SearchStates.waiting_title)
async def do_search_title(message: Message, db: Database, state: FSMContext):
    query = message.text.strip()
    await state.update_data(search_query=query)
    await state.clear()
    rows = await db.search_anime_by_title(query, limit=PER_PAGE)
    if not rows:
        await message.answer("😕 Hech narsa topilmadi.", reply_markup=kb.main_menu())
        return
    await message.answer(
        f"🔎 «{query}» bo'yicha natijalar:",
        reply_markup=kb.anime_list_keyboard(rows, 0, 1, "search_title"),
    )


@router.message(SearchStates.waiting_code)
async def do_search_code(message: Message, db: Database, state: FSMContext):
    code = message.text.strip()
    await state.clear()
    anime = await db.get_anime_by_code(code)
    if not anime:
        await message.answer("😕 Bunday ID bilan kino topilmadi.", reply_markup=kb.main_menu())
        return
    await show_anime_detail(message, db, anime["id"], message.from_user.id)


# ------------------------------------------------------------------ #
# Katalog / Yangi / Mashhur / Janrlar
# ------------------------------------------------------------------ #
@router.message(F.text == "🆕 Yangi kinolar")
async def new_anime(message: Message, db: Database):
    rows = await db.list_new_anime(page=0, per_page=PER_PAGE)
    if not rows:
        await message.answer("Hozircha kino qo'shilmagan.")
        return
    total_pages = max(1, math.ceil(await db.count_published_anime() / PER_PAGE))
    await message.answer("🆕 Yangi qo'shilgan kinolar:",
                          reply_markup=kb.anime_list_keyboard(rows, 0, total_pages, "new"))


@router.message(F.text == "🔥 Mashhur kinolar")
async def popular_anime(message: Message, db: Database):
    rows = await db.list_popular_anime(page=0, per_page=PER_PAGE)
    if not rows:
        await message.answer("Hozircha kino qo'shilmagan.")
        return
    total_pages = max(1, math.ceil(await db.count_published_anime() / PER_PAGE))
    await message.answer("🔥 Mashhur kinolar:",
                          reply_markup=kb.anime_list_keyboard(rows, 0, total_pages, "popular"))


@router.message(F.text == "🔥 Yangi qismlar")
async def recent_episodes(message: Message, db: Database, telegram_id: int | None = None):
    """Oldindan mavjud kinolarga yaqinda qo'shilgan qismlar (🆕 Yangi kinolardan farqli)."""
    telegram_id = telegram_id or message.from_user.id
    rows = await db.list_recent_episodes(limit=50)
    viewer_has_vip = await has_vip_access(db, telegram_id)
    visible = [r for r in rows if viewer_has_vip or not r["anime_is_vip"]]
    visible = visible[:PER_PAGE]
    if not visible:
        await message.answer("Hozircha yangi qism qo'shilmagan.")
        return
    await message.answer("🔥 Yaqinda qo'shilgan qismlar:", reply_markup=kb.recent_episodes_keyboard(visible))


@router.message(F.text == "🎲 Tasodifiy kino")
async def random_anime(message: Message, db: Database, telegram_id: int | None = None):
    """
    MUHIM: bu funksiya (random tanlash/VIP filtrlash mantig'i) o'zgartirilmagan —
    faqat qayerdan chaqirilishi o'zgardi: endi asosiy menyuda alohida tugma emas,
    balki "🎬 Kino ko'rish" markazi ichida (va eski matnli buyruq ham ishlayveradi).
    """
    telegram_id = telegram_id or message.from_user.id
    rows = await db.all_anime_for_random()
    if not rows:
        await message.answer("Hozircha kino qo'shilmagan.")
        return
    viewer_has_vip = await has_vip_access(db, telegram_id)
    candidates = [a for a in rows if viewer_has_vip or not a["is_vip"]]
    if not candidates:
        candidates = rows  # hammasi VIP bo'lsa ham ko'rsatamiz — anime sahifasi o'zi bloklaydi
    anime = random.choice(candidates)
    await message.answer("🎲 Bugungi tasodifiy kino:")
    await show_anime_detail(message, db, anime["id"], telegram_id)


# ------------------------------------------------------------------ #
# "🎬 Anime ko'rish" markazi — mavjud brauzing funksiyalarini (qayta
# yozmasdan) bitta joydan ochish uchun. Har bir tugma MAVJUD funksiyani
# chaqiradi, yangi parallel mantiq yaratilmaydi.
# ------------------------------------------------------------------ #
@router.message(F.text == "🎬 Kino ko'rish")
async def anime_browse_hub(message: Message):
    await message.answer("🎬 Kino ko'rish:", reply_markup=kb.anime_browse_hub_keyboard())


@router.callback_query(F.data == "browse:hub")
async def browse_hub_cb(call: CallbackQuery):
    await call.answer()
    await call.message.answer("🎬 Kino ko'rish:", reply_markup=kb.anime_browse_hub_keyboard())


@router.callback_query(F.data == "browse:search")
async def browse_search(call: CallbackQuery, state: FSMContext, db: Database):
    await call.answer()
    await search_entry(call.message, state, db)


@router.callback_query(F.data == "browse:catalog")
async def browse_catalog(call: CallbackQuery, db: Database):
    await call.answer()
    await catalog(call.message, db)


@router.callback_query(F.data == "browse:genres")
async def browse_genres(call: CallbackQuery, db: Database):
    await call.answer()
    await genres_list(call.message, db)


@router.callback_query(F.data == "browse:new")
async def browse_new(call: CallbackQuery, db: Database):
    await call.answer()
    await new_anime(call.message, db)


@router.callback_query(F.data == "browse:popular")
async def browse_popular(call: CallbackQuery, db: Database):
    await call.answer()
    await popular_anime(call.message, db)


@router.callback_query(F.data == "browse:recent_episodes")
async def browse_recent_episodes(call: CallbackQuery, db: Database):
    await call.answer()
    await recent_episodes(call.message, db, telegram_id=call.from_user.id)


@router.callback_query(F.data == "browse:random")
async def browse_random(call: CallbackQuery, db: Database):
    await call.answer()
    await random_anime(call.message, db, telegram_id=call.from_user.id)


@router.message(F.text == "📚 Katalog")
async def catalog(message: Message, db: Database):
    genres = await db.list_genres()
    banner = await _vip_free_banner(db)
    await message.answer(f"{banner}📚 Janr bo'yicha katalog. Janrni tanlang:", reply_markup=kb.genres_menu(genres))


@router.message(F.text == "🎭 Janrlar")
async def genres_list(message: Message, db: Database):
    genres = await db.list_genres()
    banner = await _vip_free_banner(db)
    await message.answer(f"{banner}🎭 Janrni tanlang:", reply_markup=kb.genres_menu(genres))


@router.callback_query(F.data.startswith("search_genre:"))
async def genre_selected(call: CallbackQuery, db: Database, state: FSMContext):
    genre_id = int(call.data.split(":")[1])
    genre = await db.get_genre(genre_id)
    if not genre:
        await call.answer("Janr topilmadi.", show_alert=True)
        return
    if genre["is_vip"] and not await has_vip_access(db, call.from_user.id):
        await call.answer(
            "💎 Bu VIP janr. Ko'rish uchun VIP obuna kerak (👤 Profil → 💎 VIP olish).",
            show_alert=True,
        )
        return
    await state.update_data(genre_id=genre_id)
    rows = await db.list_anime_by_genre(genre_id, page=0, per_page=PER_PAGE)
    if not rows:
        await call.answer("Bu janrda hali kino yo'q.", show_alert=True)
        return
    total_pages = max(1, math.ceil(await db.count_anime_by_genre(genre_id) / PER_PAGE))
    await call.message.answer(
        f"{genre['emoji']} <b>{genre['name']}</b> janridagi kinolar:",
        reply_markup=kb.anime_list_keyboard(rows, 0, total_pages, "genre"),
    )
    await call.answer()


@router.callback_query(F.data.startswith("list:"))
async def list_pagination(call: CallbackQuery, db: Database, state: FSMContext):
    _, kind, page_str = call.data.split(":")
    page = int(page_str)
    per_page = PER_PAGE

    if kind == "new":
        rows = await db.list_new_anime(page=page, per_page=per_page)
        title = "🆕 Yangi qo'shilgan kinolar:"
        total_pages = max(1, math.ceil(await db.count_published_anime() / per_page))
    elif kind == "popular":
        rows = await db.list_popular_anime(page=page, per_page=per_page)
        title = "🔥 Mashhur kinolar:"
        total_pages = max(1, math.ceil(await db.count_published_anime() / per_page))
    elif kind == "genre":
        data = await state.get_data()
        genre_id = data.get("genre_id")
        genre = await db.get_genre(genre_id) if genre_id else None
        rows = await db.list_anime_by_genre(genre_id, page=page, per_page=per_page) if genre_id else []
        title = f"{genre['emoji']} {genre['name']} janridagi kinolar:" if genre else "Natijalar:"
        total_pages = max(1, math.ceil((await db.count_anime_by_genre(genre_id) if genre_id else 0) / per_page))
    elif kind == "col":
        data = await state.get_data()
        col_id = data.get("collection_id")
        col = await db.get_collection(col_id) if col_id else None
        rows = await db.collection_items(col_id, page=page, per_page=per_page) if col_id else []
        title = f"{col['emoji']} <b>{col['title']}</b>:" if col else "Natijalar:"
        total_pages = max(1, math.ceil((await db.count_collection_items(col_id) if col_id else 0) / per_page))
    elif kind == "favorites":
        all_rows = await db.list_favorites(call.from_user.id)
        rows = all_rows[page * per_page:(page + 1) * per_page]
        title = "❤️ Sevimli kinolaringiz:"
        total_pages = max(1, math.ceil(len(all_rows) / per_page))
    elif kind == "search_title":
        data = await state.get_data()
        query = data.get("search_query", "")
        all_rows = await db.search_anime_by_title(query, limit=200)
        rows = all_rows[page * per_page:(page + 1) * per_page]
        title = f"🔎 «{query}» bo'yicha natijalar:"
        total_pages = max(1, math.ceil(len(all_rows) / per_page))
    else:
        rows = []
        title = "Natijalar:"
        total_pages = 1

    if not rows and page > 0:
        await call.answer("Boshqa natija yo'q.", show_alert=True)
        return

    try:
        await call.message.edit_text(title, reply_markup=kb.anime_list_keyboard(rows, page, total_pages, kind))
    except TelegramBadRequest:
        await call.message.answer(title, reply_markup=kb.anime_list_keyboard(rows, page, total_pages, kind))
    await call.answer()


# ------------------------------------------------------------------ #
# Anime detali
# ------------------------------------------------------------------ #
async def _detail_markup(db: Database, anime, viewer_telegram_id: int, locked: bool = False):
    """Kino sahifasi tugmalari (sahifa ochilganda ham, sevimli/kuzatish bosilganda ham bir xil)."""
    anime_id = anime["id"]
    keys = anime.keys()
    is_fav = await db.is_favorite(anime_id, viewer_telegram_id)
    is_following = await db.is_following(anime_id, viewer_telegram_id)
    seasons = await db.list_seasons(anime_id)
    versions = await db.list_versions(anime_id)
    ep_count = await db.count_episodes(anime_id)
    single_episode_id = await _single_episode_id(db, anime_id, bool(seasons))
    trailer = (anime["trailer_url"] if "trailer_url" in keys else None) or None
    if locked and await db.get_setting("preview_enabled", "1") != "1":
        trailer = None
    is_series = ("kind" in keys and anime["kind"] == "series")
    return kb.anime_detail_keyboard(
        anime_id, is_fav, is_following, bool(seasons), single_episode_id,
        locked=locked,
        has_versions=bool(versions),
        trailer_url=trailer,
        show_follow=bool(seasons) or ep_count > 1 or is_series,
    )


async def show_anime_detail(message: Message, db: Database, anime_id: int, viewer_telegram_id: int, edit: bool = False):
    anime = await db.get_anime(anime_id)
    if not anime:
        await message.answer("Bu kino topilmadi (o'chirilgan bo'lishi mumkin).")
        return

    # VIP kino, VIP bo'lmagan foydalanuvchi: karta (poster, tavsif, treyler) ko'rinadi, video qulflangan
    locked = bool(anime["is_vip"]) and not await has_vip_access(db, viewer_telegram_id)

    caption = await build_anime_card_text(db, anime, locked=locked)
    if not locked:
        quota = await funnel.free_quota_line(db, viewer_telegram_id)
        if quota:
            caption += f"\n\n{quota}"
    markup = await _detail_markup(db, anime, viewer_telegram_id, locked=locked)
    if anime["poster_file_id"]:
        try:
            await message.answer_photo(anime["poster_file_id"], caption=caption, reply_markup=markup)
            return
        except TelegramBadRequest:
            pass  # poster yaroqsiz bo'lsa (token almashgan va h.k.) — matn bilan davom etamiz
    await message.answer(caption, reply_markup=markup)


@router.callback_query(F.data.startswith("seasons:"))
async def show_seasons(call: CallbackQuery, db: Database):
    anime_id = int(call.data.split(":")[1])
    anime = await db.get_anime(anime_id)
    if not anime:
        await call.answer("Kino topilmadi.", show_alert=True)
        return
    if anime["is_vip"] and not await has_vip_access(db, call.from_user.id):
        await call.answer("💎 Bu VIP kino. Ko'rish uchun VIP kerak.", show_alert=True)
        return
    seasons = await db.list_seasons(anime_id)
    if not seasons:
        await call.answer("Fasllar topilmadi.", show_alert=True)
        return
    text = f"🎬 <b>{anime['title']}</b>\nFaslni tanlang:"
    markup = kb.seasons_keyboard(anime_id, seasons)
    try:
        await call.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        await call.message.answer(text, reply_markup=markup)
    await call.answer()


@router.callback_query(F.data.startswith("follow:"))
async def toggle_follow_cb(call: CallbackQuery, db: Database):
    anime_id = int(call.data.split(":")[1])
    following = await db.toggle_follow(anime_id, call.from_user.id)
    await call.answer("🔔 Kuzatishga qo'shildingiz." if following else "🔕 Kuzatish bekor qilindi.")
    await _refresh_detail_markup(call, db, anime_id)


async def _refresh_detail_markup(call: CallbackQuery, db: Database, anime_id: int) -> None:
    anime = await db.get_anime(anime_id)
    if not anime:
        return
    locked = bool(anime["is_vip"]) and not await has_vip_access(db, call.from_user.id)
    try:
        await call.message.edit_reply_markup(reply_markup=await _detail_markup(db, anime, call.from_user.id, locked))
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.startswith("anime:"))
async def anime_detail_cb(call: CallbackQuery, db: Database):
    anime_id = int(call.data.split(":")[1])
    await show_anime_detail(call.message, db, anime_id, call.from_user.id)
    await call.answer()


# ------------------------------------------------------------------ #
# Sevimlilar
# ------------------------------------------------------------------ #
@router.callback_query(F.data.startswith("fav:"))
async def toggle_fav(call: CallbackQuery, db: Database):
    anime_id = int(call.data.split(":")[1])
    added = await db.toggle_favorite(anime_id, call.from_user.id)
    await call.answer("❤️ Sevimlilarga qo'shildi!" if added else "💔 Sevimlilardan olib tashlandi.")
    await _refresh_detail_markup(call, db, anime_id)


@router.message(F.text == "❤️ Sevimlilar")
async def favorites_menu(message: Message, db: Database):
    rows = await db.list_favorites(message.from_user.id)
    if not rows:
        await message.answer("❤️ Sevimlilar ro'yxati hozircha bo'sh.")
        return
    page_rows = rows[:PER_PAGE]
    total_pages = max(1, math.ceil(len(rows) / PER_PAGE))
    await message.answer(
        "❤️ Sevimli kinolaringiz:",
        reply_markup=kb.anime_list_keyboard(page_rows, 0, total_pages, "favorites"),
    )


# ------------------------------------------------------------------ #
# Ko'rish tarixi
# ------------------------------------------------------------------ #
@router.message(F.text == "🕐 Ko'rish tarixi")
async def history_menu(message: Message, db: Database):
    rows = await db.list_watch_history(message.from_user.id, limit=PER_PAGE)
    if not rows:
        await message.answer("🕐 Ko'rish tarixingiz hozircha bo'sh.")
        return
    lines = ["🕐 <b>Ko'rish tarixi</b>\n"]
    for r in rows:
        season_txt = f" {r['season_number']}-fasl," if r["season_number"] else ""
        lines.append(f"🎬 {r['title']} —{season_txt} {r['episode_number']}-qism")
    await message.answer("\n".join(lines), reply_markup=kb.history_keyboard(rows))


# ------------------------------------------------------------------ #
# Reyting
# ------------------------------------------------------------------ #
@router.callback_query(F.data.startswith("rate_open:"))
async def rate_open(call: CallbackQuery, db: Database):
    anime_id = int(call.data.split(":")[1])
    if await db.has_rated(anime_id, call.from_user.id):
        await call.answer("Siz bu kinoga allaqachon baho bergansiz.", show_alert=True)
        return
    await call.message.answer("Kinoni baholang:", reply_markup=kb.rating_keyboard(anime_id))
    await call.answer()


@router.callback_query(F.data.startswith("rate:"))
async def rate_submit(call: CallbackQuery, db: Database, bot: Bot):
    _, anime_id_str, score_str = call.data.split(":")
    anime_id, score = int(anime_id_str), int(score_str)
    await db.rate_anime(anime_id, call.from_user.id, score)
    # Kanaldagi e'londa ko'rsatilgan reytingni ham yangilab qo'yamiz (fonda, javobni kechiktirmaydi)
    schedule_channel_post_refresh(bot, db, anime_id)
    try:
        await call.message.edit_text(f"✅ Bahoyingiz qabul qilindi: {score}⭐")
    except TelegramBadRequest:
        await call.message.answer(f"✅ Bahoyingiz qabul qilindi: {score}⭐")
    await call.answer("Rahmat!")


# ------------------------------------------------------------------ #
# Qismlar / video ko'rish
# ------------------------------------------------------------------ #
@router.callback_query(F.data.startswith("episodes:"))
async def show_episodes(call: CallbackQuery, db: Database):
    _, anime_id_str, season_token, page_str = call.data.split(":")
    anime_id, page = int(anime_id_str), int(page_str)
    season_id = None if season_token == "0" else int(season_token)
    anime = await db.get_anime(anime_id)
    if not anime:
        await call.answer("Kino topilmadi.", show_alert=True)
        return
    if anime["is_vip"] and not await has_vip_access(db, call.from_user.id):
        await call.answer("💎 Bu VIP kino. Ko'rish uchun VIP kerak.", show_alert=True)
        return
    total = await db.count_episodes_in_season(anime_id, season_id)
    if total == 0:
        await call.answer("Hali qismlar qo'shilmagan.", show_alert=True)
        return
    episodes = await db.list_episodes_page(anime_id, page, season_id)
    season_label = ""
    if season_id:
        season = await db.get_season(season_id)
        if season:
            season_label = f" — {season['season_number']}-fasl"
    text = f"🎬 <b>{anime['title']}</b>{season_label}\nQism raqamini tanlang ({total} ta qism):"
    filler_ranges = await db.list_filler_ranges(anime_id, season_id)
    if filler_ranges:
        text += (
            "\n\n🟡 <b>Filler qismlar</b> (asosiy syujetga aloqasi yo'q, video mavjud emas):\n"
            + format_filler_ranges(filler_ranges)
        )
    markup = kb.episodes_keyboard(anime_id, episodes, page, total, season_token)
    try:
        await call.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        await call.message.answer(text, reply_markup=markup)
    await call.answer()


async def _send_episode(db: Database, bot: Bot, telegram_id: int, anime, episode) -> None:
    """Video yuborish + ko'rish tarixi/progressni saqlash + (kerak bo'lsa) baholash taklifi."""
    season_txt = ""
    season = await db.get_season(episode["season_id"]) if episode["season_id"] else None
    if season:
        season_txt = f" {season['season_number']}-fasl,"
    is_single_movie = (not season) and (await db.count_episodes(anime["id"])) == 1
    caption = f"🎬 {anime['title']}" if is_single_movie else f"🎬 {anime['title']} —{season_txt} {episode['episode_number']}-qism"
    await archive.send_episode_video(
        bot,
        telegram_id,
        episode,
        caption=caption,
        protect=bool(anime["is_vip"]),
    )
    await db.log_watch(telegram_id, anime["id"], episode["id"])
    await db.set_watch_progress(telegram_id, anime["id"], episode["id"])

    # Baholash FAQAT animening eng oxirgi qismi (barcha fasllar bo'yicha haqiqiy
    # oxirgisi, fasl oxiri emas) tomosha qilingandan keyin chiqadi.
    # get_next_episode() fasldan-faslga to'g'ri o'tadi, shuning uchun bu yerda
    # "keyingi qism yo'q" = "bu animening haqiqiy oxirgi qismi" degani.
    next_ep = await db.get_next_episode(anime["id"], episode["id"])
    is_final_episode = next_ep is None
    if is_final_episode and not await db.has_rated(anime["id"], telegram_id):
        await bot.send_message(
            telegram_id,
            "Ushbu kinoni baholashni unutmang 👇",
            reply_markup=kb.post_watch_rate_keyboard(anime["id"]),
        )


@router.callback_query(F.data.startswith("watch:"))
async def watch_episode(call: CallbackQuery, db: Database, bot: Bot):
    episode_id = int(call.data.split(":")[1])
    episode = await db.get_episode(episode_id)
    if not episode:
        await call.answer("Video topilmadi.", show_alert=True)
        return
    if episode["is_filler"]:
        await call.answer("🟡 Bu qism filler — video mavjud emas.", show_alert=True)
        return
    anime = await db.get_anime(episode["anime_id"])
    if not anime:
        await call.answer("Kino topilmadi.", show_alert=True)
        return
    allowed, reason = await funnel.check_access(db, call.from_user.id, anime)
    if not allowed:
        await call.answer()
        await funnel.send_vip_offer(bot, db, call.message.chat.id, call.from_user.id, reason, anime["title"])
        return
    await _send_episode(db, bot, call.from_user.id, anime, episode)
    await call.answer()


@router.callback_query(F.data.startswith("continue_watch:"))
async def continue_watch(call: CallbackQuery, db: Database, bot: Bot):
    anime_id = int(call.data.split(":")[1])
    anime = await db.get_anime(anime_id)
    if not anime:
        await call.answer("Kino topilmadi.", show_alert=True)
        return
    allowed, reason = await funnel.check_access(db, call.from_user.id, anime)
    if not allowed:
        await call.answer()
        await funnel.send_vip_offer(bot, db, call.message.chat.id, call.from_user.id, reason, anime["title"])
        return
    progress = await db.get_watch_progress_for_anime(call.from_user.id, anime_id)
    if not progress:
        await call.answer("Bu kino bo'yicha ko'rish tarixi topilmadi.", show_alert=True)
        return
    next_ep = await db.get_next_episode(anime_id, progress["episode_id"])
    target_ep = next_ep or await db.get_episode(progress["episode_id"])
    if not target_ep:
        await call.answer("Qism topilmadi.", show_alert=True)
        return
    await _send_episode(db, bot, call.from_user.id, anime, target_ep)
    await call.answer()


# ------------------------------------------------------------------ #
# Profil
# ------------------------------------------------------------------ #
@router.message(F.text == "👤 Profil")
async def profile(message: Message, db: Database):
    user = await ensure_user(db, message.from_user)
    is_vip = await db.is_vip(message.from_user.id)
    favs = await db.list_favorites(message.from_user.id)
    history = await db.list_watch_history(message.from_user.id, limit=1000)
    status = "💎 VIP" if is_vip else "👤 Oddiy foydalanuvchi"
    vip_until_txt = ""
    if is_vip and user["vip_until"]:
        vip_until_txt = f"\n⏳ VIP tugash sanasi: {user['vip_until'][:10]}"

    username_line = f"🔗 Username: @{user['username']}\n" if user["username"] else "🔗 Username: —\n"
    text = (
        f"👤 <b>Profil</b>\n\n"
        f"🆔 ID: <code>{user['telegram_id']}</code>\n"
        f"👨‍💼 Ism: {user['full_name']}\n"
        f"{username_line}"
    )
    text += (
        f"📊 Status: {status}{vip_until_txt}\n"
        f"💰 Balans: {fmt_number(user['balance'])} so'm\n"
        f"❤️ Sevimlilar: {len(favs)} ta\n"
        f"📚 Ko'rilganlar: {len(history)} ta"
    )
    await message.answer(text, reply_markup=kb.profile_keyboard())


# ------------------------------------------------------------------ #
# VIP sotib olish
# ------------------------------------------------------------------ #
@router.message(F.text == "💎 VIP olish")
async def vip_menu_button(message: Message, db: Database, state: FSMContext):
    """Asosiy menyudagi "💎 VIP olish" tugmasi: narxlar; VIP bo'lganlarga muddati ko'rsatilib, uzaytirish taklif qilinadi."""
    await state.clear()
    user = await ensure_user(db, message.from_user)
    if await db.is_vip(message.from_user.id) and user["vip_until"]:
        head = (
            "💎 <b>Siz VIP foydalanuvchisiz</b>\n"
            f"⏳ Tugash sanasi: {user['vip_until'][:10]}\n\n"
            "Muddatni uzaytirish (yangi muddat mavjudiga qo'shiladi):"
        )
    else:
        head = "💎 <b>VIP — cheklovlarsiz kirish</b>"
    text, markup = await funnel.vip_offer(db, message.from_user.id, head)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "vip_open")
async def vip_open(call: CallbackQuery, db: Database):
    head = "💎 <b>VIP — cheklovlarsiz kirish</b>"
    if await db.is_vip(call.from_user.id):
        head = "💎 <b>VIP muddatini uzaytirish</b>\n(yangi muddat mavjudiga qo'shiladi)"
    text, markup = await funnel.vip_offer(db, call.from_user.id, head)
    await call.message.answer(text, reply_markup=markup)
    await call.answer()


@router.callback_query(F.data.startswith("vip_buy:"))
async def vip_buy(call: CallbackQuery, db: Database, state: FSMContext):
    months = int(call.data.split(":")[1])
    prices, discount = await funnel.get_prices(db, call.from_user.id)
    price = prices.get(months, 0)
    card_number = await db.get_setting("payment_card_number", "—")
    card_holder = await db.get_setting("payment_card_holder", "—")

    await state.set_state(VipPaymentStates.waiting_screenshot)
    await state.update_data(vip_months=months, vip_price=price)

    discount_line = f"🎉 Chegirma: -{discount}%\n" if discount else ""
    text = (
        f"💳 <b>{months} oylik VIP — {fmt_number(price)} so'm</b>\n"
        f"{discount_line}\n"
        f"To'lovni quyidagi kartaga amalga oshiring:\n"
        f"💳 Karta: <code>{card_number}</code>\n"
        f"👤 Egasi: {card_holder}\n\n"
        f"To'lovni amalga oshirgach, chek (screenshot) rasmini shu yerga yuboring 📸"
    )
    await call.message.answer(text, reply_markup=kb.cancel_menu())
    await call.answer()


@router.message(VipPaymentStates.waiting_screenshot, F.photo)
async def vip_receive_screenshot(message: Message, db: Database, state: FSMContext, bot: Bot):
    data = await state.get_data()
    months = data.get("vip_months")
    price = data.get("vip_price", "0")
    await state.clear()

    screenshot_file_id = message.photo[-1].file_id
    payment_id = await db.create_pending_payment(message.from_user.id, months, int(price or 0), screenshot_file_id)

    user = await ensure_user(db, message.from_user)
    await message.answer(
        "✅ Chekingiz qabul qilindi. Admin tasdiqlagach VIP faollashadi. Iltimos kuting.",
        reply_markup=kb.main_menu(is_admin=bool(user["is_admin"])),
    )

    admins_cur = await db.conn.execute("SELECT telegram_id FROM users WHERE is_admin=1")
    admin_ids = [r["telegram_id"] for r in await admins_cur.fetchall()]
    caption = (
        f"💳 <b>Yangi VIP to'lov</b>\n\n"
        f"👤 Foydalanuvchi: {message.from_user.full_name} (@{message.from_user.username or '—'})\n"
        f"🆔 ID: <code>{message.from_user.id}</code>\n"
        f"📦 Paket: {months} oylik\n"
        f"💰 Narx: {fmt_number(price)} so'm"
    )
    for admin_id in admin_ids:
        try:
            await bot.send_photo(
                admin_id, screenshot_file_id, caption=caption,
                reply_markup=kb.vip_payment_confirm_keyboard(payment_id),
            )
        except Exception:
            logger.exception("Adminga to'lov xabari yuborilmadi: %s", admin_id)


@router.message(VipPaymentStates.waiting_screenshot)
async def vip_screenshot_wrong_type(message: Message):
    await message.answer("📸 Iltimos, to'lov chekining rasmini (screenshot) yuboring.")


# ------------------------------------------------------------------ #
# Yordam
# ------------------------------------------------------------------ #
@router.message(F.text == "🆘 Yordam")
async def help_menu(message: Message, db: Database):
    text = await db.get_setting("help_text")
    admin_username = await db.get_setting("help_admin_username")
    full = text
    if admin_username:
        full += f"\n\n📢 Reklama va murojaat uchun\n👤 Admin: @{admin_username.lstrip('@')}"
    await message.answer(full)


# ------------------------------------------------------------------ #
# 🏆 Top 10 / 🎟 Premyeralar / 🎞 To'plamlar
# ------------------------------------------------------------------ #
@router.message(F.text == "🏆 Top 10")
async def top10_viewed(message: Message, db: Database):
    rows = await db.top_viewed(limit=10, days=30)
    if not rows:
        rows = await db.list_top_rated(limit=10)
        if not rows:
            await message.answer("Hozircha ma'lumot yo'q — kinolar ko'rila boshlagach Top 10 paydo bo'ladi.")
            return
        lines = ["🏆 <b>Eng yuqori baholangan 10 ta kino:</b>\n"]
        for i, a in enumerate(rows, start=1):
            lines.append(f"{i}. {a['title']} — {a['avg_score']:.1f}⭐")
    else:
        lines = ["🏆 <b>Oxirgi 30 kunning eng ko'p ko'rilgan 10 ta kinosi:</b>\n"]
        for i, a in enumerate(rows, start=1):
            lines.append(f"{i}. {a['title']} — {a['view_count']} marta ko'rilgan")
    await message.answer("\n".join(lines), reply_markup=kb.anime_list_keyboard(rows, 0, 1, "top"))


@router.message(F.text == "🎟 Premyeralar")
async def premieres(message: Message, db: Database):
    rows = await db.list_premieres(limit=PER_PAGE)
    if not rows:
        await message.answer("Hozircha yangi premyeralar yo'q.")
        return
    await message.answer(
        "🎟 <b>Premyeralar</b> — oxirgi 30 kunda qo'shilgan kinolar:",
        reply_markup=kb.anime_list_keyboard(rows, 0, 1, "prem"),
    )


@router.message(F.text == "🎞 To'plamlar")
async def collections_menu(message: Message, db: Database):
    cols = [c for c in await db.list_collections() if c["item_count"] > 0]
    if not cols:
        await message.answer("Hozircha to'plamlar yo'q.")
        return
    await message.answer("🎞 <b>To'plamlar</b> — tanlang:", reply_markup=kb.collections_keyboard(cols))


@router.callback_query(F.data.regexp(r"^col:\d+$"))
async def collection_open(call: CallbackQuery, db: Database, state: FSMContext):
    col_id = int(call.data.split(":")[1])
    col = await db.get_collection(col_id)
    if not col:
        await call.answer("To'plam topilmadi.", show_alert=True)
        return
    rows = await db.collection_items(col_id, page=0, per_page=PER_PAGE)
    if not rows:
        await call.answer("Bu to'plamda hali kino yo'q.", show_alert=True)
        return
    await state.update_data(collection_id=col_id)
    total_pages = max(1, math.ceil(await db.count_collection_items(col_id) / PER_PAGE))
    await call.message.answer(
        f"{col['emoji']} <b>{col['title']}</b>:",
        reply_markup=kb.anime_list_keyboard(rows, 0, total_pages, "col"),
    )
    await call.answer()


@router.callback_query(F.data == "browse:top10")
async def browse_top10(call: CallbackQuery, db: Database):
    await call.answer()
    await top10_viewed(call.message, db)


@router.callback_query(F.data == "browse:premieres")
async def browse_premieres(call: CallbackQuery, db: Database):
    await call.answer()
    await premieres(call.message, db)


@router.callback_query(F.data == "browse:collections")
async def browse_collections(call: CallbackQuery, db: Database):
    await call.answer()
    await collections_menu(call.message, db)


# ------------------------------------------------------------------ #
# 🎙 Variantlar (bir kino — bir nechta til/sifat)
# ------------------------------------------------------------------ #
@router.callback_query(F.data.startswith("ver:list:"))
async def versions_list(call: CallbackQuery, db: Database, bot: Bot):
    anime_id = int(call.data.split(":")[2])
    anime = await db.get_anime(anime_id)
    if not anime:
        await call.answer("Kino topilmadi.", show_alert=True)
        return
    allowed, reason = await funnel.check_access(db, call.from_user.id, anime)
    if not allowed:
        await call.answer()
        await funnel.send_vip_offer(bot, db, call.message.chat.id, call.from_user.id, reason, anime["title"])
        return
    versions = await db.list_versions(anime_id)
    if not versions:
        await call.answer("Variantlar topilmadi.", show_alert=True)
        return
    await call.message.answer(
        f"🎙 <b>{anime['title']}</b>\nTil va sifatni tanlang:",
        reply_markup=kb.versions_keyboard(anime_id, versions),
    )
    await call.answer()


@router.callback_query(F.data.startswith("ver:watch:"))
async def version_watch(call: CallbackQuery, db: Database, bot: Bot):
    version = await db.get_version(int(call.data.split(":")[2]))
    if not version:
        await call.answer("Variant topilmadi.", show_alert=True)
        return
    anime = await db.get_anime(version["anime_id"])
    if not anime:
        await call.answer("Kino topilmadi.", show_alert=True)
        return
    allowed, reason = await funnel.check_access(db, call.from_user.id, anime)
    if not allowed:
        await call.answer()
        await funnel.send_vip_offer(bot, db, call.message.chat.id, call.from_user.id, reason, anime["title"])
        return
    await archive.send_episode_video(
        bot, call.from_user.id, {"video_file_id": version["video_file_id"]},
        caption=f"🎬 {anime['title']}\n🎙 {version['label']}", protect=bool(anime["is_vip"]),
    )
    await db.log_watch(call.from_user.id, anime["id"], None)
    if not await db.has_rated(anime["id"], call.from_user.id):
        await bot.send_message(
            call.from_user.id, "Ushbu kinoni baholashni unutmang 👇",
            reply_markup=kb.post_watch_rate_keyboard(anime["id"]),
        )
    await call.answer()


# ------------------------------------------------------------------ #
# ⚠️ Muammo haqida xabar
# ------------------------------------------------------------------ #
@router.callback_query(F.data.startswith("report:"))
async def report_start(call: CallbackQuery, state: FSMContext):
    anime_id = int(call.data.split(":")[1])
    await state.set_state(ReportStates.waiting_text)
    await state.update_data(report_anime_id=anime_id)
    await call.message.answer(
        "⚠️ Muammoni qisqa yozing (masalan: «video ochilmayapti», «ovoz yo'q», «boshqa kino ketgan»):",
        reply_markup=kb.cancel_menu(),
    )
    await call.answer()


@router.message(ReportStates.waiting_text, F.text)
async def report_save(message: Message, db: Database, state: FSMContext, bot: Bot):
    data = await state.get_data()
    anime_id = data.get("report_anime_id")
    await state.clear()
    text = message.text.strip()
    report_id = await db.create_report(message.from_user.id, anime_id, text)
    user = await ensure_user(db, message.from_user)
    await message.answer(
        "✅ Rahmat! Xabaringiz adminga yuborildi, tez orada tuzatamiz.",
        reply_markup=kb.main_menu(is_admin=bool(user["is_admin"])),
    )
    anime = await db.get_anime(anime_id) if anime_id else None
    title = f"{anime['title']} (ID: {anime['anime_code']})" if anime else "—"
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    b = InlineKeyboardBuilder()
    b.button(text="✅ Tuzatildi", callback_data=f"rep:fix:{report_id}")
    msg = (
        f"⚠️ <b>Muammo xabari</b>\n\n🎬 {title}\n"
        f"👤 {message.from_user.full_name} (<code>{message.from_user.id}</code>)\n\n💬 {text}"
    )
    for admin_id in await db.all_admin_ids():
        try:
            await bot.send_message(admin_id, msg, reply_markup=b.as_markup())
        except Exception:  # noqa: BLE001
            logger.exception("Adminga muammo xabari yuborilmadi: %s", admin_id)


@router.message(ReportStates.waiting_text)
async def report_wrong_type(message: Message):
    await message.answer("✏️ Iltimos, muammoni matn ko'rinishida yozing.")


# ------------------------------------------------------------------ #
# 📝 Kino so'rash
# ------------------------------------------------------------------ #
REQUESTS_PER_DAY = 3


@router.message(F.text == "📝 Kino so'rash")
async def request_start(message: Message, db: Database, state: FSMContext):
    if await db.get_setting("movie_requests_enabled", "1") != "1":
        await message.answer("📝 Kino so'rash hozircha o'chirilgan.")
        return
    if await db.count_requests_today(message.from_user.id) >= REQUESTS_PER_DAY:
        await message.answer(f"Bugun {REQUESTS_PER_DAY} ta so'rov yubordingiz. Ertaga yana urinib ko'ring 🙂")
        return
    await state.set_state(RequestStates.waiting_text)
    await message.answer(
        "📝 Qaysi kinoni qo'shishimizni xohlaysiz? Nomini (va bilsangiz yilini) yozing:",
        reply_markup=kb.cancel_menu(),
    )


@router.message(RequestStates.waiting_text, F.text)
async def request_save(message: Message, db: Database, state: FSMContext, bot: Bot):
    await state.clear()
    text = message.text.strip()
    req_id = await db.create_movie_request(message.from_user.id, text)
    user = await ensure_user(db, message.from_user)
    await message.answer(
        "✅ So'rovingiz qabul qilindi! Kino qo'shilsa, sizga xabar beramiz.",
        reply_markup=kb.main_menu(is_admin=bool(user["is_admin"])),
    )
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    b = InlineKeyboardBuilder()
    b.button(text="✅ Qo'shildi", callback_data=f"req:done:{req_id}")
    b.button(text="❌ Rad etish", callback_data=f"req:no:{req_id}")
    b.adjust(2)
    msg = (
        f"📝 <b>Yangi kino so'rovi</b>\n\n"
        f"👤 {message.from_user.full_name} (<code>{message.from_user.id}</code>)\n\n🎬 {text}"
    )
    for admin_id in await db.all_admin_ids():
        try:
            await bot.send_message(admin_id, msg, reply_markup=b.as_markup())
        except Exception:  # noqa: BLE001
            logger.exception("Adminga so'rov yuborilmadi: %s", admin_id)


@router.message(RequestStates.waiting_text)
async def request_wrong_type(message: Message):
    await message.answer("✏️ Iltimos, kino nomini matn ko'rinishida yozing.")


# ------------------------------------------------------------------ #
# Kod yoki nom yozib to'g'ridan-to'g'ri qidirish (menyuni bosmasdan)
# MUHIM: bu handlerlar fayl OXIRIDA turishi kerak — avval menyu tugmalari tekshiriladi.
# ------------------------------------------------------------------ #
@router.message(StateFilter(None), F.text.regexp(r"^\s*#?\d{1,9}\s*$"))
async def quick_code(message: Message, db: Database):
    code = message.text.strip().lstrip("#").strip()
    anime = await db.get_anime_by_code(code)
    if not anime:
        await message.answer(
            "😕 Bunday kodli kino topilmadi. Nomini yozib qidirishingiz yoki 📝 Kino so'rash tugmasini bosishingiz mumkin."
        )
        return
    await show_anime_detail(message, db, anime["id"], message.from_user.id)


@router.message(StateFilter(None), F.text, ~F.text.startswith("/"))
async def quick_title(message: Message, db: Database, state: FSMContext):
    query = (message.text or "").strip()
    if len(query) < 2:
        return
    rows = await db.search_anime_by_title(query, limit=200)
    if not rows:
        await message.answer(
            f"😕 «{query}» bo'yicha hech narsa topilmadi.\n"
            "Boshqacha yozib ko'ring yoki 📝 Kino so'rash tugmasi orqali shu kinoni so'rang."
        )
        return
    await state.update_data(search_query=query)
    total_pages = max(1, math.ceil(len(rows) / PER_PAGE))
    await message.answer(
        f"🔎 «{query}» bo'yicha natijalar:",
        reply_markup=kb.anime_list_keyboard(rows[:PER_PAGE], 0, total_pages, "search_title"),
    )
