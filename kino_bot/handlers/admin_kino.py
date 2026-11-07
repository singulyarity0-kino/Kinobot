"""
Admin: kino botga xos bo'limlar.

  💎 VIP voronka   — kunlik limit, bepul sinov, chegirma, eslatmalar, treyler (⚙️ sozlamalar)
  📝 So'rovlar     — foydalanuvchilar so'ragan kinolar
  ⚠️ Muammolar     — "video ochilmayapti" kabi xabarlar
  🎞 To'plamlar    — Oskar g'oliblari, Marvel, Oilaviy kinolar ...
  Kino sahifasida  — rejissyor/aktyorlar/davomiylik/yosh/reyting/treyler/sifat, til/sifat variantlari, to'plamga qo'shish
"""
from __future__ import annotations

import html
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import config
import gemini
import keyboards as kb
import payments
from database import Database
from kino_db import detect_quality
from funnel import int_setting
from handlers.common import IsAdminFilter, build_admin_anime_text
from states import KinoAdminStates

logger = logging.getLogger("kino_bot.admin_kino")
router = Router(name="admin_kino")
router.message.filter(IsAdminFilter())
router.callback_query.filter(IsAdminFilter())


def _back(target: str = "adm:panel"):
    return kb.admin_back_button(target)


# ================================================================== #
# 💎 VIP voronka
# ================================================================== #
FUNNEL_NUMBERS = {
    "free_daily_limit": ("🎟 Kunlik bepul limit", "Oddiy foydalanuvchi kuniga nechta kino ocha oladi? (0 = cheklovsiz)", 0, 50),
    "trial_days": ("🎁 Sinov muddati (kun)", "Yangi foydalanuvchiga necha kunlik bepul VIP berilsin?", 1, 30),
    "renewal_discount": ("🎉 Qayta xarid chegirmasi (%)", "VIP'ni qayta olganlarga necha foiz chegirma? (0 = yo'q)", 0, 90),
    "early_access_hours": (
        "⏳ Erta kirish (soat)",
        "Yangi kino oddiy foydalanuvchiga necha soatdan keyin ochiladi? VIP'ga darhol ochiq. (0 = hammaga darhol)",
        0, 720,
    ),
    "temp_vip_hours": (
        "⚡ Vaqtinchalik VIP (soat)",
        "AI chek tekshiruvidan o'tganda necha soatlik vaqtinchalik VIP berilsin? (1–72, tavsiya 12)",
        1, 72,
    ),
    "stars_price_1": ("⭐ Stars narxi: 1 oy", "1 oylik VIP necha Telegram Stars (⭐) turadi? (0 = Stars o'chiq)", 0, 100000),
    "stars_price_2": ("⭐ Stars narxi: 2 oy", "2 oylik VIP necha Stars? (0 = o'chiq)", 0, 100000),
    "stars_price_3": ("⭐ Stars narxi: 3 oy", "3 oylik VIP necha Stars? (0 = o'chiq)", 0, 100000),
    "free_max_quality": (
        "📀 Oddiylar uchun eng yuqori sifat",
        "Oddiy foydalanuvchi ko'ra oladigan eng yuqori sifatni yozing: <code>480</code>, <code>720</code>, "
        "<code>1080</code> (yoki <code>0</code> — cheklovsiz). Undan yuqori sifat faqat VIP uchun.",
        0, 2160,
    ),
}
FUNNEL_TOGGLES = {
    "trial_enabled": "🎁 Bepul sinov",
    "vip_reminders_enabled": "⏳ Tugash eslatmalari",
    "preview_enabled": "🎬 Qulflangan kartada treyler",
    "movie_requests_enabled": "📝 Kino so'rash",
    "manual_pay_enabled": "🧾 Kartaga o'tkazib chek yuborish usuli",
    "ai_receipt_enabled": "🤖 AI chek tekshiruvi (vaqtinchalik VIP)",
    "translate_description": "🌐 Tavsifni o'zbekchaga tarjima qilish",
    "ai_rewrite_description": "🤖 Tavsifni Gemini bilan qiziqarli yozish",
    "ai_channel_hook": "🤖 Kanal postiga Gemini jumlasi",
}


