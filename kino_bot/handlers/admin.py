from __future__ import annotations

import asyncio
import datetime
import html
import json
import logging
import math
import os
from typing import Optional

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import alerts
import archive
import backup_service
import config
import keyboards as kb
from database import Database, validate_sqlite_backup
from handlers import user as user_handlers
from handlers.admin_more import begin_add_options
from handlers.common import (
    IsAdminFilter,
    build_anime_card_text,
    build_admin_anime_text,
    build_announcement_markup,
    ensure_user,
    fmt_number,
)
from states import AddAnimeStates, AddEpisodeStates, AdminTextStates, BroadcastStates, EditAnimeStates
from tg_utils import call_with_retry

logger = logging.getLogger("anime_bot.admin")
router = Router(name="admin")
router.message.filter(IsAdminFilter())
router.callback_query.filter(IsAdminFilter())

PER_PAGE = 10

# broadcast_id -> {"paused": bool, "cancel": bool}
_broadcast_flags: dict[int, dict] = {}


# ------------------------------------------------------------------ #
# GLOBAL: Bekor qilish / /start / /cancel — har qanday FSM holatidan
# MAJBURIY chiqarish uchun (bu handlerlar shu routerdagi eng yuqorida
# turgani uchun, pastdagi har bir "waiting_..." bosqichidan USTUN keladi
# va admin hech qachon "Iltimos ... yuboring" degan holatda qolib
# ketmaydi, hatto /start yoki /cancel yozsa ham).
# ------------------------------------------------------------------ #
async def _cleanup_restore_tmp(state: FSMContext) -> None:
    """Agar restore fayli yuklab olingan, lekin tasdiqlanmagan bo'lsa — diskdan tozalaydi."""
    data = await state.get_data()
    tmp_path = data.get("restore_tmp_path")
    if tmp_path and os.path.exists(tmp_path):
        try:
            os.remove(tmp_path)
        except OSError:
            pass


@router.message(F.text == "❌ Bekor qilish")
async def admin_cancel_any(message: Message, state: FSMContext, db: Database):
    await _cleanup_restore_tmp(state)
    await user_handlers.cancel_any(message, db, state)


@router.message(Command("cancel"))
async def admin_cancel_command(message: Message, state: FSMContext, db: Database):
    await _cleanup_restore_tmp(state)
    await user_handlers.cancel_any(message, db, state)


@router.message(CommandStart())
async def admin_start_override(message: Message, state: FSMContext, db: Database):
    await _cleanup_restore_tmp(state)
    await user_handlers.cmd_start(message, db, state)


# ------------------------------------------------------------------ #
# Kirish
# ------------------------------------------------------------------ #
@router.message(F.text == "⚙️ Admin panel")
async def open_admin_panel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "⚙️ <b>Admin panel</b>\n\nBo'limni tanlang:",
        reply_markup=kb.admin_panel_menu(),
    )
    await message.answer(
        "Foydalanuvchi paneliga qaytish uchun tugmadan foydalaning 👇",
        reply_markup=kb.admin_back_to_user_menu(),
    )


@router.callback_query(F.data == "adm:panel")
async def back_to_panel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await call.message.edit_text("⚙️ <b>Admin panel</b>\n\nBo'limni tanlang:", reply_markup=kb.admin_panel_menu())
    except TelegramBadRequest:
        await call.message.answer("⚙️ <b>Admin panel</b>\n\nBo'limni tanlang:", reply_markup=kb.admin_panel_menu())
    await call.answer()


# ------------------------------------------------------------------ #
# Statistika
# ------------------------------------------------------------------ #
@router.callback_query(F.data == "adm:stats")
async def stats(call: CallbackQuery, db: Database):
    s = await db.stats()
    x = await db.stats_extended()
    lines = [
        "📊 <b>Statistika</b>",
        "",
        f"👥 Foydalanuvchilar: {fmt_number(s['users'])}",
        f"💎 VIP foydalanuvchilar: {fmt_number(s['vip_users'])}",
        f"🎬 Kinolar: {fmt_number(s['anime'])}",
        f"📼 Qismlar: {fmt_number(s['episodes'])}",
        "",
        "🆕 <b>Yangi foydalanuvchilar</b>",
        f"• Bugun: {fmt_number(x['new_today'])}",
        f"• 7 kunda: {fmt_number(x['new_7d'])}",
        f"• 30 kunda: {fmt_number(x['new_30d'])}",
        "",
        "📈 <b>Faollik</b> (video ko'rganlar)",
        f"• Bugun: {fmt_number(x['active_today'])} kishi, {fmt_number(x['views_today'])} ko'rish",
        f"• 7 kunda: {fmt_number(x['active_7d'])} kishi, {fmt_number(x['views_7d'])} ko'rish",
        f"• Jami ko'rishlar: {fmt_number(x['views_total'])}",
    ]
    sales = await db.vip_sales_stats()
    lines += [
        "",
        "💰 <b>VIP daromad</b> (to'lovlar)",
        f"• Bugun: {fmt_number(sales['today'][0])} ta, {fmt_number(sales['today'][1])} so'm",
        f"• 7 kunda: {fmt_number(sales['week'][0])} ta, {fmt_number(sales['week'][1])} so'm",
        f"• 30 kunda: {fmt_number(sales['month'][0])} ta, {fmt_number(sales['month'][1])} so'm",
        f"• Jami: {fmt_number(sales['all'][0])} ta, {fmt_number(sales['all'][1])} so'm",
        f"📝 Yangi so'rovlar: {await db.count_new_requests()} · ⚠️ Muammolar: {await db.count_new_reports()}",
    ]
    if x["top_7d"]:
        lines += ["", "🔥 <b>Oxirgi 7 kunda eng ko'p ko'rilgan</b>"]
        lines += [f"{i}. {html.escape(t)} — {c}" for i, (t, c) in enumerate(x["top_7d"], 1)]
    if x["top_all"]:
        lines += ["", "🏆 <b>Hamma vaqtdagi eng ko'p ko'rilgan</b>"]
        lines += [f"{i}. {html.escape(t)} — {c}" for i, (t, c) in enumerate(x["top_all"], 1)]
    await call.message.answer("\n".join(lines), reply_markup=kb.admin_back_button())
    await call.answer()


# ------------------------------------------------------------------ #
# Anime qo'shish
# ------------------------------------------------------------------ #
@router.callback_query(F.data == "adm:add_anime")
async def add_anime_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(AddAnimeStates.waiting_title)
    await call.message.answer("➕ Yangi kino nomini kiriting:", reply_markup=kb.cancel_menu())
    await call.answer()


