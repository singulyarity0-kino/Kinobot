"""
Admin panelning qo'shimcha bo'limlari:

  1) Kino qo'shish/tahrirlashda davlat, til (ovoz), subtitr va yilni INLINE TUGMA bilan tanlash
  2) Kino posterini o'chirmasdan almashtirish
  3) Janrlarni boshqarish (yangi janr, nomi/emoji, oddiy/VIP, o'chirish)
  4) Kunlik tasodifiy kino e'lonining vaqtini sozlash

Bu router admin.router dan KEYIN ulanadi (main.py / main_polling.py), shuning uchun
admin.py dagi global "❌ Bekor qilish", /cancel, /start handlerlari bu yerdagi
har qanday holatdan ustun turadi.
"""
from __future__ import annotations

import datetime
import logging
import random
from typing import Optional

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import backup_service
import config
import keyboards as kb
from database import Database
from handlers.common import IsAdminFilter, build_admin_anime_text, parse_hhmm
from states import DailyPostStates, EditAnimeStates, GenreStates, OptionStates

logger = logging.getLogger("anime_bot.admin_more")
router = Router(name="admin_more")
router.message.filter(IsAdminFilter())
router.callback_query.filter(IsAdminFilter())

BAD_CHARS = ("<", ">", "&")


async def _edit_or_send(call: CallbackQuery, text: str, markup) -> None:
    try:
        await call.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        await call.message.answer(text, reply_markup=markup)


async def _send_admin_anime_card(bot: Bot, chat_id: int, db: Database, anime_id: int) -> None:
    text = await build_admin_anime_text(db, anime_id)
    if not text:
        return
    seasons = await db.list_seasons(anime_id)
    await bot.send_message(chat_id, text, reply_markup=kb.admin_anime_detail_keyboard(anime_id, bool(seasons)))


# ================================================================== #
# 1) Davlat / til / subtitr / yilni inline tugma bilan tanlash
# ================================================================== #
def _year_options() -> list[tuple[str, str]]:
    y = datetime.date.today().year
    return [("", str(v)) for v in range(y, y - 8, -1)]


KINDS: dict[str, dict] = {
    "country": {
        "multi": False, "per_row": 2, "field": "country", "label": "🌍 Davlat",
        "prompt": "🌍 Kino qaysi davlatniki? Tanlang:",
        "other_prompt": "✍️ Davlat nomini yozing (masalan: Tailand):",
    },
    "audio": {
        "multi": True, "per_row": 2, "field": "language", "label": "🎙 Til",
        "prompt": "🎙 Kino qaysi tilda (ovoz)? Bir nechtasini belgilashingiz mumkin, so'ng «✅ Tayyor» bosing:",
        "other_prompt": "✍️ Tilni yozing (bir nechta bo'lsa vergul bilan: Turkcha, Hind):",
    },
    "subs": {
        "multi": True, "per_row": 2, "field": "subtitles", "label": "💬 Subtitr",
        "prompt": "💬 Subtitr tilini tanlang (bir nechtasini belgilash mumkin). Subtitr yo'q bo'lsa «O'tkazib yuborish»:",
        "other_prompt": "✍️ Subtitr tilini yozing (bir nechta bo'lsa vergul bilan):",
    },
    "year": {
        "multi": False, "per_row": 4, "field": "release_year", "label": "📅 Yil",
        "prompt": "📅 Kino qaysi yilda chiqqan? Tanlang:",
        "other_prompt": "✍️ Yilni yozing (masalan: 2009 yoki 2013–2023):",
    },
}
_EDIT_KIND = {"country": "country", "year": "year", "language": "audio", "subs": "subs"}


def _options(kind: str) -> list[tuple[str, str]]:
    if kind == "country":
        return config.COUNTRY_OPTIONS
    if kind == "audio":
        return config.AUDIO_LANG_OPTIONS
    if kind == "subs":
        return config.SUB_LANG_OPTIONS
    return _year_options()