async def _funnel_view(db: Database):
    lim = await int_setting(db, "free_daily_limit", 0)
    text = [
        "💎 <b>VIP voronka sozlamalari</b>",
        "",
        f"🎟 Kunlik bepul limit: <b>{lim if lim else 'cheklovsiz'}</b>",
        f"🎁 Sinov: <b>{await int_setting(db, 'trial_days', 1)} kun</b>",
        f"🎉 Qayta xarid chegirmasi: <b>{await int_setting(db, 'renewal_discount', 0)}%</b>",
        f"⏳ Erta kirish: <b>{await int_setting(db, 'early_access_hours', 0) or 'yo‘q'}"
        f"{' soat' if await int_setting(db, 'early_access_hours', 0) else ''}</b>",
        f"📀 Oddiylar uchun sifat: <b>{await int_setting(db, 'free_max_quality', 0) or 'cheklovsiz'}"
        f"{'p gacha' if await int_setting(db, 'free_max_quality', 0) else ''}</b>",
        f"💳 Avtomatik karta to'lovi: <b>{'ulangan' if payments.card_enabled() else 'ulanmagan (PAYMENT_PROVIDER_TOKEN)'}</b>",
        f"⭐ Stars narxlari (1/2/3 oy): <b>{await int_setting(db, 'stars_price_1', 0)} / "
        f"{await int_setting(db, 'stars_price_2', 0)} / {await int_setting(db, 'stars_price_3', 0)}</b>",
        f"🤖 Gemini: <b>{'ulangan' if gemini.is_configured() else 'kalit yo‘q (GEMINI_API_KEY)'}</b>",
        f"⚡ Vaqtinchalik VIP (AI chek): <b>{await int_setting(db, 'temp_vip_hours', 12)} soat</b>",
        "",
        "Pastdagi tugmalar bilan o'zgartiring:",
    ]
    b = InlineKeyboardBuilder()
    for key, (label, _p, _lo, _hi) in FUNNEL_NUMBERS.items():
        b.button(text=f"✏️ {label}", callback_data=f"adm:fn_set:{key}")
    for key, label in FUNNEL_TOGGLES.items():
        on = await db.get_setting(key, "1") == "1"
        b.button(text=f"{'✅' if on else '⬜️'} {label}", callback_data=f"adm:fn_tg:{key}")
    b.button(text="⬅️ Orqaga", callback_data="adm:panel")
    b.adjust(1)
    return "\n".join(text), b.as_markup()


@router.callback_query(F.data == "adm:funnel")
async def funnel_menu(call: CallbackQuery, db: Database, state: FSMContext):
    await state.clear()
    text, markup = await _funnel_view(db)
    await call.message.answer(text, reply_markup=markup)
    await call.answer()


@router.callback_query(F.data.startswith("adm:fn_tg:"))
async def funnel_toggle(call: CallbackQuery, db: Database):
    key = call.data.split(":")[2]
    if key not in FUNNEL_TOGGLES:
        await call.answer()
        return
    cur = await db.get_setting(key, "1")
    await db.set_setting(key, "0" if cur == "1" else "1")
    text, markup = await _funnel_view(db)
    try:
        await call.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        pass
    await call.answer("Saqlandi")


@router.callback_query(F.data.startswith("adm:fn_set:"))
async def funnel_set_start(call: CallbackQuery, state: FSMContext):
    key = call.data.split(":")[2]
    if key not in FUNNEL_NUMBERS:
        await call.answer()
        return
    await state.set_state(KinoAdminStates.waiting_setting_value)
    await state.update_data(fn_key=key)
    await call.message.answer(FUNNEL_NUMBERS[key][1], reply_markup=kb.cancel_menu())
    await call.answer()