@router.message(AddAnimeStates.waiting_title)
async def add_anime_title(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("✏️ Iltimos, kino nomini matn ko'rinishida yuboring.")
        return
    title = message.text.strip()
    await state.update_data(new_title=title, anilist_id=None, al_country=None, al_year=None, al_extra=None)
    await state.set_state(AddAnimeStates.waiting_source)
    await message.answer(
        f"🔎 «{html.escape(title)}» haqidagi ma'lumotlarni (tavsif, poster, janr, yil, rejissyor, aktyorlar, treyler) TMDB'dan "
        "avtomatik to'ldiraymi?",
        reply_markup=kb.anilist_source_keyboard(),
    )


@router.message(AddAnimeStates.waiting_description)
async def add_anime_description(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("✏️ Iltimos, tavsifni matn ko'rinishida yuboring (yoki «-»).")
        return
    desc = message.text.strip()
    await state.update_data(new_description="" if desc == "-" else desc)
    await state.set_state(AddAnimeStates.waiting_poster)
    await message.answer("🖼 Kino uchun poster rasm yuboring (yoki «-» deb yozing, rasmsiz qo'shish uchun):")


@router.message(AddAnimeStates.waiting_poster, F.photo)
async def add_anime_poster(message: Message, state: FSMContext, db: Database):
    await state.update_data(new_poster=message.photo[-1].file_id)
    await _ask_genres(message, state, db)


@router.message(AddAnimeStates.waiting_poster, F.text == "-")
async def add_anime_no_poster(message: Message, state: FSMContext, db: Database):
    await state.update_data(new_poster=None)
    await _ask_genres(message, state, db)


@router.message(AddAnimeStates.waiting_poster)
async def add_anime_poster_wrong(message: Message):
    await message.answer("🖼 Rasm yuboring yoki rasmsiz o'tish uchun «-» deb yozing.")


async def _ask_genres(message: Message, state: FSMContext, db: Database):
    await state.update_data(selected_genres=[])
    await state.set_state(AddAnimeStates.waiting_genres)
    genres = await db.list_genres()
    await message.answer(
        "🎭 Janr(lar)ni tanlang (bir nechtasini belgilash mumkin), so'ng ✅ Tayyor bosing:",
        reply_markup=kb.genres_menu(genres, selected_ids=set(), for_admin_add=True),
    )


@router.callback_query(AddAnimeStates.waiting_genres, F.data.startswith("pick_genre:"))
async def add_anime_pick_genre(call: CallbackQuery, state: FSMContext, db: Database, bot: Bot):
    value = call.data.split(":")[1]
    data = await state.get_data()
    selected: list[int] = data.get("selected_genres", [])

    if value == "done":
        # Anime hali yaratilmaydi: avval davlat -> til -> subtitr -> yil inline tugma bilan tanlanadi,
        # keyin handlers/admin_more.py anime'ni yaratadi.
        await state.update_data(selected_genres=selected)
        await call.answer()
        await begin_add_options(bot, call.message.chat.id, state)
        return

    genre_id = int(value)
    if genre_id in selected:
        selected.remove(genre_id)
    else:
        selected.append(genre_id)
    await state.update_data(selected_genres=selected)
    genres = await db.list_genres()
    try:
        await call.message.edit_reply_markup(
            reply_markup=kb.genres_menu(genres, selected_ids=set(selected), for_admin_add=True)
        )
    except TelegramBadRequest:
        pass
    await call.answer()


# ------------------------------------------------------------------ #
# Animelar ro'yxati / detali (admin)
# ------------------------------------------------------------------ #
@router.callback_query(F.data.startswith("adm:anime_list:"))
async def admin_anime_list(call: CallbackQuery, db: Database):
    page = int(call.data.split(":")[2])
    rows = (await db.all_anime_for_random())
    rows.sort(key=lambda r: r["created_at"], reverse=True)
    total_pages = max(1, math.ceil(len(rows) / PER_PAGE))
    page_rows = rows[page * PER_PAGE:(page + 1) * PER_PAGE]
    text = f"🎬 <b>Kinolar</b> ({len(rows)} ta):" if rows else "Hali kino qo'shilmagan."
    markup = kb.admin_anime_list_keyboard(page_rows, page, total_pages)
    try:
        await call.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        await call.message.answer(text, reply_markup=markup)
    await call.answer()


@router.callback_query(F.data.startswith("adm:anime:"))
async def admin_anime_detail(call: CallbackQuery, db: Database):
    anime_id = int(call.data.split(":")[2])
    anime = await db.get_anime(anime_id)
    if not anime:
        await call.answer("Topilmadi.", show_alert=True)
        return
    seasons = await db.list_seasons(anime_id)
    text = await build_admin_anime_text(db, anime_id)
    await call.message.answer(text, reply_markup=kb.admin_anime_detail_keyboard(anime_id, bool(seasons)))
    await call.answer()


@router.callback_query(F.data.startswith("adm:edit_title:"))
async def admin_edit_title_start(call: CallbackQuery, state: FSMContext):
    anime_id = int(call.data.split(":")[2])
    await state.update_data(edit_anime_id=anime_id)
    await state.set_state(EditAnimeStates.waiting_new_title)
    await call.message.answer("✏️ Yangi nomni kiriting:", reply_markup=kb.cancel_menu())
    await call.answer()


@router.message(EditAnimeStates.waiting_new_title)
async def admin_edit_title_save(message: Message, state: FSMContext, db: Database):
    if not message.text:
        await message.answer("✏️ Iltimos, matn yuboring.")
        return
    data = await state.get_data()
    anime_id = data["edit_anime_id"]
    await db.conn.execute("UPDATE anime SET title=? WHERE id=?", (message.text.strip(), anime_id))
    await db.conn.commit()
    await state.clear()
    user = await ensure_user(db, message.from_user)
    await message.answer("✅ Nomi yangilandi.", reply_markup=kb.main_menu(is_admin=True))
    await message.answer("⚙️ Admin panel:", reply_markup=kb.admin_panel_menu())


@router.callback_query(F.data.startswith("adm:edit_desc:"))
async def admin_edit_desc_start(call: CallbackQuery, state: FSMContext):
    anime_id = int(call.data.split(":")[2])
    await state.update_data(edit_anime_id=anime_id)
    await state.set_state(EditAnimeStates.waiting_new_description)
    await call.message.answer("✏️ Yangi tavsifni kiriting:", reply_markup=kb.cancel_menu())
    await call.answer()


@router.message(EditAnimeStates.waiting_new_description)
async def admin_edit_desc_save(message: Message, state: FSMContext, db: Database):
    if not message.text:
        await message.answer("✏️ Iltimos, matn yuboring.")
        return
    data = await state.get_data()
    anime_id = data["edit_anime_id"]
    await db.conn.execute("UPDATE anime SET description=? WHERE id=?", (message.text.strip(), anime_id))
    await db.conn.commit()
    await state.clear()
    await message.answer("✅ Tavsif yangilandi.", reply_markup=kb.main_menu(is_admin=True))
    await message.answer("⚙️ Admin panel:", reply_markup=kb.admin_panel_menu())


@router.callback_query(F.data.startswith("adm:edit_genres:"))
async def admin_edit_genres_start(call: CallbackQuery, state: FSMContext, db: Database):
    anime_id = int(call.data.split(":")[2])
    current = await db.get_anime_genres(anime_id)
    selected = [g["id"] for g in current]
    await state.update_data(edit_anime_id=anime_id, selected_genres=selected)
    await state.set_state(EditAnimeStates.waiting_genres)
    genres = await db.list_genres()
    await call.message.answer(
        "🎭 Janrlarni belgilang, so'ng ✅ Tayyor bosing:",
        reply_markup=kb.genres_menu(genres, selected_ids=set(selected), for_admin_add=True),
    )
    await call.answer()


@router.callback_query(EditAnimeStates.waiting_genres, F.data.startswith("pick_genre:"))
async def admin_edit_genres_pick(call: CallbackQuery, state: FSMContext, db: Database):
    value = call.data.split(":")[1]
    data = await state.get_data()
    selected: list[int] = data.get("selected_genres", [])
    anime_id = data["edit_anime_id"]

    if value == "done":
        await db.update_anime_genres(anime_id, selected)
        await state.clear()
        seasons = await db.list_seasons(anime_id)
        await call.message.answer(
            "✅ Janrlar yangilandi.", reply_markup=kb.admin_anime_detail_keyboard(anime_id, bool(seasons))
        )
        await call.answer()
        return

    genre_id = int(value)
    if genre_id in selected:
        selected.remove(genre_id)
    else:
        selected.append(genre_id)
    await state.update_data(selected_genres=selected)
    genres = await db.list_genres()
    try:
        await call.message.edit_reply_markup(
            reply_markup=kb.genres_menu(genres, selected_ids=set(selected), for_admin_add=True)
        )
    except TelegramBadRequest:
        pass
    await call.answer()


@router.callback_query(F.data.startswith("adm:delete_anime_confirm:"))
async def admin_delete_confirm(call: CallbackQuery):
    anime_id = int(call.data.split(":")[2])
    await call.message.answer(
        "🗑 Rostdan ham ushbu kinoni (barcha qismlari bilan) o'chirmoqchimisiz?",
        reply_markup=kb.confirm_delete_keyboard(anime_id),
    )
    await call.answer()


@router.callback_query(F.data.startswith("adm:delete_anime:"))
async def admin_delete_anime(call: CallbackQuery, db: Database):
    anime_id = int(call.data.split(":")[2])
    await db.conn.execute("DELETE FROM anime WHERE id=?", (anime_id,))
    await db.conn.commit()
    await call.message.edit_text("🗑 Kino o'chirildi.")
    await call.answer()
    backup_service.request_backup("kino o'chirildi")


# ------------------------------------------------------------------ #
# Qism qo'shish
# ------------------------------------------------------------------ #
FILLER_MARK = "__filler__"  # qism navbatida filler o'rnini belgilaydi (video yo'q)


@router.callback_query(F.data.startswith("adm:add_episode:"))
async def add_episode_start(call: CallbackQuery, state: FSMContext):
    """Kino sahifasidan to'g'ridan-to'g'ri qism qo'shish — FAQAT fasllarsiz (oddiy) kinolar uchun."""
    anime_id = int(call.data.split(":")[2])
    await state.update_data(episode_anime_id=anime_id, episode_season_id=None, episode_videos=[])
    await state.set_state(AddEpisodeStates.waiting_video)
    await call.message.answer(
        "📼 Video(lar)ni birma-bir yuboring.\n\n"
        "✅ <b>Tayyor</b> — barcha yuborilgan videolarni qism sifatida saqlaydi.\n"
        "❌ <b>Bekor qilish</b> — hech narsa saqlamay chiqib ketadi.",
        reply_markup=kb.episode_upload_keyboard(),
    )
    await call.answer()


# Qism yuklash: har bir video uchun alohida javob yuborilmaydi (Telegram "flood limit" qo'yib, bot bir-ikki
# daqiqa "qotib" qolardi). Buning o'rniga videolar jim qabul qilinadi va yuborish to'xtagach (≈2 soniya)
# BITTA yangilangan holat xabari chiqadi.
_upload_locks: dict[int, asyncio.Lock] = {}
_status_gen: dict[int, int] = {}
STATUS_QUIET_SECONDS = 2.0


def _upload_lock(uid: int) -> asyncio.Lock:
    lock = _upload_locks.get(uid)
    if lock is None:
        lock = _upload_locks[uid] = asyncio.Lock()
    return lock


def _bump_status_gen(uid: int) -> int:
    _status_gen[uid] = _status_gen.get(uid, 0) + 1
    return _status_gen[uid]


def _upload_status_text(videos: list[str]) -> str:
    filler_n = sum(1 for v in videos if v == FILLER_MARK)
    filler_txt = f", {filler_n} ta filler" if filler_n else ""
    return (
        f"✅ Qabul qilindi (jami: {len(videos) - filler_n} ta video{filler_txt}).\n"
        f"Yana video yuboring, 🟡 Filler qo'shing yoki ✅ Tayyor tugmasini bosing."
    )


async def _send_upload_status(bot: Bot, state: FSMContext, chat_id: int, uid: int, gen: int) -> None:
    try:
        await asyncio.sleep(STATUS_QUIET_SECONDS)
        if _status_gen.get(uid) != gen:
            return  # orada yana video keldi yoki jarayon tugadi — yangisi javob beradi
        async with _upload_lock(uid):
            if await state.get_state() != AddEpisodeStates.waiting_video.state:
                return
            data = await state.get_data()
            videos = list(data.get("episode_videos", []))
            old_msg_id = data.get("episode_status_msg_id")
        if old_msg_id:
            try:
                await bot.delete_message(chat_id, old_msg_id)
            except Exception:
                pass
        msg = await call_with_retry(
            lambda: bot.send_message(chat_id, _upload_status_text(videos), reply_markup=kb.episode_upload_keyboard())
        )
        async with _upload_lock(uid):
            if await state.get_state() == AddEpisodeStates.waiting_video.state:
                await state.update_data(episode_status_msg_id=msg.message_id)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        await alerts.report(bot, "qism yuklash holati", e)


@router.message(AddEpisodeStates.waiting_video, F.video)
async def add_episode_video(message: Message, state: FSMContext, bot: Bot):
    uid = message.from_user.id
    async with _upload_lock(uid):
        data = await state.get_data()
        videos: list[str] = list(data.get("episode_videos", []))
        videos.append(message.video.file_id)
        await state.update_data(episode_videos=videos)
    gen = _bump_status_gen(uid)
    asyncio.create_task(_send_upload_status(bot, state, message.chat.id, uid, gen))


@router.callback_query(AddEpisodeStates.waiting_video, F.data == "adm:episodes_filler")
async def add_episode_filler_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(AddEpisodeStates.waiting_filler_count)
    await call.message.answer(
        "🟡 Nechta ketma-ket <b>filler</b> qism qo'shilsin? Raqam yuboring (masalan: <code>6</code>).\n\n"
        "Filler qismlar navbatdagi o'rniga qo'yiladi (raqamlash buzilmaydi), video yuborish shart emas. "
        "Foydalanuvchiga \"5-qismdan 10-qismgacha — filler\" deb yoziladi.",
        reply_markup=kb.episode_filler_back_keyboard(),
    )
    await call.answer()


@router.callback_query(AddEpisodeStates.waiting_filler_count, F.data == "adm:episodes_filler_back")
async def add_episode_filler_back(call: CallbackQuery, state: FSMContext):
    await state.set_state(AddEpisodeStates.waiting_video)
    await call.message.answer(
        "📼 Video yuborishni davom ettiring yoki ✅ Tayyor tugmasini bosing.",
        reply_markup=kb.episode_upload_keyboard(),
    )
    await call.answer()


@router.message(AddEpisodeStates.waiting_filler_count, F.text)
async def add_episode_filler_count(message: Message, state: FSMContext):
    txt = (message.text or "").strip()
    if not txt.isdigit() or not (1 <= int(txt) <= 300):
        await message.answer("❗ 1 dan 300 gacha bo'lgan raqam yuboring.", reply_markup=kb.episode_filler_back_keyboard())
        return
    n = int(txt)
    async with _upload_lock(message.from_user.id):
        data = await state.get_data()
        videos: list[str] = list(data.get("episode_videos", []))
        videos.extend([FILLER_MARK] * n)
        await state.update_data(episode_videos=videos, episode_status_msg_id=None)
    await state.set_state(AddEpisodeStates.waiting_video)
    filler_n = sum(1 for v in videos if v == FILLER_MARK)
    await message.answer(
        f"🟡 {n} ta filler qo'shildi (navbatda: {len(videos) - filler_n} ta video, {filler_n} ta filler).\n"
        f"Keyingi videoni yuboring yoki ✅ Tayyor tugmasini bosing.",
        reply_markup=kb.episode_upload_keyboard(),
    )


@router.message(AddEpisodeStates.waiting_filler_count)
async def add_episode_filler_wrong_type(message: Message):
    await message.answer("❗ Iltimos, raqam yuboring (masalan: 6).", reply_markup=kb.episode_filler_back_keyboard())


@router.message(AddEpisodeStates.waiting_video)
async def add_episode_wrong_type(message: Message):
    await message.answer(
        "📼 Iltimos video fayl yuboring (yoki pastdagi ✅ Tayyor / ❌ Bekor qilish tugmasidan foydalaning).",
        reply_markup=kb.episode_upload_keyboard(),
    )


async def _return_after_episode_upload(
    message: Message, db: Database, anime_id: int, season_id: Optional[int]
) -> None:
    """Video yuklash tugagach (Tayyor yoki Bekor qilish), admin qayerga qaytishini ko'rsatadi."""
    if season_id:
        season = await db.get_season(season_id)
        anime = await db.get_anime(anime_id)
        if season and anime:
            count = await db.count_episodes_in_season(anime_id, season_id)
            text = f"📁 <b>{anime['title']} — {season['season_number']}-fasl</b>\n📼 Qismlar: {count}"
            await message.answer(text, reply_markup=kb.admin_season_detail_keyboard(season_id, anime_id))
            return
    anime = await db.get_anime(anime_id)
    if anime:
        seasons = await db.list_seasons(anime_id)
        await message.answer(
            f"🎬 <b>{anime['title']}</b>",
            reply_markup=kb.admin_anime_detail_keyboard(anime_id, bool(seasons)),
        )


async def _notify_followers_new_episode(bot: Bot, db: Database, anime, episode) -> None:
    """Kinoni kuzatayotgan foydalanuvchilarga yangi qism haqida xabar beradi (bir marta, takrorsiz)."""
    try:
        followers = await db.list_followers(anime["id"])
    except Exception:
        logger.exception("Kuzatuvchilar ro'yxatini olishda xato")
        return
    if not followers:
        return
    text = f"🔔 <b>{anime['title']}</b> — yangi qism qo'shildi!\n🎬 {episode['episode_number']}-qism"
    markup = kb.new_episode_notify_keyboard(episode["id"])
    for f in followers:
        try:
            first_time = await db.try_mark_notified(f["user_pk"], episode["id"])
        except Exception:
            continue
        if not first_time:
            continue  # bu foydalanuvchiga shu qism haqida allaqachon xabar berilgan
        try:
            await bot.send_message(f["telegram_id"], text, reply_markup=markup)
        except Exception:
            pass
        await asyncio.sleep(0.05)


@router.callback_query(AddEpisodeStates.waiting_video, F.data == "adm:episodes_done")
async def add_episode_done(call: CallbackQuery, state: FSMContext, db: Database, bot: Bot):
    uid = call.from_user.id
    _bump_status_gen(uid)          # kutib turgan holat xabarini bekor qiladi
    await call.answer()
    await asyncio.sleep(1.0)       # yo'lda kelayotgan oxirgi videolar ham ro'yxatga tushib ulgursin
    async with _upload_lock(uid):
        if await state.get_state() != AddEpisodeStates.waiting_video.state:
            return                 # ikki marta bosilgan bo'lsa — ikkinchisi hech narsa qilmaydi
        data = await state.get_data()
        anime_id = data.get("episode_anime_id")
        season_id = data.get("episode_season_id")
        videos: list[str] = list(data.get("episode_videos", []))
        old_status = data.get("episode_status_msg_id")
        await state.clear()

    if old_status:
        try:
            await bot.delete_message(call.message.chat.id, old_status)
        except Exception:
            pass

    if not videos:
        await call.message.answer("❗ Hech qanday video yuborilmadi.")
        if anime_id:
            await _return_after_episode_upload(call.message, db, anime_id, season_id)
        return

    last_episode = None
    last_real_episode = None  # kuzatuvchilarga faqat video bor (filler bo'lmagan) qism haqida xabar beramiz
    filler_count = 0
    real_episode_ids: list[int] = []
    for item in videos:
        if item == FILLER_MARK:
            last_episode = await db.add_episode(anime_id, "", season_id, is_filler=True)
            filler_count += 1
        else:
            last_episode = await db.add_episode(anime_id, item, season_id)
            last_real_episode = last_episode
            real_episode_ids.append(last_episode["id"])

    filler_txt = f" (shundan {filler_count} ta filler)" if filler_count else ""
    await call.message.answer(
        f"✅ {len(videos)} ta qism saqlandi{filler_txt} "
        f"(oxirgi qo'shilgani: {last_episode['episode_number']}-qism)."
    )
    await _return_after_episode_upload(call.message, db, anime_id, season_id)

    # Zaxira: videolar fonda kanalga ham joylanadi, baza nusxasi esa 1 daqiqadan keyin olinadi
    archive.schedule_archive(bot, db, real_episode_ids)
    backup_service.request_backup("qismlar saqlandi")

    anime = await db.get_anime(anime_id)
    if anime and last_real_episode:
        asyncio.create_task(_notify_followers_new_episode(bot, db, anime, last_real_episode))


@router.callback_query(AddEpisodeStates.waiting_video, F.data == "adm:episodes_cancel")
async def add_episode_cancel(call: CallbackQuery, state: FSMContext, db: Database):
    _bump_status_gen(call.from_user.id)
    data = await state.get_data()
    anime_id = data.get("episode_anime_id")
    season_id = data.get("episode_season_id")
    await state.clear()
    await call.answer("Bekor qilindi. Hech narsa saqlanmadi.", show_alert=True)
    if anime_id:
        await _return_after_episode_upload(call.message, db, anime_id, season_id)


# ------------------------------------------------------------------ #
# Fasllar (seasons) boshqaruvi
# ------------------------------------------------------------------ #
async def _open_seasons_admin(message: Message, db: Database, anime_id: int) -> None:
    anime = await db.get_anime(anime_id)
    if not anime:
        return
    seasons = await db.list_seasons(anime_id)
    header = f"📺 <b>{anime['title']}</b>\n🆔 Kino ID: <code>{anime['anime_code']}</code>\n\n"
    text = (
        header + "Faslni tanlang:"
        if seasons
        else header + "Hali fasl qo'shilmagan.\n\n➕ Yangi fasl qo'shish orqali boshlang."
    )
    await message.answer(text, reply_markup=kb.admin_seasons_keyboard(anime_id, seasons))


@router.callback_query(F.data.startswith("adm:season_pick:"))
async def season_pick_anime_list(call: CallbackQuery, db: Database):
    page = int(call.data.split(":")[2])
    rows = await db.all_anime_for_random()
    rows = sorted(rows, key=lambda r: r["created_at"], reverse=True)
    total_pages = max(1, math.ceil(len(rows) / PER_PAGE))
    page_rows = rows[page * PER_PAGE: (page + 1) * PER_PAGE]
    text = "📚 Qaysi kinoga fasl qo'shmoqchisiz?" if rows else "Hali kino qo'shilmagan."
    markup = kb.admin_pick_anime_for_season_keyboard(page_rows, page, total_pages)
    try:
        await call.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        await call.message.answer(text, reply_markup=markup)
    await call.answer()


@router.callback_query(F.data.startswith("adm:season_pick_anime:"))
async def season_pick_anime_selected(call: CallbackQuery, db: Database):
    anime_id = int(call.data.split(":")[2])
    anime = await db.get_anime(anime_id)
    if not anime:
        await call.answer("Topilmadi.", show_alert=True)
        return
    await _open_seasons_admin(call.message, db, anime_id)
    await call.answer()


@router.callback_query(F.data.startswith("adm:seasons_of:"))
async def seasons_of_anime(call: CallbackQuery, db: Database):
    anime_id = int(call.data.split(":")[2])
    await _open_seasons_admin(call.message, db, anime_id)
    await call.answer()


@router.callback_query(F.data.startswith("adm:add_season:"))
async def add_season(call: CallbackQuery, db: Database, state: FSMContext):
    anime_id = int(call.data.split(":")[2])
    # Agar bu animeda ilgari fasllarsiz qismlar bo'lsa — ularni xavfsiz "1-fasl"ga ko'chiradi
    await db.ensure_season_migration(anime_id)
    next_num = await db.next_season_number(anime_id)
    season = await db.create_season(anime_id, next_num)
    await state.update_data(episode_anime_id=anime_id, episode_season_id=season["id"], episode_videos=[])
    await state.set_state(AddEpisodeStates.waiting_video)
    await call.message.answer(
        f"✅ {next_num}-fasl yaratildi.\n\n"
        f"📼 Endi shu fasl uchun video(lar)ni birma-bir yuboring.\n\n"
        f"✅ <b>Tayyor</b> — barcha yuborilgan videolarni qism sifatida saqlaydi.\n"
        f"❌ <b>Bekor qilish</b> — hech narsa saqlamay chiqib ketadi.",
        reply_markup=kb.episode_upload_keyboard(),
    )
    await call.answer()


@router.callback_query(F.data.startswith("adm:season:"))
async def season_detail(call: CallbackQuery, db: Database):
    season_id = int(call.data.split(":")[2])
    season = await db.get_season(season_id)
    if not season:
        await call.answer("Topilmadi.", show_alert=True)
        return
    anime = await db.get_anime(season["anime_id"])
    count = await db.count_episodes_in_season(season["anime_id"], season_id)
    text = f"📁 <b>{anime['title']} — {season['season_number']}-fasl</b>\n📼 Qismlar: {count}"
    await call.message.answer(text, reply_markup=kb.admin_season_detail_keyboard(season_id, season["anime_id"]))
    await call.answer()


@router.callback_query(F.data.startswith("adm:add_episode_season:"))
async def add_episode_season_start(call: CallbackQuery, state: FSMContext, db: Database):
    season_id = int(call.data.split(":")[2])
    season = await db.get_season(season_id)
    if not season:
        await call.answer("Topilmadi.", show_alert=True)
        return
    await state.update_data(episode_anime_id=season["anime_id"], episode_season_id=season_id, episode_videos=[])
    await state.set_state(AddEpisodeStates.waiting_video)
    await call.message.answer(
        f"📼 {season['season_number']}-fasl uchun video(lar)ni birma-bir yuboring.\n\n"
        "✅ <b>Tayyor</b> — barcha yuborilgan videolarni qism sifatida saqlaydi.\n"
        "❌ <b>Bekor qilish</b> — hech narsa saqlamay chiqib ketadi.",
        reply_markup=kb.episode_upload_keyboard(),
    )
    await call.answer()


# ------------------------------------------------------------------ #
# Kanalga e'lon qilish
# ------------------------------------------------------------------ #
@router.callback_query(F.data.startswith("adm:announce:"))
async def announce_anime(call: CallbackQuery, db: Database, bot: Bot):
    anime_id = int(call.data.split(":")[2])
    if not config.MAIN_CHANNEL:
        await call.answer("MAIN_CHANNEL .env faylida sozlanmagan.", show_alert=True)
        return
    ok = await _post_announcement(bot, db, anime_id)
    await call.answer("📢 Kanalga e'lon qilindi!" if ok else "❌ Xatolik yuz berdi.", show_alert=True)


async def _post_announcement(bot: Bot, db: Database, anime_id: int) -> bool:
    anime = await db.get_anime(anime_id)
    if not anime:
        return False
    # Anime kartasi matni — xuddi anime sahifasidagi bilan bir xil, bitta umumiy
    # funksiyadan (qarang: handlers.common.build_anime_card_text).
    caption = await build_anime_card_text(db, anime)
    # MUHIM: bu tugma yangi ko'rish tizimi yaratmaydi — u shunchaki botni
    # deep-link bilan ochadi, so'ng BOTDAGI MAVJUD /start -> majburiy obuna ->
    # VIP tekshiruvi -> anime sahifasi oqimi (cmd_start ichidagi "anime_<code>"
    # filiali) ishlaydi, xuddi foydalanuvchi botda "Anime ID" orqali qidirgandek.
    markup = await build_announcement_markup(bot, anime)
    try:
        if anime["poster_file_id"]:
            sent = await bot.send_photo(config.MAIN_CHANNEL, anime["poster_file_id"], caption=caption, reply_markup=markup)
        else:
            sent = await bot.send_message(config.MAIN_CHANNEL, caption, reply_markup=markup)
        try:
            # Xabar ID'sini saqlaymiz: keyin reyting o'zgarsa, shu e'lonni tahrirlaymiz
            await db.set_channel_message_id(anime_id, sent.message_id)
        except Exception:
            logger.exception("Kanal xabari ID'sini saqlashda xatolik")
        return True
    except Exception:
        logger.exception("Kanalga e'lon qilishda xatolik")
        return False


# ------------------------------------------------------------------ #
# Foydalanuvchilar
# ------------------------------------------------------------------ #
@router.callback_query(F.data.startswith("adm:users:"))
async def admin_users_list(call: CallbackQuery, db: Database):
    page = int(call.data.split(":")[2])
    total = await db.count_users()
    rows = await db.list_users(page, PER_PAGE)
    total_pages = max(1, math.ceil(total / PER_PAGE))
    text = f"👥 <b>Foydalanuvchilar</b> ({total} ta):"
    markup = kb.admin_users_list_keyboard(rows, page, total_pages)
    try:
        await call.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        await call.message.answer(text, reply_markup=markup)
    await call.answer()


async def _render_user_detail_card(db: Database, telegram_id: int):
    """Foydalanuvchi kartochkasi matni va tugmalarini tayyorlaydi (qayta ishlatish uchun)."""
    user = await db.get_user(telegram_id)
    if not user:
        return None, None
    is_vip = await db.is_vip(telegram_id)
    favs = await db.list_favorites(telegram_id)
    history = await db.list_watch_history(telegram_id, limit=1000)
    cur = await db.conn.execute(
        "SELECT COUNT(*) FROM vip_purchases WHERE user_id=?", (user["id"],)
    )
    (vip_purchase_count,) = await cur.fetchone()

    username_line = f"🔗 Username: @{user['username']}\n" if user["username"] else "🔗 Username: —\n"
    text = (
        f"👤 <b>{user['full_name']}</b>\n"
        f"🆔 ID: <code>{user['telegram_id']}</code>\n"
        f"{username_line}"
    )
    text += (
        f"📊 Status: {'🛡 Admin' if user['is_admin'] else ('💎 VIP' if is_vip else '👤 Oddiy')}\n"
        f"⏳ VIP muddati: {user['vip_until'][:10] if is_vip and user['vip_until'] else '—'}\n"
        f"💰 Balans: {fmt_number(user['balance'])} so'm\n"
        f"💎 VIP xaridlar: {vip_purchase_count} ta\n"
        f"❤️ Sevimlilar: {len(favs)} ta\n"
        f"📚 Ko'rilganlar: {len(history)} ta\n"
        f"📅 Qo'shilgan: {user['joined_at'][:10]}"
    )
    markup = kb.admin_user_detail_keyboard(telegram_id, bool(user["is_admin"]), is_vip)
    return text, markup


@router.callback_query(F.data.startswith("adm:user:"))
async def admin_user_detail(call: CallbackQuery, db: Database):
    telegram_id = int(call.data.split(":")[2])
    text, markup = await _render_user_detail_card(db, telegram_id)
    if text is None:
        await call.answer("Topilmadi.", show_alert=True)
        return
    await call.message.answer(text, reply_markup=markup)
    await call.answer()


@router.callback_query(F.data.startswith("adm:make_admin:"))
async def make_admin(call: CallbackQuery, db: Database):
    telegram_id = int(call.data.split(":")[2])
    await db.set_admin(telegram_id, True)
    text, markup = await _render_user_detail_card(db, telegram_id)
    if text is not None:
        try:
            await call.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest:
            await call.message.answer(text, reply_markup=markup)
    await call.answer("✅ Admin qilindi.", show_alert=True)


@router.callback_query(F.data.startswith("adm:revoke_admin:"))
async def revoke_admin(call: CallbackQuery, db: Database):
    telegram_id = int(call.data.split(":")[2])
    await db.set_admin(telegram_id, False)
    text, markup = await _render_user_detail_card(db, telegram_id)
    if text is not None:
        try:
            await call.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest:
            await call.message.answer(text, reply_markup=markup)
    await call.answer("✅ Admin huquqi olib tashlandi.", show_alert=True)


@router.callback_query(F.data.startswith("adm:revoke_vip:"))
async def revoke_vip_cb(call: CallbackQuery, db: Database, bot: Bot):
    telegram_id = int(call.data.split(":")[2])
    was_vip = await db.is_vip(telegram_id)
    await db.revoke_vip(telegram_id)
    if was_vip:
        try:
            await bot.send_message(
                telegram_id,
                "❌ 💎 VIP obunangiz administrator tomonidan bekor qilindi.",
            )
        except Exception:
            pass
    text, markup = await _render_user_detail_card(db, telegram_id)
    if text is not None:
        try:
            await call.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest:
            await call.message.answer(text, reply_markup=markup)
    await call.answer("✅ VIP bekor qilindi." if was_vip else "Bu foydalanuvchi allaqachon VIP emas edi.", show_alert=True)


@router.callback_query(F.data.startswith("adm:grant_vip_menu:"))
async def grant_vip_menu(call: CallbackQuery):
    telegram_id = int(call.data.split(":")[2])
    await call.message.answer("💎 Muddatni tanlang:", reply_markup=kb.grant_vip_keyboard(telegram_id))
    await call.answer()


@router.callback_query(F.data.startswith("grant_vip:"))
async def grant_vip(call: CallbackQuery, db: Database, bot: Bot):
    _, telegram_id_str, months_str = call.data.split(":")
    telegram_id, months = int(telegram_id_str), int(months_str)
    new_until = await db.grant_vip(telegram_id, months, price=0, source="admin_grant",
                                    approved_by=call.from_user.id)
    await call.message.answer(f"✅ VIP berildi. Tugash sanasi: {new_until.date()}")
    await call.answer()
    try:
        await bot.send_message(
            telegram_id,
            f"🎉 Sizga admin tomonidan {months} oylik 💎 VIP status berildi!\n"
            f"⏳ Amal qilish muddati: {new_until.date()} gacha.",
        )
    except Exception:
        pass


# ------------------------------------------------------------------ #
# VIP to'lovlarini tasdiqlash / rad etish
# ------------------------------------------------------------------ #
@router.callback_query(F.data.startswith("pay_approve:"))
async def pay_approve(call: CallbackQuery, db: Database, bot: Bot):
    payment_id = int(call.data.split(":")[1])
    payment = await db.get_pending_payment(payment_id)
    if not payment or payment["status"] != "pending":
        await call.answer("Bu to'lov allaqachon ko'rib chiqilgan.", show_alert=True)
        return
    user = await db.get_user_by_pk(payment["user_id"])
    new_until = await db.grant_vip(
        user["telegram_id"], payment["months"], payment["price"], source="payment",
        approved_by=call.from_user.id,
    )
    await db.resolve_payment(payment_id, "approved", call.from_user.id)
    try:
        await call.message.edit_caption(caption=(call.message.caption or "") + "\n\n✅ TASDIQLANDI")
    except TelegramBadRequest:
        pass
    await call.answer("✅ VIP berildi.")
    try:
        await bot.send_message(
            user["telegram_id"],
            f"🎉 To'lovingiz tasdiqlandi! 💎 VIP faollashtirildi.\n"
            f"⏳ Amal qilish muddati: {new_until.date()} gacha.",
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("pay_reject:"))
async def pay_reject(call: CallbackQuery, db: Database, bot: Bot):
    payment_id = int(call.data.split(":")[1])
    payment = await db.get_pending_payment(payment_id)
    if not payment or payment["status"] != "pending":
        await call.answer("Bu to'lov allaqachon ko'rib chiqilgan.", show_alert=True)
        return
    await db.resolve_payment(payment_id, "rejected", call.from_user.id)
    user = await db.get_user_by_pk(payment["user_id"])
    try:
        await call.message.edit_caption(caption=(call.message.caption or "") + "\n\n❌ RAD ETILDI")
    except TelegramBadRequest:
        pass
    await call.answer("❌ Rad etildi.")
    try:
        await bot.send_message(
            user["telegram_id"],
            "❌ Kechirasiz, to'lovingiz tasdiqlanmadi. Savollar bo'lsa admin bilan bog'laning (🆘 Yordam).",
        )
    except Exception:
        pass


# ------------------------------------------------------------------ #
# VIP narxlari
# ------------------------------------------------------------------ #
@router.callback_query(F.data == "adm:vip_prices")
async def vip_prices_menu(call: CallbackQuery, db: Database):
    p1 = await db.get_setting("vip_price_1", "0")
    p2 = await db.get_setting("vip_price_2", "0")
    p3 = await db.get_setting("vip_price_3", "0")
    text = (
        "💰 <b>VIP narxlari</b>\n\n"
        f"1 oylik: {fmt_number(p1)} so'm\n"
        f"2 oylik: {fmt_number(p2)} so'm\n"
        f"3 oylik: {fmt_number(p3)} so'm"
    )
    await call.message.edit_text(text, reply_markup=kb.vip_prices_menu())
    await call.answer()


@router.callback_query(F.data.startswith("adm:set_price:"))
async def set_price_start(call: CallbackQuery, state: FSMContext):
    months = call.data.split(":")[2]
    state_map = {
        "1": AdminTextStates.waiting_vip_price_1,
        "2": AdminTextStates.waiting_vip_price_2,
        "3": AdminTextStates.waiting_vip_price_3,
    }
    await state.set_state(state_map[months])
    await call.message.answer(f"💰 {months} oylik VIP uchun yangi narxni kiriting (faqat raqam, so'mda):",
                               reply_markup=kb.cancel_menu())
    await call.answer()


async def _save_price(message: Message, state: FSMContext, db: Database, key: str):
    if not message.text:
        await message.answer("Iltimos faqat raqam kiriting.")
        return
    txt = message.text.strip().replace(" ", "")
    if not txt.isdigit():
        await message.answer("Iltimos faqat raqam kiriting.")
        return
    await db.set_setting(key, txt)
    await state.clear()
    await message.answer("✅ Narx yangilandi.", reply_markup=kb.main_menu(is_admin=True))
    await message.answer("⚙️ Admin panel:", reply_markup=kb.admin_panel_menu())


@router.message(AdminTextStates.waiting_vip_price_1)
async def save_price_1(message: Message, state: FSMContext, db: Database):
    await _save_price(message, state, db, "vip_price_1")


@router.message(AdminTextStates.waiting_vip_price_2)
async def save_price_2(message: Message, state: FSMContext, db: Database):
    await _save_price(message, state, db, "vip_price_2")


@router.message(AdminTextStates.waiting_vip_price_3)
async def save_price_3(message: Message, state: FSMContext, db: Database):
    await _save_price(message, state, db, "vip_price_3")


# ------------------------------------------------------------------ #
# To'lov ma'lumotlari (karta)
# ------------------------------------------------------------------ #
@router.callback_query(F.data == "adm:payment_info")
async def payment_info(call: CallbackQuery, db: Database):
    number = await db.get_setting("payment_card_number", "—")
    holder = await db.get_setting("payment_card_holder", "—")
    text = f"💳 <b>To'lov ma'lumotlari</b>\n\nKarta: <code>{number}</code>\nEgasi: {holder}"
    await call.message.edit_text(text, reply_markup=kb.payment_info_menu())
    await call.answer()


@router.callback_query(F.data == "adm:set_card_number")
async def set_card_number_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(AdminTextStates.waiting_card_number)
    await call.message.answer("💳 Yangi karta raqamini kiriting:", reply_markup=kb.cancel_menu())
    await call.answer()


@router.message(AdminTextStates.waiting_card_number)
async def save_card_number(message: Message, state: FSMContext, db: Database):
    if not message.text:
        await message.answer("✏️ Iltimos, matn yuboring.")
        return
    await db.set_setting("payment_card_number", message.text.strip())
    await state.clear()
    await message.answer("✅ Saqlandi.", reply_markup=kb.main_menu(is_admin=True))
    await message.answer("⚙️ Admin panel:", reply_markup=kb.admin_panel_menu())


@router.callback_query(F.data == "adm:set_card_holder")
async def set_card_holder_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(AdminTextStates.waiting_card_holder)
    await call.message.answer("👤 Karta egasining ismini kiriting:", reply_markup=kb.cancel_menu())
    await call.answer()


@router.message(AdminTextStates.waiting_card_holder)
async def save_card_holder(message: Message, state: FSMContext, db: Database):
    if not message.text:
        await message.answer("✏️ Iltimos, matn yuboring.")
        return
    await db.set_setting("payment_card_holder", message.text.strip())
    await state.clear()
    await message.answer("✅ Saqlandi.", reply_markup=kb.main_menu(is_admin=True))
    await message.answer("⚙️ Admin panel:", reply_markup=kb.admin_panel_menu())


# ------------------------------------------------------------------ #
# /start rasm/matn
# ------------------------------------------------------------------ #
@router.callback_query(F.data == "adm:start_settings")
async def start_settings(call: CallbackQuery):
    await call.message.edit_text("🖼 /start sozlamalari:", reply_markup=kb.start_settings_menu())
    await call.answer()


@router.callback_query(F.data == "adm:set_start_text")
async def set_start_text_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(AdminTextStates.waiting_start_text)
    await call.message.answer("✏️ Yangi /start matnini kiriting (HTML teglar ishlaydi):",
                               reply_markup=kb.cancel_menu())
    await call.answer()


@router.message(AdminTextStates.waiting_start_text)
async def save_start_text(message: Message, state: FSMContext, db: Database):
    if not message.text:
        await message.answer("✏️ Iltimos, matn yuboring.")
        return
    await db.set_setting("start_text", message.html_text or message.text)
    await state.clear()
    await message.answer("✅ Matn yangilandi.", reply_markup=kb.main_menu(is_admin=True))
    await message.answer("⚙️ Admin panel:", reply_markup=kb.admin_panel_menu())


@router.callback_query(F.data == "adm:set_start_photo")
async def set_start_photo_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(AdminTextStates.waiting_start_photo)
    await call.message.answer("🖼 Yangi rasmni yuboring:", reply_markup=kb.cancel_menu())
    await call.answer()


@router.message(AdminTextStates.waiting_start_photo, F.photo)
async def save_start_photo(message: Message, state: FSMContext, db: Database):
    await db.set_setting("start_photo_file_id", message.photo[-1].file_id)
    await state.clear()
    await message.answer("✅ Rasm yangilandi.", reply_markup=kb.main_menu(is_admin=True))
    await message.answer("⚙️ Admin panel:", reply_markup=kb.admin_panel_menu())


@router.message(AdminTextStates.waiting_start_photo)
async def save_start_photo_wrong(message: Message):
    await message.answer("🖼 Iltimos rasm yuboring.")


# ------------------------------------------------------------------ #
# Yordam matni
# ------------------------------------------------------------------ #
@router.callback_query(F.data == "adm:help_settings")
async def help_settings(call: CallbackQuery):
    await call.message.edit_text("🆘 Yordam sozlamalari:", reply_markup=kb.help_settings_menu())
    await call.answer()


@router.callback_query(F.data == "adm:set_help_text")
async def set_help_text_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(AdminTextStates.waiting_help_text)
    await call.message.answer("✏️ Yangi yordam matnini kiriting:", reply_markup=kb.cancel_menu())
    await call.answer()


@router.message(AdminTextStates.waiting_help_text)
async def save_help_text(message: Message, state: FSMContext, db: Database):
    if not message.text:
        await message.answer("✏️ Iltimos, matn yuboring.")
        return
    await db.set_setting("help_text", message.html_text or message.text)
    await state.clear()
    await message.answer("✅ Yangilandi.", reply_markup=kb.main_menu(is_admin=True))
    await message.answer("⚙️ Admin panel:", reply_markup=kb.admin_panel_menu())


@router.callback_query(F.data == "adm:set_help_admin")
async def set_help_admin_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(AdminTextStates.waiting_help_admin_username)
    await call.message.answer("👤 Admin username kiriting (masalan: @username):", reply_markup=kb.cancel_menu())
    await call.answer()


@router.message(AdminTextStates.waiting_help_admin_username)
async def save_help_admin(message: Message, state: FSMContext, db: Database):
    if not message.text:
        await message.answer("✏️ Iltimos, matn yuboring (masalan: @username).")
        return
    await db.set_setting("help_admin_username", message.text.strip().lstrip("@"))
    await state.clear()
    await message.answer("✅ Yangilandi.", reply_markup=kb.main_menu(is_admin=True))
    await message.answer("⚙️ Admin panel:", reply_markup=kb.admin_panel_menu())


# ------------------------------------------------------------------ #
# Majburiy obuna kanallari
# ------------------------------------------------------------------ #
@router.callback_query(F.data == "adm:channels")
async def channels_menu_open(call: CallbackQuery, db: Database):
    channels = await db.list_required_channels()
    text = "📢 <b>Majburiy obuna kanallari</b>\n\nO'chirish uchun kanalga bosing." if channels else \
        "📢 Hozircha majburiy kanal qo'shilmagan."
    await call.message.edit_text(text, reply_markup=kb.channels_menu(channels))
    await call.answer()


@router.callback_query(F.data == "adm:add_channel")
async def add_channel_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(AdminTextStates.waiting_required_channel)
    await call.message.answer(
        "➕ Kanal username (@kanal) yoki ID (-100...) va nomini yuboring.\n"
        "Format: <code>@kanal | Kanal nomi</code>\n\n"
        "❗ Bot kanalga admin qilib qo'shilgan bo'lishi shart (obunani tekshirish uchun).",
        reply_markup=kb.cancel_menu(),
    )
    await call.answer()


@router.message(AdminTextStates.waiting_required_channel)
async def add_channel_save(message: Message, state: FSMContext, db: Database, bot: Bot):
    if not message.text:
        await message.answer("✏️ Iltimos, matn yuboring. Format: @kanal | Kanal nomi")
        return
    parts = message.text.split("|", 1)
    chat_id = parts[0].strip()
    title = parts[1].strip() if len(parts) > 1 else chat_id

    invite_link = None
    is_numeric_id = chat_id.lstrip("-").isdigit()

    if is_numeric_id:
        # Username emas, ID orqali qo'shilmoqda — "Kanalga o'tish" tugmasi ishlashi uchun
        # bot haqiqiy taklif (invite) linkini o'zi yaratib olishga harakat qiladi
        # (chunki -100... ID'dan https://t.me/... link to'g'ridan-to'g'ri yasab bo'lmaydi).
        try:
            link_obj = await bot.create_chat_invite_link(chat_id=chat_id, name=title[:32])
            invite_link = link_obj.invite_link
        except Exception as e:
            logger.warning("Kanal uchun invite link yaratib bo'lmadi (%s): %s", chat_id, e)

    await db.add_required_channel(chat_id, title, invite_link)
    await state.clear()

    if is_numeric_id and not invite_link:
        await message.answer(
            "⚠️ Kanal ID orqali saqlandi, lekin taklif linkini avtomatik yaratib bo'lmadi.\n\n"
            "Sabab: botni ushbu kanalga <b>administrator</b> qilib qo'shmagan bo'lishingiz mumkin "
            "(kamida \"Foydalanuvchilarni taklif qilish\" huquqi bilan).\n\n"
            "Obuna tekshiruvi (kim obuna, kim emas) baribir to'g'ri ishlayveradi, lekin "
            "foydalanuvchiga \"Kanalga o'tish\" tugmasi ko'rinmaydi. Buni tuzatish uchun: "
            "botni kanalga admin qiling, so'ng bu kanalni o'chirib qayta qo'shing.",
            reply_markup=kb.main_menu(is_admin=True),
        )
    else:
        await message.answer("✅ Kanal qo'shildi.", reply_markup=kb.main_menu(is_admin=True))
    await message.answer("⚙️ Admin panel:", reply_markup=kb.admin_panel_menu())


@router.callback_query(F.data.startswith("adm:del_channel:"))
async def del_channel(call: CallbackQuery, db: Database):
    channel_pk = int(call.data.split(":")[2])
    await db.remove_required_channel(channel_pk)
    channels = await db.list_required_channels()
    await call.message.edit_text(
        "📢 <b>Majburiy obuna kanallari</b>" if channels else "📢 Hozircha majburiy kanal qo'shilmagan.",
        reply_markup=kb.channels_menu(channels),
    )
    await call.answer("O'chirildi.")


@router.callback_query(F.data == "adm:vip_free_menu")
async def vip_free_menu_open(call: CallbackQuery, db: Database):
    until = await db.get_vip_free_until()
    active = bool(until and until > datetime.datetime.utcnow())
    text = "🎁 <b>VIP'ni vaqtincha bepul qilish</b>\n\n"
    if active:
        text += (
            f"✅ Hozir faol: <b>{until.strftime('%Y-%m-%d %H:%M')}</b> (UTC) gacha "
            f"barcha foydalanuvchilar VIP kinolarni BEPUL tomosha qilishi mumkin.\n\n"
        )
    else:
        text += "Hozir faol emas. Muddatni tanlang — shu vaqt davomida BARCHA foydalanuvchilar uchun VIP kinolar bepul bo'ladi:\n\n"
    await call.message.answer(text, reply_markup=kb.vip_free_menu(active))
    await call.answer()


async def _notify_all_vip_granted(bot: Bot, db: Database) -> None:
    """'Barchaga VIP berish' faollashganda barcha foydalanuvchilarga xabar beradi."""
    ids = await db.all_telegram_ids()
    text = "🎉 Barcha foydalanuvchilarga VIP berildi!"
    markup = kb.vip_granted_all_keyboard()
    for uid in ids:
        try:
            await bot.send_message(uid, text, reply_markup=markup)
        except Exception:
            pass
        await asyncio.sleep(0.05)


@router.callback_query(F.data.startswith("adm:vip_free_set:"))
async def vip_free_set(call: CallbackQuery, db: Database, bot: Bot):
    days = int(call.data.split(":")[2])
    until = datetime.datetime.utcnow() + datetime.timedelta(days=days)
    await db.set_vip_free_until(until)
    await call.message.answer(
        f"✅ Faollashtirildi! VIP kinolar <b>{until.strftime('%Y-%m-%d %H:%M')}</b> (UTC) gacha "
        f"barcha foydalanuvchilar uchun BEPUL bo'ladi.",
        reply_markup=kb.admin_panel_menu(),
    )
    await call.answer()
    asyncio.create_task(_notify_all_vip_granted(bot, db))


@router.callback_query(F.data == "adm:vip_free_stop")
async def vip_free_stop(call: CallbackQuery, db: Database):
    await db.set_vip_free_until(None)
    await call.message.answer("🛑 Bepul VIP rejimi to'xtatildi.", reply_markup=kb.admin_panel_menu())
    await call.answer()


# ------------------------------------------------------------------ #
# Backup (zaxira olish / tiklash)
# ------------------------------------------------------------------ #
@router.callback_query(F.data == "adm:backup_menu")
async def backup_menu_open(call: CallbackQuery):
    await call.message.answer(
        "💾 <b>Zaxira nusxa</b>\n\nQuyidagilardan birini tanlang:",
        reply_markup=kb.backup_menu(),
    )
    await call.answer()


@router.callback_query(F.data == "adm:backup_export")
async def do_backup(call: CallbackQuery, db: Database):
    path = await db.backup()
    try:
        from aiogram.types import FSInputFile
        await call.message.answer_document(FSInputFile(path), caption="💾 Zaxira nusxa tayyor.")
    except Exception:
        await call.message.answer(f"💾 Zaxira nusxa yaratildi: {path}")
    await call.answer()


@router.callback_query(F.data == "adm:backup_import")
async def backup_import_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(AdminTextStates.waiting_backup_file)
    await call.message.answer(
        "📥 <b>Zaxiradan tiklash</b>\n\n"
        "⚠️ Diqqat: bu amal joriy bazadagi barcha ma'lumotlarni yuboradigan "
        "faylingizdagi ma'lumotlar bilan <b>to'liq almashtiradi</b>. "
        "Ehtiyot chorasi sifatida joriy baza avtomatik ravishda zaxiralanadi, "
        "shuning uchun xato bo'lsa ham hech narsa butunlay yo'qolmaydi.\n\n"
        "Botdan kelgan zaxira faylini (<code>.zip</code>, eski nusxalar uchun <code>.db</code>) "
        "shu yerga hujjat (document) sifatida yuboring:",
        reply_markup=kb.cancel_menu(),
    )
    await call.answer()


@router.message(AdminTextStates.waiting_backup_file, F.document)
async def backup_import_receive(message: Message, state: FSMContext, bot: Bot):
    doc = message.document
    name = (doc.file_name or "").lower()
    if not (name.endswith(".db") or name.endswith(".zip")):
        await message.answer(
            "❗ Fayl kengaytmasi <code>.zip</code> (yoki eski <code>.db</code>) bo'lishi kerak. "
            "Boshqa fayl yuboring yoki ❌ Bekor qilish tugmasini bosing."
        )
        return

    status_msg = await message.answer("⏳ Fayl tekshirilmoqda...")

    os.makedirs(config.BACKUP_DIR, exist_ok=True)
    tmp_path = os.path.join(config.BACKUP_DIR, f"_restore_{message.from_user.id}_{doc.file_unique_id}.db")
    try:
        file = await bot.get_file(doc.file_id)
        if name.endswith(".zip"):
            zip_tmp = tmp_path + ".zip"
            await bot.download_file(file.file_path, destination=zip_tmp)
            try:
                await asyncio.to_thread(backup_service.extract_db_from_zip, zip_tmp, tmp_path)
            finally:
                try:
                    os.remove(zip_tmp)
                except OSError:
                    pass
        else:
            await bot.download_file(file.file_path, destination=tmp_path)
    except Exception as e:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        await status_msg.edit_text(
            f"❌ Faylni o'qib bo'lmadi: {html.escape(str(e)[:200], quote=False)}\n\n"
            "Boshqa fayl yuboring yoki ❌ Bekor qilish tugmasini bosing."
        )
        return

    ok, err = await validate_sqlite_backup(tmp_path)
    if not ok:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        await status_msg.edit_text(
            f"❌ Fayl yaroqsiz: {err}\n\nBoshqa fayl yuboring yoki ❌ Bekor qilish tugmasini bosing."
        )
        return

    await state.update_data(restore_tmp_path=tmp_path)
    await status_msg.edit_text(
        "✅ Fayl to'g'ri SQLite baza ekan.\n\n"
        "‼️ <b>So'nggi tasdiq</b>: hozirgi baza avtomatik zaxiralab qo'yiladi, "
        "so'ngra u shu fayl bilan to'liq almashtiriladi. Davom etasizmi?"
    )
    await message.answer("Tanlang:", reply_markup=kb.confirm_restore_keyboard())


@router.message(AdminTextStates.waiting_backup_file)
async def backup_import_wrong_type(message: Message):
    await message.answer(
        "📥 Iltimos zaxira faylini (<code>.zip</code> yoki <code>.db</code>) hujjat (document) sifatida yuboring "
        "(rasm yoki matn emas)."
    )


@router.callback_query(AdminTextStates.waiting_backup_file, F.data == "adm:backup_import_confirm")
async def backup_import_confirm(call: CallbackQuery, state: FSMContext, db: Database):
    data = await state.get_data()
    tmp_path = data.get("restore_tmp_path")
    await state.clear()

    if not tmp_path or not os.path.exists(tmp_path):
        await call.answer("❗ Fayl topilmadi, jarayonni qaytadan boshlang.", show_alert=True)
        return

    await call.answer()
    await call.message.answer("⏳ Baza tiklanmoqda, biroz kuting...")
    try:
        await db.backup()  # joriy holatni xavfsizlik uchun avval zaxiralaymiz
        await db.replace_with(tmp_path)
    except Exception as e:
        logger.exception("Bazani tiklashda xatolik")
        await call.message.answer(f"❌ Tiklashda xatolik yuz berdi: {e}")
        return
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    await call.message.answer(
        "✅ Baza muvaffaqiyatli tiklandi!",
        reply_markup=kb.admin_panel_menu(),
    )


@router.callback_query(F.data == "adm:backup_import_cancel")
async def backup_import_cancel_cb(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    tmp_path = data.get("restore_tmp_path")
    await state.clear()
    if tmp_path and os.path.exists(tmp_path):
        try:
            os.remove(tmp_path)
        except OSError:
            pass
    await call.answer("Bekor qilindi.", show_alert=True)
    await call.message.answer("⚙️ Admin panel", reply_markup=kb.admin_panel_menu())


# ------------------------------------------------------------------ #
# Broadcast
# ------------------------------------------------------------------ #
@router.callback_query(F.data == "adm:broadcast")
async def broadcast_start(call: CallbackQuery, state: FSMContext, db: Database):
    # Agar hozir faol (yoki pauzadagi) reklama bo'lsa — yangisini so'ramay,
    # o'shaning holatini (⏸/▶️ tugmalari bilan) ko'rsatamiz.
    active = await db.get_latest_active_broadcast()
    if active:
        paused = active["status"] == "paused"
        status_txt = "⏸ To'xtatilgan" if paused else "▶️ Yuborilmoqda"
        text = (
            f"📢 <b>Reklama</b>\n\n"
            f"Holati: {status_txt}\n"
            f"Yuborildi: {active['sent_count'] + active['failed_count']}/{active['total_count']} "
            f"(✅ {active['sent_count']} | ❌ {active['failed_count']})"
        )
        await call.message.answer(text, reply_markup=kb.broadcast_control_keyboard(active["id"], paused))
        await call.answer()
        return

    await state.set_state(BroadcastStates.waiting_content)
    await call.message.answer(
        "📢 Barcha foydalanuvchilarga yuboriladigan xabarni yuboring "
        "(matn, rasm, video — istalgan turdagi xabar bo'lishi mumkin):",
        reply_markup=kb.cancel_menu(),
    )
    await call.answer()


@router.message(BroadcastStates.waiting_content)
async def broadcast_receive(message: Message, state: FSMContext, db: Database, bot: Bot):
    await state.clear()
    user = await ensure_user(db, message.from_user)
    ids = await db.all_telegram_ids()
    broadcast_id = await db.create_broadcast(
        content_type="copy",
        payload=json.dumps({"chat_id": message.chat.id, "message_id": message.message_id}),
        total=len(ids),
        created_by=message.from_user.id,
    )
    _broadcast_flags[broadcast_id] = {"paused": False}
    status_msg = await message.answer(
        f"📢 Yuborish boshlandi: 0/{len(ids)}",
        reply_markup=kb.broadcast_control_keyboard(broadcast_id, paused=False),
    )
    await message.answer("⚙️ Admin panel:", reply_markup=kb.admin_panel_menu())
    asyncio.create_task(_run_broadcast(bot, db, broadcast_id, ids, message.chat.id,
                                        message.message_id, status_msg.chat.id, status_msg.message_id))


async def _run_broadcast(bot: Bot, db: Database, broadcast_id: int, user_ids: list[int],
                          src_chat_id: int, src_message_id: int, status_chat_id: int, status_message_id: int):
    sent, failed = 0, 0
    for uid in user_ids:
        flags = _broadcast_flags.get(broadcast_id, {})
        while flags.get("paused"):
            await asyncio.sleep(2)
            flags = _broadcast_flags.get(broadcast_id, {})
        try:
            await bot.copy_message(chat_id=uid, from_chat_id=src_chat_id, message_id=src_message_id)
            sent += 1
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            try:
                await bot.copy_message(chat_id=uid, from_chat_id=src_chat_id, message_id=src_message_id)
                sent += 1
            except Exception:
                failed += 1
        except (TelegramForbiddenError, TelegramBadRequest):
            failed += 1
        except Exception:
            failed += 1
        await db.update_broadcast_progress(broadcast_id, sent, failed)
        if (sent + failed) % 25 == 0:
            try:
                await bot.edit_message_text(
                    f"📢 Yuborilmoqda: {sent + failed}/{len(user_ids)} "
                    f"(✅ {sent} | ❌ {failed})",
                    chat_id=status_chat_id, message_id=status_message_id,
                    reply_markup=kb.broadcast_control_keyboard(broadcast_id, paused=False),
                )
            except Exception:
                pass
        await asyncio.sleep(0.05)  # Telegram limitlariga hurmat

    await db.set_broadcast_status(broadcast_id, "done")
    _broadcast_flags.pop(broadcast_id, None)
    try:
        await bot.edit_message_text(
            f"✅ Yuborish tugadi: {sent + failed}/{len(user_ids)} (✅ {sent} | ❌ {failed})",
            chat_id=status_chat_id, message_id=status_message_id,
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("adm:bc_pause:"))
async def bc_pause(call: CallbackQuery, db: Database):
    broadcast_id = int(call.data.split(":")[2])
    _broadcast_flags.setdefault(broadcast_id, {})["paused"] = True
    await db.set_broadcast_status(broadcast_id, "paused")
    try:
        await call.message.edit_reply_markup(reply_markup=kb.broadcast_control_keyboard(broadcast_id, paused=True))
    except TelegramBadRequest:
        pass
    await call.answer("⏸ To'xtatildi.")


@router.callback_query(F.data.startswith("adm:bc_resume:"))
async def bc_resume(call: CallbackQuery, db: Database):
    broadcast_id = int(call.data.split(":")[2])
    _broadcast_flags.setdefault(broadcast_id, {})["paused"] = False
    await db.set_broadcast_status(broadcast_id, "running")
    try:
        await call.message.edit_reply_markup(reply_markup=kb.broadcast_control_keyboard(broadcast_id, paused=False))
    except TelegramBadRequest:
        pass
    await call.answer("▶️ Davom ettirildi.")