async def _show_picker(bot: Bot, chat_id: int, state: FSMContext, kind: str,
                       preselect: list[str], current_note: str = "") -> None:
    cfg = KINDS[kind]
    text = cfg["prompt"]
    if current_note:
        text += f"\n\nHozirgi qiymat: <b>{current_note}</b>"
    markup = kb.option_picker_keyboard(_options(kind), preselect, cfg["multi"], cfg["per_row"])
    msg = await bot.send_message(chat_id, text, reply_markup=markup)
    await state.update_data(opt_kind=kind, opt_sel=list(preselect), opt_msg_id=msg.message_id)
    await state.set_state(OptionStates.picking)


async def _refresh_picker(bot: Bot, chat_id: int, state: FSMContext) -> None:
    data = await state.get_data()
    kind = data["opt_kind"]
    cfg = KINDS[kind]
    markup = kb.option_picker_keyboard(_options(kind), data.get("opt_sel", []), cfg["multi"], cfg["per_row"])
    try:
        await bot.edit_message_reply_markup(chat_id=chat_id, message_id=data["opt_msg_id"], reply_markup=markup)
    except TelegramBadRequest:
        pass


async def _close_picker(bot: Bot, chat_id: int, state: FSMContext, summary: str) -> None:
    data = await state.get_data()
    try:
        await bot.edit_message_text(summary, chat_id=chat_id, message_id=data["opt_msg_id"])
    except TelegramBadRequest:
        pass


async def begin_add_options(bot: Bot, chat_id: int, state: FSMContext) -> None:
    """Yangi kino qo'shishda janrlar tanlangach chaqiriladi: davlat -> til -> subtitr -> yil."""
    # TMDB'dan davlat va yil olingan bo'lsa, ularni qayta so'ramaymiz
    data = await state.get_data()
    prefilled: dict[str, str] = {}
    if data.get("al_country"):
        prefilled["country"] = data["al_country"]
    if data.get("al_year"):
        prefilled["year"] = data["al_year"]
    order = [k for k in ("country", "audio", "subs", "year") if k not in prefilled]
    first = order.pop(0)
    await state.update_data(opt_mode="add", opt_vals=prefilled, opt_seq=order)
    await _show_picker(bot, chat_id, state, first, [])


async def _advance(bot: Bot, chat_id: int, state: FSMContext, db: Database, value: str) -> None:
    data = await state.get_data()
    kind = data["opt_kind"]
    vals = dict(data.get("opt_vals", {}))
    vals[kind] = value
    await state.update_data(opt_vals=vals)
    await _close_picker(bot, chat_id, state, f"{KINDS[kind]['label']}: {value or '—'}")
    seq = list(data.get("opt_seq", []))
    if seq:
        nxt = seq.pop(0)
        await state.update_data(opt_seq=seq)
        await _show_picker(bot, chat_id, state, nxt, [])
    else:
        await _finish(bot, chat_id, state, db)


async def _finish(bot: Bot, chat_id: int, state: FSMContext, db: Database) -> None:
    data = await state.get_data()
    mode = data.get("opt_mode")
    vals = data.get("opt_vals", {})
    if mode == "add":
        anime = await db.create_anime(
            data["new_title"],
            data.get("new_description") or "",
            data.get("new_poster"),
            data.get("selected_genres", []),
            country=vals.get("country") or None,
            release_year=vals.get("year") or None,
            language=vals.get("audio", ""),
            subtitles=vals.get("subs") or None,
            anilist_id=data.get("anilist_id") or None,
        )
        extra = data.get("al_extra") or {}
        for field in ("kind", "tmdb_id", "duration", "age_rating", "imdb_rating", "director", "actors", "trailer_url"):
            if extra.get(field):
                await db.update_anime_field(anime["id"], field, str(extra[field]))
        anime = await db.get_anime(anime["id"])
        await state.clear()
        backup_service.request_backup("kino qo'shildi")
        vip_note = " (💎 VIP kino deb belgilandi)" if anime["is_vip"] else ""
        await bot.send_message(
            chat_id,
            f"✅ «{anime['title']}» qo'shildi! ID: <code>{anime['anime_code']}</code>{vip_note}\n\n"
            f"Endi qismlarni (video) qo'shishingiz mumkin.",
            reply_markup=kb.main_menu(is_admin=True),
        )
        await _send_admin_anime_card(bot, chat_id, db, anime["id"])
        return

    # edit rejimi: bitta maydon yangilanadi
    anime_id = data["edit_anime_id"]
    kind = next(iter(vals))
    cfg = KINDS[kind]
    await db.update_anime_field(anime_id, cfg["field"], vals[kind] or None)
    await state.clear()
    await bot.send_message(chat_id, f"✅ {cfg['label']} yangilandi.", reply_markup=kb.main_menu(is_admin=True))
    await _send_admin_anime_card(bot, chat_id, db, anime_id)