@router.message(KinoAdminStates.waiting_setting_value, F.text)
async def funnel_set_value(message: Message, db: Database, state: FSMContext):
    data = await state.get_data()
    key = data.get("fn_key")
    label, _p, lo, hi = FUNNEL_NUMBERS[key]
    raw = message.text.strip()
    if not raw.isdigit() or not (lo <= int(raw) <= hi):
        await message.answer(f"❗ {lo} dan {hi} gacha butun son kiriting.")
        return
    if key == "free_max_quality" and int(raw) not in (0, 360, 480, 720, 1080, 2160):
        await message.answer("❗ Faqat: 0, 360, 480, 720, 1080 yoki 2160.")
        return
    await db.set_setting(key, raw)
    await state.clear()
    await message.answer(f"✅ {label}: {raw}", reply_markup=kb.main_menu(is_admin=True))
    text, markup = await _funnel_view(db)
    await message.answer(text, reply_markup=markup)


# ================================================================== #
# 📝 So'rovlar
# ================================================================== #
@router.callback_query(F.data == "adm:requests")
async def requests_list(call: CallbackQuery, db: Database):
    rows = await db.list_movie_requests("new", limit=15)
    if not rows:
        await call.message.answer("📝 Yangi so'rovlar yo'q.", reply_markup=_back())
        await call.answer()
        return
    await call.message.answer(f"📝 <b>Yangi so'rovlar</b> ({len(rows)} ta ko'rsatilmoqda):", reply_markup=_back())
    for r in rows:
        b = InlineKeyboardBuilder()
        b.button(text="✅ Qo'shildi", callback_data=f"req:done:{r['id']}")
        b.button(text="❌ Rad etish", callback_data=f"req:no:{r['id']}")
        b.adjust(2)
        await call.message.answer(
            f"🎬 {html.escape(r['text'])}\n👤 <code>{r['telegram_id']}</code>", reply_markup=b.as_markup()
        )
    await call.answer()


@router.callback_query(F.data.regexp(r"^req:(done|no):\d+$"))
async def request_resolve(call: CallbackQuery, db: Database, bot: Bot):
    _, action, rid = call.data.split(":")
    req = await db.get_movie_request(int(rid))
    if not req or req["status"] != "new":
        await call.answer("Bu so'rov allaqachon ko'rib chiqilgan.", show_alert=True)
        return
    await db.set_request_status(req["id"], "done" if action == "done" else "rejected")
    if action == "done":
        msg = f"🎉 So'ragan kinongiz qo'shildi: «{html.escape(req['text'])}»\nNomini yozib qidiring 🔎"
    else:
        msg = f"😕 Afsuski «{html.escape(req['text'])}» kinosini hozircha qo'sha olmaymiz."
    try:
        await bot.send_message(req["telegram_id"], msg)
    except Exception:  # noqa: BLE001 — foydalanuvchi botni bloklagan bo'lishi mumkin
        pass
    try:
        await call.message.edit_text(call.message.html_text + ("\n\n✅ Qo'shildi" if action == "done" else "\n\n❌ Rad etildi"))
    except TelegramBadRequest:
        pass
    await call.answer("Bajarildi")


# ================================================================== #
# ⚠️ Muammolar
# ================================================================== #
@router.callback_query(F.data == "adm:reports")
async def reports_list(call: CallbackQuery, db: Database):
    rows = await db.list_reports("new", limit=15)
    if not rows:
        await call.message.answer("⚠️ Yangi muammo xabarlari yo'q.", reply_markup=_back())
        await call.answer()
        return
    await call.message.answer(f"⚠️ <b>Muammolar</b> ({len(rows)} ta ko'rsatilmoqda):", reply_markup=_back())
    for r in rows:
        b = InlineKeyboardBuilder()
        b.button(text="✅ Tuzatildi", callback_data=f"rep:fix:{r['id']}")
        if r["anime_id"]:
            b.button(text="🎬 Kino sahifasi", callback_data=f"adm:anime:{r['anime_id']}")
        b.adjust(1)
        title = f"{r['anime_title']} (ID: {r['anime_code']})" if r["anime_title"] else "—"
        await call.message.answer(
            f"🎬 {html.escape(title)}\n💬 {html.escape(r['text'])}\n👤 <code>{r['telegram_id']}</code>",
            reply_markup=b.as_markup(),
        )
    await call.answer()