@router.callback_query(F.data.regexp(r"^adm:edit_(country|year|language|subs):\d+$"))
async def edit_option_start(call: CallbackQuery, state: FSMContext, db: Database, bot: Bot):
    _, what, id_str = call.data.split(":")
    kind = _EDIT_KIND[what.replace("edit_", "")]
    anime = await db.get_anime(int(id_str))
    if not anime:
        await call.answer("Kino topilmadi.", show_alert=True)
        return
    cfg = KINDS[kind]
    current = (anime[cfg["field"]] or "") if cfg["field"] in anime.keys() else ""
    preselect = [p.strip() for p in current.split(",") if p.strip()] if cfg["multi"] else []
    await state.clear()
    await state.update_data(opt_mode="edit", edit_anime_id=anime["id"], opt_vals={}, opt_seq=[])
    await _show_picker(bot, call.message.chat.id, state, kind, preselect, current)
    await call.answer()


@router.callback_query(OptionStates.picking, F.data.startswith("opt:"))
async def option_callback(call: CallbackQuery, state: FSMContext, db: Database, bot: Bot):
    parts = call.data.split(":")
    action = parts[1]
    chat_id = call.message.chat.id
    data = await state.get_data()
    kind = data.get("opt_kind")
    if not kind:
        await call.answer("Bu tugma eskirgan.", show_alert=True)
        return
    cfg = KINDS[kind]
    options = _options(kind)
    sel: list[str] = list(data.get("opt_sel", []))

    if action == "pick":
        idx = int(parts[2])
        if idx >= len(options):
            await call.answer()
            return
        await call.answer()
        await _advance(bot, chat_id, state, db, options[idx][1])
    elif action == "tog":
        idx = int(parts[2])
        if idx >= len(options):
            await call.answer()
            return
        name = options[idx][1]
        low = [s.lower() for s in sel]
        if name.lower() in low:
            sel.pop(low.index(name.lower()))
        else:
            sel.append(name)
        await state.update_data(opt_sel=sel)
        await _refresh_picker(bot, chat_id, state)
        await call.answer()
    elif action == "rm":
        preset_lower = {n.lower() for _, n in options}
        customs = [s for s in sel if s.lower() not in preset_lower]
        j = int(parts[2])
        if j < len(customs):
            sel.remove(customs[j])
            await state.update_data(opt_sel=sel)
            await _refresh_picker(bot, chat_id, state)
        await call.answer()
    elif action == "done":
        await call.answer()
        await _advance(bot, chat_id, state, db, ", ".join(sel))
    elif action == "skip":
        await call.answer()
        await _advance(bot, chat_id, state, db, "")
    elif action == "other":
        await state.set_state(OptionStates.waiting_custom)
        await call.message.answer(cfg["other_prompt"], reply_markup=kb.cancel_menu())
        await call.answer()
    elif action == "cancel":
        await _close_picker(bot, chat_id, state, "❌ Bekor qilindi.")
        await state.clear()
        await call.message.answer("❌ Bekor qilindi.", reply_markup=kb.main_menu(is_admin=True))
        await call.message.answer("⚙️ Admin panel:", reply_markup=kb.admin_panel_menu())
        await call.answer()
    else:
        await call.answer()