@router.callback_query(F.data.startswith("rep:fix:"))
async def report_fix(call: CallbackQuery, db: Database, bot: Bot):
    report = await db.get_report(int(call.data.split(":")[2]))
    if not report or report["status"] != "new":
        await call.answer("Bu xabar allaqachon yopilgan.", show_alert=True)
        return
    await db.set_report_status(report["id"], "fixed")
    try:
        await bot.send_message(report["telegram_id"], "✅ Xabar bergan muammongiz tuzatildi. Rahmat! 🙏")
    except Exception:  # noqa: BLE001
        pass
    try:
        await call.message.edit_text(call.message.html_text + "\n\n✅ Tuzatildi")
    except TelegramBadRequest:
        pass
    await call.answer("Yopildi")


# ================================================================== #
# 🎞 To'plamlar
# ================================================================== #
@router.callback_query(F.data == "adm:cols")
async def cols_menu(call: CallbackQuery, db: Database, state: FSMContext):
    await state.clear()
    cols = await db.list_collections()
    b = InlineKeyboardBuilder()
    for c in cols:
        b.button(text=f"🗑 {c['emoji']} {c['title']} ({c['item_count']})", callback_data=f"adm:col_del:{c['id']}")
    b.button(text="➕ Yangi to'plam", callback_data="adm:col_new")
    b.button(text="⬅️ Orqaga", callback_data="adm:panel")
    b.adjust(1)
    hint = "Kinoni to'plamga qo'shish: 🎬 Kinolar ro'yxati → kino → «🎞 To'plamlarga qo'shish».\nO'chirish uchun to'plam tugmasini bosing."
    await call.message.answer(f"🎞 <b>To'plamlar</b>\n\n{hint}", reply_markup=b.as_markup())
    await call.answer()


@router.callback_query(F.data == "adm:col_new")
async def col_new(call: CallbackQuery, state: FSMContext):
    await state.set_state(KinoAdminStates.waiting_collection_name)
    await call.message.answer(
        "🎞 To'plam nomini yozing (boshiga emoji qo'ysangiz bo'ladi, masalan: <code>🏆 Oskar g'oliblari</code>):",
        reply_markup=kb.cancel_menu(),
    )
    await call.answer()


@router.message(KinoAdminStates.waiting_collection_name, F.text)
async def col_new_name(message: Message, db: Database, state: FSMContext):
    raw = message.text.strip()
    emoji, title = "🎞", raw
    first, _, rest = raw.partition(" ")
    if rest and not first[0].isalnum():  # birinchi so'z emoji bo'lsa
        emoji, title = first, rest.strip()
    if not title or len(title) > 40:
        await message.answer("❗ Nom 1–40 belgi bo'lsin.")
        return
    cid = await db.create_collection(title, emoji)
    await state.clear()
    if cid is None:
        await message.answer("❗ Bunday nomli to'plam allaqachon bor.", reply_markup=kb.main_menu(is_admin=True))
        return
    await message.answer(f"✅ «{emoji} {title}» to'plami yaratildi.", reply_markup=kb.main_menu(is_admin=True))


@router.callback_query(F.data.startswith("adm:col_del:"))
async def col_delete(call: CallbackQuery, db: Database):
    cid = int(call.data.split(":")[2])
    await db.delete_collection(cid)
    await call.answer("🗑 To'plam o'chirildi (kinolar o'chmadi)", show_alert=True)