@router.message(OptionStates.waiting_custom, F.text)
async def option_custom_text(message: Message, state: FSMContext, db: Database, bot: Bot):
    text = (message.text or "").strip()
    if not text or len(text) > 60 or any(c in text for c in BAD_CHARS):
        await message.answer("❗ 1–60 belgidan iborat matn yuboring (<, > va & belgilarisiz).")
        return
    data = await state.get_data()
    kind = data["opt_kind"]
    cfg = KINDS[kind]
    chat_id = message.chat.id
    await state.set_state(OptionStates.picking)
    if cfg["multi"]:
        sel = list(data.get("opt_sel", []))
        have = {s.lower() for s in sel}
        for piece in [p.strip() for p in text.split(",") if p.strip()]:
            if piece.lower() not in have:
                sel.append(piece)
                have.add(piece.lower())
        await state.update_data(opt_sel=sel)
        await message.answer(
            "✅ Qo'shildi. Yana tanlang yoki «✅ Tayyor» tugmasini bosing.",
            reply_markup=kb.main_menu(is_admin=True),
        )
        await _refresh_picker(bot, chat_id, state)
    else:
        await message.answer(f"✅ {text}", reply_markup=kb.main_menu(is_admin=True))
        await _advance(bot, chat_id, state, db, text)


@router.message(OptionStates.waiting_custom)
async def option_custom_wrong(message: Message):
    await message.answer("✍️ Iltimos, matn yuboring yoki «❌ Bekor qilish» tugmasini bosing.")


@router.callback_query(OptionStates.waiting_custom, F.data.startswith("opt:"))
async def option_callback_while_typing(call: CallbackQuery):
    await call.answer("Avval matnni yozing yoki «❌ Bekor qilish» tugmasini bosing.", show_alert=True)


@router.message(OptionStates.picking)
async def option_picking_text(message: Message):
    await message.answer("👆 Iltimos, yuqoridagi tugmalardan birini tanlang.")


@router.callback_query(F.data.startswith("opt:"))
async def option_stale(call: CallbackQuery):
    await call.answer("Bu tugma eskirgan. Jarayonni qaytadan boshlang.", show_alert=True)


# ================================================================== #
# 2) Poster tahrirlash
# ================================================================== #
@router.callback_query(F.data.startswith("adm:edit_poster:"))
async def edit_poster_start(call: CallbackQuery, state: FSMContext, db: Database):
    anime_id = int(call.data.split(":")[2])
    if not await db.get_anime(anime_id):
        await call.answer("Kino topilmadi.", show_alert=True)
        return
    await state.clear()
    await state.update_data(edit_anime_id=anime_id)
    await state.set_state(EditAnimeStates.waiting_new_poster)
    await call.message.answer(
        "🖼 Yangi poster rasmini yuboring.\n\nPosterni butunlay olib tashlash uchun «-» deb yozing.",
        reply_markup=kb.cancel_menu(),
    )
    await call.answer()


async def _poster_done(message: Message, state: FSMContext, db: Database, bot: Bot, new_value: Optional[str]) -> None:
    data = await state.get_data()
    anime_id = data["edit_anime_id"]
    await db.update_anime_field(anime_id, "poster_file_id", new_value)
    await state.clear()
    await message.answer(
        "✅ Poster yangilandi." if new_value else "✅ Poster olib tashlandi.",
        reply_markup=kb.main_menu(is_admin=True),
    )
    await message.answer(
        "ℹ️ Kanalga oldin yuborilgan e'lon o'zgarmaydi. Yangi poster bilan qayta e'lon qilish uchun "
        "kino sahifasidagi «📢 Kanalga e'lon qilish» tugmasidan foydalaning."
    )
    await _send_admin_anime_card(bot, message.chat.id, db, anime_id)


@router.message(EditAnimeStates.waiting_new_poster, F.photo)
async def edit_poster_photo(message: Message, state: FSMContext, db: Database, bot: Bot):
    await _poster_done(message, state, db, bot, message.photo[-1].file_id)


@router.message(EditAnimeStates.waiting_new_poster, F.text == "-")
async def edit_poster_remove(message: Message, state: FSMContext, db: Database, bot: Bot):
    await _poster_done(message, state, db, bot, None)


@router.message(EditAnimeStates.waiting_new_poster)
async def edit_poster_wrong(message: Message):
    await message.answer("🖼 Rasm yuboring (yoki olib tashlash uchun «-» yozing).")