async def _colpick_markup(db: Database, anime_id: int):
    cols = await db.list_collections()
    chosen = await db.anime_collection_ids(anime_id)
    b = InlineKeyboardBuilder()
    for c in cols:
        mark = "✅ " if c["id"] in chosen else ""
        b.button(text=f"{mark}{c['emoji']} {c['title']}", callback_data=f"adm:col_tg:{anime_id}:{c['id']}")
    b.button(text="⬅️ Kino sahifasiga", callback_data=f"adm:anime:{anime_id}")
    b.adjust(1)
    return b.as_markup()


@router.callback_query(F.data.startswith("adm:colpick:"))
async def colpick(call: CallbackQuery, db: Database):
    anime_id = int(call.data.split(":")[2])
    if not await db.list_collections():
        await call.answer("Avval to'plam yarating: Admin panel → 🎞 To'plamlar.", show_alert=True)
        return
    await call.message.answer("🎞 Kinoni qaysi to'plamlarga qo'shamiz?", reply_markup=await _colpick_markup(db, anime_id))
    await call.answer()


@router.callback_query(F.data.startswith("adm:col_tg:"))
async def col_toggle(call: CallbackQuery, db: Database):
    _, _, anime_id, col_id = call.data.split(":")
    await db.toggle_collection_item(int(col_id), int(anime_id))
    try:
        await call.message.edit_reply_markup(reply_markup=await _colpick_markup(db, int(anime_id)))
    except TelegramBadRequest:
        pass
    await call.answer()


# ================================================================== #
# 🎬 Kino qo'shimcha maydonlari
# ================================================================== #
FIELDS = {
    "director": ("🎬 Rejissyor", "Rejissyor ismini yozing (masalan: <code>Christopher Nolan</code>):"),
    "actors": ("👥 Aktyorlar", "Aktyorlarni vergul bilan yozing:"),
    "duration": ("⏱ Davomiylik", "Davomiylikni yozing (masalan: <code>2 soat 15 daqiqa</code>):"),
    "age_rating": ("🔞 Yosh chegarasi", "Yosh chegarasini yozing: <code>0+</code>, <code>6+</code>, <code>12+</code>, <code>16+</code> yoki <code>18+</code>:"),
    "imdb_rating": ("🌟 Reyting (TMDB/IMDb)", "Reytingni yozing (masalan: <code>8.1</code>):"),
    "trailer_url": ("🎞 Treyler havolasi", "Treyler havolasini yuboring (YouTube):"),
    "quality": ("📀 Sifat", "Sifatni yozing (masalan: <code>1080p</code>):"),
}


@router.callback_query(F.data.regexp(r"^adm:kx:\d+$"))
async def kx_menu(call: CallbackQuery, db: Database):
    anime_id = int(call.data.split(":")[2])
    anime = await db.get_anime(anime_id)
    if not anime:
        await call.answer("Kino topilmadi.", show_alert=True)
        return
    b = InlineKeyboardBuilder()
    for field, (label, _p) in FIELDS.items():
        filled = "✅ " if anime[field] else ""
        b.button(text=f"{filled}{label}", callback_data=f"adm:kxe:{anime_id}:{field}")
    kind_now = "serial" if anime["kind"] == "series" else "kino"
    b.button(text=f"📺 Turi: {kind_now} (almashtirish)", callback_data=f"adm:kxk:{anime_id}")
    open_now = "✅ hammaga darhol ochiq" if anime["open_to_all"] else "⬜️ kechikish qo'llanadi"
    b.button(text=f"⏳ Erta kirish: {open_now}", callback_data=f"adm:kxo:{anime_id}")
    b.button(text="🤖 Janrni Gemini bilan taklif qilish", callback_data=f"adm:aig:{anime_id}")
    b.button(text="⬅️ Kino sahifasiga", callback_data=f"adm:anime:{anime_id}")
    b.adjust(2, 2, 2, 1, 1, 1, 1, 1)
    await call.message.answer("🎬 Qaysi ma'lumotni o'zgartiramiz?", reply_markup=b.as_markup())
    await call.answer()


@router.callback_query(F.data.startswith("adm:kxk:"))
async def kx_kind(call: CallbackQuery, db: Database):
    anime_id = int(call.data.split(":")[2])
    anime = await db.get_anime(anime_id)
    if not anime:
        await call.answer("Kino topilmadi.", show_alert=True)
        return
    new_kind = "movie" if anime["kind"] == "series" else "series"
    await db.update_anime_field(anime_id, "kind", new_kind)
    await call.answer("Turi: " + ("serial" if new_kind == "series" else "kino"), show_alert=True)


@router.callback_query(F.data.startswith("adm:kxo:"))
async def kx_open_to_all(call: CallbackQuery, db: Database):
    anime_id = int(call.data.split(":")[2])
    on = await db.toggle_open_to_all(anime_id)
    await call.answer(
        "✅ Bu kino hammaga darhol ochiq (kechikishsiz)." if on else "⏳ Kechikish qo'llanadi.", show_alert=True
    )


@router.callback_query(F.data.startswith("adm:aig:"))
async def kx_ai_genres(call: CallbackQuery, db: Database, state: FSMContext):
    anime_id = int(call.data.split(":")[2])
    anime = await db.get_anime(anime_id)
    if not anime:
        await call.answer("Kino topilmadi.", show_alert=True)
        return
    if not gemini.is_configured():
        await call.answer("Gemini kaliti yo'q (Render: GEMINI_API_KEY).", show_alert=True)
        return
    await call.answer("Gemini o'ylayapti...")
    genres = await db.list_genres()
    names = [g["name"] for g in genres if not g["is_vip"]]
    picked = await gemini.suggest_genres(anime["title"], html.unescape(anime["description"] or ""), names)
    if not picked:
        await call.message.answer("😕 Gemini janr taklif qila olmadi (limit tugagan yoki tavsif yo'q).")
        return
    by_name = {g["name"]: g["id"] for g in genres}
    ids = [by_name[n] for n in picked if n in by_name]
    await state.update_data(ai_genre_ids=ids, ai_genre_anime=anime_id)
    b = InlineKeyboardBuilder()
    b.button(text="✅ Qo'llash", callback_data=f"adm:aig_ok:{anime_id}")
    b.button(text="❌ Bekor", callback_data=f"adm:kx:{anime_id}")
    b.adjust(2)
    await call.message.answer("🤖 Taklif: " + ", ".join(picked), reply_markup=b.as_markup())


@router.callback_query(F.data.startswith("adm:aig_ok:"))
async def kx_ai_genres_apply(call: CallbackQuery, db: Database, state: FSMContext):
    anime_id = int(call.data.split(":")[2])
    data = await state.get_data()
    if data.get("ai_genre_anime") != anime_id or not data.get("ai_genre_ids"):
        await call.answer("Taklif eskirgan, qaytadan so'rang.", show_alert=True)
        return
    await db.update_anime_genres(anime_id, data["ai_genre_ids"])
    await state.update_data(ai_genre_ids=None, ai_genre_anime=None)
    await call.answer("✅ Janrlar yangilandi", show_alert=True)


@router.callback_query(F.data.startswith("adm:kxe:"))
async def kx_edit_start(call: CallbackQuery, state: FSMContext):
    _, _, anime_id, field = call.data.split(":")
    if field not in FIELDS:
        await call.answer()
        return
    await state.set_state(KinoAdminStates.waiting_field_value)
    await state.update_data(kx_anime_id=int(anime_id), kx_field=field)
    await call.message.answer(FIELDS[field][1] + "\n(O'chirish uchun «-» yozing)", reply_markup=kb.cancel_menu())
    await call.answer()