# ================================================================== #
# 3) Janrlarni boshqarish
# ================================================================== #
async def _show_genres(call: CallbackQuery, db: Database) -> None:
    genres = await db.list_genres()
    text = (
        f"🎭 <b>Janrlar</b> ({len(genres)} ta)\n\n"
        "💎 — VIP janr. Janrni bossangiz, nomini yoki emojisini o'zgartirish, "
        "VIP/oddiy qilish yoki o'chirish mumkin."
    )
    await _edit_or_send(call, text, kb.admin_genres_keyboard(genres))


def _genre_card(genre, anime_count: int) -> str:
    status = "💎 VIP janr" if genre["is_vip"] else "🆓 Oddiy janr"
    return (
        f"{genre['emoji']} <b>{genre['name']}</b>\n"
        f"Holati: {status}\n"
        f"Shu janrdagi kinolar: {anime_count} ta"
    )


async def _send_genre_card(bot: Bot, chat_id: int, db: Database, genre_id: int) -> None:
    genre = await db.get_genre(genre_id)
    if not genre:
        return
    count = await db.count_anime_in_genre(genre_id)
    await bot.send_message(
        chat_id, _genre_card(genre, count),
        reply_markup=kb.admin_genre_detail_keyboard(genre_id, bool(genre["is_vip"])),
    )


@router.callback_query(F.data.in_({"adm:genres", "gen:list"}))
async def genres_list(call: CallbackQuery, state: FSMContext, db: Database):
    await state.clear()
    await _show_genres(call, db)
    await call.answer()


@router.callback_query(F.data.startswith("gen:open:"))
async def genre_open(call: CallbackQuery, state: FSMContext, db: Database):
    await state.clear()
    genre = await db.get_genre(int(call.data.split(":")[2]))
    if not genre:
        await call.answer("Janr topilmadi.", show_alert=True)
        return
    count = await db.count_anime_in_genre(genre["id"])
    await _edit_or_send(call, _genre_card(genre, count),
                        kb.admin_genre_detail_keyboard(genre["id"], bool(genre["is_vip"])))
    await call.answer()