@router.message(KinoAdminStates.waiting_field_value, F.text)
async def kx_edit_value(message: Message, db: Database, state: FSMContext, bot: Bot):
    data = await state.get_data()
    anime_id, field = data["kx_anime_id"], data["kx_field"]
    value = message.text.strip()
    if value == "-":
        value = None
    elif field == "trailer_url" and not value.lower().startswith(("http://", "https://")):
        await message.answer("❗ Havola http:// yoki https:// bilan boshlansin.")
        return
    elif field == "imdb_rating":
        try:
            if not (0 <= float(value.replace(",", ".")) <= 10):
                raise ValueError
            value = value.replace(",", ".")
        except ValueError:
            await message.answer("❗ 0 dan 10 gacha son yozing (masalan 7.8).")
            return
    elif len(value) > 300:
        await message.answer("❗ Juda uzun (300 belgigacha).")
        return
    await db.update_anime_field(anime_id, field, value)
    await state.clear()
    await message.answer(f"✅ {FIELDS[field][0]} yangilandi.", reply_markup=kb.main_menu(is_admin=True))
    text = await build_admin_anime_text(db, anime_id)
    has_seasons = bool(await db.list_seasons(anime_id))
    if text:
        await message.answer(text, reply_markup=kb.admin_anime_detail_keyboard(anime_id, has_seasons))


# ================================================================== #
# 🎙 Til / sifat variantlari
# ================================================================== #
async def _versions_view(db: Database, anime_id: int):
    anime = await db.get_anime(anime_id)
    versions = await db.list_versions(anime_id)
    lines = [f"🎙 <b>{html.escape(anime['title'])}</b> — variantlar", ""]
    lines += [f"• {html.escape(v['label'])}" for v in versions] or ["Hali variant yo'q."]
    lines += ["", "Har variant — alohida video (masalan, o'zbekcha 1080p, ruscha 720p). Foydalanuvchi tanlaydi."]
    b = InlineKeyboardBuilder()
    for v in versions:
        b.button(text=f"🗑 {v['label']}", callback_data=f"adm:ver_del:{anime_id}:{v['id']}")
    b.button(text="➕ Variant qo'shish", callback_data=f"adm:ver_add:{anime_id}")
    b.button(text="⬅️ Kino sahifasiga", callback_data=f"adm:anime:{anime_id}")
    b.adjust(1)
    return "\n".join(lines), b.as_markup()


@router.callback_query(F.data.regexp(r"^adm:ver:\d+$"))
async def ver_menu(call: CallbackQuery, db: Database):
    anime_id = int(call.data.split(":")[2])
    if not await db.get_anime(anime_id):
        await call.answer("Kino topilmadi.", show_alert=True)
        return
    text, markup = await _versions_view(db, anime_id)
    await call.message.answer(text, reply_markup=markup)
    await call.answer()


@router.callback_query(F.data.startswith("adm:ver_del:"))
async def ver_delete(call: CallbackQuery, db: Database):
    _, _, anime_id, vid = call.data.split(":")
    await db.delete_version(int(vid))
    text, markup = await _versions_view(db, int(anime_id))
    try:
        await call.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        pass
    await call.answer("🗑 O'chirildi")


def _quality_keyboard():
    b = InlineKeyboardBuilder()
    for q in (360, 480, 720, 1080, 2160):
        b.button(text=f"{q}p", callback_data=f"adm:vq:{q}")
    b.adjust(3, 2)
    return b.as_markup()


def _lang_keyboard():
    b = InlineKeyboardBuilder()
    for i, (emoji, name) in enumerate(config.AUDIO_LANG_OPTIONS):
        b.button(text=f"{emoji} {name}", callback_data=f"adm:vl:{i}")
    b.button(text="Tilsiz (faqat sifat)", callback_data="adm:vl:-1")
    b.adjust(2)
    return b.as_markup()


@router.callback_query(F.data.startswith("adm:ver_add:"))
async def ver_add_start(call: CallbackQuery, state: FSMContext):
    anime_id = int(call.data.split(":")[2])
    await state.set_state(KinoAdminStates.waiting_version_video)
    await state.update_data(ver_anime_id=anime_id)
    await call.message.answer(
        "📹 Variant videosini yuboring. Sifatni (480p/720p/1080p) bot video o'lchamidan o'zi aniqlaydi.",
        reply_markup=kb.cancel_menu(),
    )
    await call.answer()


@router.message(KinoAdminStates.waiting_version_video, F.video | F.document)
async def ver_add_video(message: Message, state: FSMContext):
    if message.video:
        quality = detect_quality(message.video.width, message.video.height)
        await state.update_data(ver_file_id=message.video.file_id, ver_quality=quality)
        await message.answer(f"📀 Sifat aniqlandi: <b>{quality}p</b>\n\n🎙 Bu variantning tilini tanlang:", reply_markup=_lang_keyboard())
    else:
        await state.update_data(ver_file_id=message.document.file_id, ver_quality=None)
        await message.answer(
            "📄 Fayl hujjat sifatida yuborilgan, o'lchamini bilmayman. Sifatini tanlang:", reply_markup=_quality_keyboard()
        )


@router.message(KinoAdminStates.waiting_version_video)
async def ver_add_video_wrong(message: Message):
    await message.answer("📹 Iltimos, video fayl yuboring.")


@router.callback_query(F.data.regexp(r"^adm:vq:\d+$"))
async def ver_pick_quality(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get("ver_file_id"):
        await call.answer("Avval video yuboring.", show_alert=True)
        return
    quality = int(call.data.split(":")[2])
    await state.update_data(ver_quality=quality)
    await call.message.answer(f"📀 Sifat: <b>{quality}p</b>\n\n🎙 Bu variantning tilini tanlang:", reply_markup=_lang_keyboard())
    await call.answer()


@router.callback_query(F.data.regexp(r"^adm:vl:-?\d+$"))
async def ver_pick_lang(call: CallbackQuery, db: Database, state: FSMContext):
    data = await state.get_data()
    if not data.get("ver_file_id") or not data.get("ver_quality"):
        await call.answer("Avval video yuboring.", show_alert=True)
        return
    idx = int(call.data.split(":")[2])
    quality = data["ver_quality"]
    if idx >= 0:
        emoji, name = config.AUDIO_LANG_OPTIONS[idx]
        label = f"{emoji} {name} · {quality}p"
    else:
        label = f"🎞 {quality}p"
    await db.add_version(data["ver_anime_id"], label, data["ver_file_id"], quality)
    await state.clear()
    await call.message.answer(f"✅ Variant qo'shildi: {label}", reply_markup=kb.main_menu(is_admin=True))
    text, markup = await _versions_view(db, data["ver_anime_id"])
    await call.message.answer(text, reply_markup=markup)
    await call.answer()


# ================================================================== #
# 📈 Kino statistikasi
# ================================================================== #
@router.callback_query(F.data == "adm:kstats")
async def kino_stats(call: CallbackQuery, db: Database):
    lines = ["📈 <b>Kino statistikasi</b> (oxirgi 30 kun)", ""]
    top = await db.top_viewed(limit=10, days=30)
    lines.append("🔥 <b>Eng ko'p ko'rilgan:</b>")
    lines += [f"{i}. {html.escape(a['title'])} — {a['view_count']}" for i, a in enumerate(top, 1)] or ["ma'lumot yo'q"]

    quality = await db.quality_stats(30)
    lines += ["", "📀 <b>Qaysi sifat tanlangan:</b>"]
    if quality:
        total = sum(n for _, n in quality)
        lines += [f"• {q}p — {n} marta ({round(n * 100 / total)}%)" for q, n in quality]
    else:
        lines.append("ma'lumot yo'q (variantli kinolar ko'rilgach paydo bo'ladi)")

    trig = await db.vip_trigger_stats(10)
    lines += ["", "💎 <b>VIP olib kelgan kinolar</b> (to'lovdan oldin ko'rgan oxirgi qulflangan kino):"]
    if trig:
        lines += [
            f"{i}. {html.escape(r['title'])} — {r['sales']} ta xarid, {r['total']:,} so'm".replace(",", " ")
            for i, r in enumerate(trig, 1)
        ]
    else:
        lines.append("ma'lumot yo'q")
    await call.message.answer("\n".join(lines), reply_markup=kb.admin_back_button("adm:panel"))
    await call.answer()