# ---- nomini o'zgartirish ----
@router.callback_query(F.data.startswith("gen:name:"))
async def genre_name_start(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.update_data(gen_id=int(call.data.split(":")[2]))
    await state.set_state(GenreStates.waiting_edit_name)
    await call.message.answer("✏️ Janrning yangi nomini yozing:", reply_markup=kb.cancel_menu())
    await call.answer()


async def _valid_genre_name(message: Message, db: Database, exclude_id: Optional[int]) -> Optional[str]:
    name = (message.text or "").strip()
    if not name or len(name) > 30 or any(c in name for c in BAD_CHARS):
        await message.answer("❗ Nom 1–30 belgidan iborat bo'lsin (<, > va & belgilarisiz).")
        return None
    if await db.genre_name_exists(name, exclude_id):
        await message.answer("❗ Bunday nomli janr allaqachon bor. Boshqa nom yozing.")
        return None
    return name


@router.message(GenreStates.waiting_edit_name, F.text)
async def genre_name_save(message: Message, state: FSMContext, db: Database, bot: Bot):
    data = await state.get_data()
    gid = data["gen_id"]
    name = await _valid_genre_name(message, db, gid)
    if not name:
        return
    await db.update_genre(gid, name=name)
    await state.clear()
    await message.answer("✅ Janr nomi yangilandi.", reply_markup=kb.main_menu(is_admin=True))
    await _send_genre_card(bot, message.chat.id, db, gid)


# ---- emojisini o'zgartirish ----
@router.callback_query(F.data.startswith("gen:emoji:"))
async def genre_emoji_start(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.update_data(gen_id=int(call.data.split(":")[2]))
    await state.set_state(GenreStates.waiting_edit_emoji)
    await call.message.answer("😀 Janr uchun yangi emoji yuboring (masalan: 🔥):", reply_markup=kb.cancel_menu())
    await call.answer()


@router.message(GenreStates.waiting_edit_emoji, F.text)
async def genre_emoji_save(message: Message, state: FSMContext, db: Database, bot: Bot):
    emoji = (message.text or "").strip()
    if not emoji or len(emoji) > 8 or any(c in emoji for c in BAD_CHARS):
        await message.answer("❗ Bitta emoji yuboring.")
        return
    data = await state.get_data()
    gid = data["gen_id"]
    await db.update_genre(gid, emoji=emoji)
    await state.clear()
    await message.answer("✅ Emoji yangilandi.", reply_markup=kb.main_menu(is_admin=True))
    await _send_genre_card(bot, message.chat.id, db, gid)


# ---- VIP / oddiy ----
@router.callback_query(F.data.startswith("gen:vip:"))
async def genre_vip_ask(call: CallbackQuery, db: Database):
    gid = int(call.data.split(":")[2])
    genre = await db.get_genre(gid)
    if not genre:
        await call.answer("Janr topilmadi.", show_alert=True)
        return
    make_vip = not genre["is_vip"]
    affected = await db.genre_vip_change_preview(gid, make_vip)
    total = await db.count_anime_in_genre(gid)
    if make_vip:
        text = (
            f"💎 «{genre['name']}» janrini <b>VIP</b> qilmoqchimisiz?\n\n"
            f"Shu janrdagi {total} ta kinodan {affected} tasi avtomatik <b>VIP</b> bo'ladi va "
            f"oddiy foydalanuvchilarga yopiladi."
        )
    else:
        text = (
            f"🆓 «{genre['name']}» janrini <b>oddiy</b> qilmoqchimisiz?\n\n"
            f"Shu janrdagi {total} ta kinodan {affected} tasi VIP'likdan chiqadi "
            f"(boshqa VIP janri borlari VIP bo'lib qoladi)."
        )
    await _edit_or_send(
        call, text,
        kb.admin_genre_confirm_keyboard(f"gen:vipok:{gid}:{1 if make_vip else 0}", f"gen:open:{gid}"),
    )
    await call.answer()


@router.callback_query(F.data.startswith("gen:vipok:"))
async def genre_vip_apply(call: CallbackQuery, db: Database):
    _, _, gid_s, target_s = call.data.split(":")
    gid, target = int(gid_s), target_s == "1"
    genre = await db.get_genre(gid)
    if not genre:
        await call.answer("Janr topilmadi.", show_alert=True)
        return
    if bool(genre["is_vip"]) != target:
        await db.set_genre_vip(gid, target)
    genre = await db.get_genre(gid)
    count = await db.count_anime_in_genre(gid)
    await _edit_or_send(call, "✅ Saqlandi.\n\n" + _genre_card(genre, count),
                        kb.admin_genre_detail_keyboard(gid, bool(genre["is_vip"])))
    await call.answer("Saqlandi")


# ---- o'chirish ----
@router.callback_query(F.data.startswith("gen:del:"))
async def genre_delete_ask(call: CallbackQuery, db: Database):
    gid = int(call.data.split(":")[2])
    genre = await db.get_genre(gid)
    if not genre:
        await call.answer("Janr topilmadi.", show_alert=True)
        return
    total = await db.count_anime_in_genre(gid)
    text = (
        f"🗑 «{genre['name']}» janrini o'chirmoqchimisiz?\n\n"
        f"Shu janrdagi {total} ta kinoning faqat shu janr belgisi olib tashlanadi, "
        f"kinolarning o'zi o'chmaydi."
    )
    if genre["is_vip"]:
        lose = await db.genre_vip_change_preview(gid, False)
        text += f"\nShundan {lose} tasi VIP'likdan chiqadi (boshqa VIP janri yo'qlari)."
    await _edit_or_send(call, text, kb.admin_genre_confirm_keyboard(f"gen:delok:{gid}", f"gen:open:{gid}"))
    await call.answer()


@router.callback_query(F.data.startswith("gen:delok:"))
async def genre_delete_apply(call: CallbackQuery, db: Database):
    gid = int(call.data.split(":")[2])
    if await db.get_genre(gid):
        await db.delete_genre(gid)
    await call.answer("O'chirildi")
    await _show_genres(call, db)


# ---- yangi janr ----
@router.callback_query(F.data == "gen:new")
async def genre_new_start(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(GenreStates.waiting_new_name)
    await call.message.answer("➕ Yangi janr nomini yozing (masalan: Isekai):", reply_markup=kb.cancel_menu())
    await call.answer()


@router.message(GenreStates.waiting_new_name, F.text)
async def genre_new_name(message: Message, state: FSMContext, db: Database):
    name = await _valid_genre_name(message, db, None)
    if not name:
        return
    await state.update_data(gen_name=name)
    await state.set_state(GenreStates.waiting_new_emoji)
    await message.answer("😀 Janr uchun emoji yuboring (yoki «-» deb yozing, standart emoji qo'yiladi):")


@router.message(GenreStates.waiting_new_emoji, F.text)
async def genre_new_emoji(message: Message, state: FSMContext):
    emoji = (message.text or "").strip()
    if emoji == "-":
        emoji = ""
    elif len(emoji) > 8 or any(c in emoji for c in BAD_CHARS):
        await message.answer("❗ Bitta emoji yuboring (yoki «-»).")
        return
    await state.update_data(gen_emoji=emoji)
    await state.set_state(None)  # ma'lumot saqlanadi, keyingi bosqich — inline tugma
    data = await state.get_data()
    await message.answer(
        f"«{data['gen_name']}» janri qanday bo'lsin?",
        reply_markup=kb.main_menu(is_admin=True),
    )
    await message.answer("Turini tanlang:", reply_markup=kb.admin_genre_type_keyboard())


@router.message(GenreStates.waiting_edit_name)
@router.message(GenreStates.waiting_edit_emoji)
@router.message(GenreStates.waiting_new_name)
@router.message(GenreStates.waiting_new_emoji)
async def genre_text_wrong(message: Message):
    await message.answer("✍️ Iltimos, matn yuboring yoki «❌ Bekor qilish» tugmasini bosing.")


@router.callback_query(F.data.startswith("gen:newtype:"))
async def genre_new_type(call: CallbackQuery, state: FSMContext, db: Database, bot: Bot):
    data = await state.get_data()
    name = data.get("gen_name")
    if not name:
        await call.answer("Bu tugma eskirgan. Qaytadan boshlang.", show_alert=True)
        return
    is_vip = call.data.split(":")[2] == "1"
    emoji = data.get("gen_emoji") or ("💎" if is_vip else "🎬")
    if await db.genre_name_exists(name):
        await state.clear()
        await call.answer("Bunday nomli janr allaqachon bor.", show_alert=True)
        return
    genre = await db.create_genre(name, emoji, is_vip)
    await state.clear()
    await call.answer("Qo'shildi")
    try:
        await call.message.edit_text(f"✅ «{genre['name']}» janri qo'shildi ({'💎 VIP' if is_vip else '🆓 oddiy'}).")
    except TelegramBadRequest:
        pass
    await _send_genre_card(bot, call.message.chat.id, db, genre["id"])


# ================================================================== #
# 4) Kunlik tasodifiy anime e'loni sozlamalari
# ================================================================== #
async def _daily_info(db: Database) -> tuple[bool, str, bool]:
    enabled = (await db.get_setting("daily_post_enabled", "1")) == "1"
    t = await db.get_setting("daily_post_time", "12:00")
    last = await db.get_setting("daily_post_last_date", "")
    today = datetime.datetime.now(config.LOCAL_TZ).strftime("%Y-%m-%d")
    return enabled, t, last == today


def _daily_text(enabled: bool, t: str, done_today: bool) -> str:
    now = datetime.datetime.now(config.LOCAL_TZ).strftime("%H:%M")
    status = "✅ Yoqilgan" if enabled else "⏸ O'chirilgan"
    lines = [
        "🕐 <b>Kunlik tasodifiy kino e'loni</b>",
        "",
        f"Holati: {status}",
        f"Vaqti: <b>{t}</b> (O'zbekiston vaqti)",
        f"Hozir: {now}",
    ]
    if enabled:
        lines.append("Bugungi e'lon: " + ("yuborilgan ✅" if done_today else "hali yuborilmagan"))
    if not config.MAIN_CHANNEL:
        lines += ["", "⚠️ MAIN_CHANNEL sozlanmagan — e'lon yuborilmaydi."]
    lines += ["", "Belgilangan vaqtda kanalga tasodifiy kino e'loni yuboriladi. Kuniga bir marta."]
    return "\n".join(lines)


async def _daily_markup_and_text(db: Database):
    enabled, t, done = await _daily_info(db)
    return _daily_text(enabled, t, done), kb.admin_daily_keyboard(enabled, t)


async def _sync_last_date(db: Database, hhmm: str) -> None:
    """Yangi vaqt allaqachon o'tib ketgan bo'lsa — bugungi e'lon yuborilgan deb belgilaymiz (ertaga yuboriladi)."""
    now = datetime.datetime.now(config.LOCAL_TZ)
    passed = now.strftime("%H:%M") >= hhmm
    await db.set_setting("daily_post_last_date", now.strftime("%Y-%m-%d") if passed else "")


@router.callback_query(F.data == "adm:daily")
async def daily_open(call: CallbackQuery, state: FSMContext, db: Database):
    await state.clear()
    text, markup = await _daily_markup_and_text(db)
    await _edit_or_send(call, text, markup)
    await call.answer()


@router.callback_query(F.data == "dp:toggle")
async def daily_toggle(call: CallbackQuery, db: Database):
    enabled, t, _ = await _daily_info(db)
    new_enabled = not enabled
    await db.set_setting("daily_post_enabled", "1" if new_enabled else "0")
    if new_enabled:
        await _sync_last_date(db, t)
    text, markup = await _daily_markup_and_text(db)
    await _edit_or_send(call, text, markup)
    await call.answer("Yoqildi" if new_enabled else "O'chirildi")


@router.callback_query(F.data.startswith("dp:time:"))
async def daily_set_time(call: CallbackQuery, db: Database):
    hhmm = parse_hhmm(call.data[len("dp:time:"):])
    if not hhmm:
        await call.answer("Noto'g'ri vaqt.", show_alert=True)
        return
    await db.set_setting("daily_post_time", hhmm)
    await _sync_last_date(db, hhmm)
    text, markup = await _daily_markup_and_text(db)
    await _edit_or_send(call, text, markup)
    await call.answer(f"Vaqt: {hhmm}")


@router.callback_query(F.data == "dp:custom")
async def daily_custom_start(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(DailyPostStates.waiting_time)
    await call.message.answer(
        "✍️ Vaqtni yozing (O'zbekiston vaqti bilan), masalan: <code>19:30</code>",
        reply_markup=kb.cancel_menu(),
    )
    await call.answer()


@router.message(DailyPostStates.waiting_time, F.text)
async def daily_custom_save(message: Message, state: FSMContext, db: Database):
    hhmm = parse_hhmm(message.text or "")
    if not hhmm:
        await message.answer("❗ Vaqtni <code>19:30</code> ko'rinishida yozing (soat 0–23, daqiqa 0–59).")
        return
    await db.set_setting("daily_post_time", hhmm)
    await _sync_last_date(db, hhmm)
    await state.clear()
    await message.answer(f"✅ Kunlik e'lon vaqti: {hhmm}", reply_markup=kb.main_menu(is_admin=True))
    text, markup = await _daily_markup_and_text(db)
    await message.answer(text, reply_markup=markup)


@router.message(DailyPostStates.waiting_time)
async def daily_custom_wrong(message: Message):
    await message.answer("✍️ Iltimos, vaqtni matn ko'rinishida yozing (masalan: 19:30).")


@router.callback_query(F.data == "dp:test")
async def daily_test(call: CallbackQuery, db: Database, bot: Bot):
    from handlers.admin import _post_announcement  # kech import: admin.py bu modulni import qiladi

    if not config.MAIN_CHANNEL:
        await call.answer("MAIN_CHANNEL sozlanmagan.", show_alert=True)
        return
    rows = await db.all_anime_for_random()
    if not rows:
        await call.answer("Hali kino qo'shilmagan.", show_alert=True)
        return
    anime = random.choice(rows)
    await call.answer("Yuborilmoqda...")
    ok = await _post_announcement(bot, db, anime["id"])
    await call.message.answer(
        f"✅ Sinov e'loni kanalga yuborildi: «{anime['title']}»" if ok
        else "❌ Kanalga yuborib bo'lmadi. Bot kanalda admin ekanini va MAIN_CHANNEL to'g'riligini tekshiring."
    )
