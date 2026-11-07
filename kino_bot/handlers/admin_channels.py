"""
Admin: 📣 E'lon kanallari — kanallarni ulash/uzish, kunlik random vaqti, rejalashtirilgan postlar.

  * Yangi kino e'loni barcha YOQILGAN kanallarga boradi.
  * Har kanalning o'z kunlik random vaqti bor; bir kunda ikki kanalga bir xil kino chiqmaydi,
    shu kanalga oxirgi 30 kunda yuborilgan kino qaytmaydi.
"""
from __future__ import annotations

import datetime
import html
import logging
import re

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import announce
import backup_service
import config
import keyboards as kb
from database import Database
from handlers.common import IsAdminFilter, parse_hhmm
from states import AnnounceStates

logger = logging.getLogger("kino_bot.admin_channels")
router = Router(name="admin_channels")
router.message.filter(IsAdminFilter())
router.callback_query.filter(IsAdminFilter())

TIME_PRESETS = ["09:00", "12:00", "15:00", "18:00", "20:00", "22:00"]


def _yn(flag: int | bool) -> str:
    return "✅ yoqilgan" if flag else "⬜️ o'chiq"


# ================================================================== #
# Kanallar ro'yxati
# ================================================================== #
async def _list_view(db: Database):
    channels = await db.list_announce_channels()
    lines = ["📣 <b>E'lon kanallari</b>", ""]
    if channels:
        for c in channels:
            lines.append(
                f"• <b>{html.escape(c['title'])}</b> — e'lon: {'✅' if c['enabled'] else '⬜️'}, "
                f"kunlik: {'✅' if c['daily_enabled'] else '⬜️'} {c['daily_time']}"
            )
    else:
        lines.append("Hali kanal ulanmagan. «➕ Kanal qo'shish» bosing.")
    lines += [
        "",
        "📢 Yangi kino e'loni barcha ✅ kanallarga boradi.",
        "🎲 Har kanalga o'z kunlik random kinosi (har xil kino, 30 kun takrorlanmaydi).",
    ]
    b = InlineKeyboardBuilder()
    for c in channels:
        b.button(text=f"⚙️ {c['title']}", callback_data=f"adm:annch_open:{c['id']}")
    b.button(text="➕ Kanal qo'shish", callback_data="adm:annch_add")
    b.button(text="🕐 Rejalashtirilgan postlar", callback_data="adm:sched_list")
    b.button(text="⬅️ Orqaga", callback_data="adm:panel")
    b.adjust(1)
    return "\n".join(lines), b.as_markup()


@router.callback_query(F.data == "adm:annch")
async def channels_menu(call: CallbackQuery, db: Database, state: FSMContext):
    await state.clear()
    text, markup = await _list_view(db)
    await call.message.answer(text, reply_markup=markup)
    await call.answer()


# ================================================================== #
# Kanal qo'shish
# ================================================================== #
@router.callback_query(F.data == "adm:annch_add")
async def channel_add_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(AnnounceStates.waiting_chat)
    await call.message.answer(
        "📣 Kanalni yuboring. Quyidagilardan birini qilsangiz bo'ladi:\n"
        "• kanal username'i: <code>@kanal_nomi</code>\n"
        "• kanal ID'si: <code>-1001234567890</code>\n"
        "• kanaldan biror postni shu yerga <b>forward</b> qiling\n\n"
        "❗ Avval botni shu kanalga <b>admin</b> qiling (post yuborish huquqi bilan).",
        reply_markup=kb.cancel_menu(),
    )
    await call.answer()


def _chat_ref_from_message(message: Message) -> str | int | None:
    origin = getattr(message, "forward_origin", None)
    chat = getattr(origin, "chat", None) if origin else None
    if chat is None:
        chat = getattr(message, "forward_from_chat", None)
    if chat is not None:
        return chat.id
    text = (message.text or "").strip()
    if not text:
        return None
    m = re.match(r"^(?:https?://)?t\.me/([A-Za-z0-9_]{4,})/?$", text)
    if m:
        return "@" + m.group(1)
    if re.match(r"^@[A-Za-z0-9_]{4,}$", text):
        return text
    if re.match(r"^-?\d{6,}$", text):
        return int(text)
    return None


@router.message(AnnounceStates.waiting_chat)
async def channel_add_chat(message: Message, db: Database, state: FSMContext, bot: Bot):
    ref = _chat_ref_from_message(message)
    if ref is None:
        await message.answer("❗ Tushunmadim. @username, -100... ID yoki kanaldan forward yuboring.")
        return
    try:
        chat = await bot.get_chat(ref)
    except TelegramBadRequest:
        await message.answer("❗ Kanal topilmadi. Username/ID to'g'riligini va bot kanalga qo'shilganini tekshiring.")
        return
    if chat.type not in ("channel", "supergroup", "group"):
        await message.answer("❗ Bu kanal emas. Kanal yoki guruh yuboring.")
        return
    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(chat.id, me.id)
    except TelegramBadRequest:
        await message.answer("❗ Botni bu kanalda tekshirib bo'lmadi. Bot kanalga admin qilib qo'shilganmi?")
        return
    can_post = getattr(member, "can_post_messages", None)
    if member.status not in ("administrator", "creator") or can_post is False:
        await message.answer(
            "❗ Bot bu kanalda admin emas yoki xabar yuborish huquqi yo'q. "
            "Kanal sozlamalarida botga «Xabarlarni yuborish» huquqini bering va qayta urinib ko'ring."
        )
        return
    channel_id = await db.add_announce_channel(str(chat.id), chat.title or str(chat.id))
    if channel_id is None:
        await message.answer("ℹ️ Bu kanal allaqachon ulangan.")
        return
    await state.clear()
    backup_service.request_backup("e'lon kanali ulandi")
    await message.answer(f"✅ «{html.escape(chat.title or '')}» ulandi.", reply_markup=kb.main_menu(is_admin=True))
    text, markup = await _channel_view(db, channel_id)
    await message.answer(text, reply_markup=markup)


# ================================================================== #
# Bitta kanal sahifasi
# ================================================================== #
async def _channel_view(db: Database, channel_id: int):
    c = await db.get_announce_channel(channel_id)
    last = c["last_daily_date"] or "hali yuborilmagan"
    text = (
        f"⚙️ <b>{html.escape(c['title'])}</b>\n"
        f"<code>{c['chat_id']}</code>\n\n"
        f"📢 Yangi kino e'lonlari: {_yn(c['enabled'])}\n"
        f"🎲 Kunlik random kino: {_yn(c['daily_enabled'])}\n"
        f"🕐 Kunlik vaqt (O'zbekiston): <b>{c['daily_time']}</b>\n"
        f"📅 Oxirgi kunlik post: {last}"
    )
    b = InlineKeyboardBuilder()
    b.button(text=f"📢 E'lonlar: {'o‘chirish' if c['enabled'] else 'yoqish'}", callback_data=f"adm:annch_tg:{channel_id}:enabled")
    b.button(text=f"🎲 Kunlik: {'o‘chirish' if c['daily_enabled'] else 'yoqish'}", callback_data=f"adm:annch_tg:{channel_id}:daily_enabled")
    for t in TIME_PRESETS:
        mark = "✅ " if t == c["daily_time"] else ""
        b.button(text=f"{mark}{t}", callback_data=f"adm:annch_t:{channel_id}:{t.replace(':', '')}")
    b.button(text="✍️ Boshqa vaqt", callback_data=f"adm:annch_tc:{channel_id}")
    b.button(text="🧪 Sinov: random kino yuborish", callback_data=f"adm:annch_test:{channel_id}")
    b.button(text="🗑 Kanalni uzish", callback_data=f"adm:annch_del:{channel_id}")
    b.button(text="⬅️ Kanallar", callback_data="adm:annch")
    b.adjust(1, 1, 3, 3, 1, 1, 1, 1)
    return text, b.as_markup()


@router.callback_query(F.data.regexp(r"^adm:annch_open:\d+$"))
async def channel_open(call: CallbackQuery, db: Database):
    channel_id = int(call.data.split(":")[2])
    if not await db.get_announce_channel(channel_id):
        await call.answer("Kanal topilmadi.", show_alert=True)
        return
    text, markup = await _channel_view(db, channel_id)
    await call.message.answer(text, reply_markup=markup)
    await call.answer()


async def _refresh(call: CallbackQuery, db: Database, channel_id: int) -> None:
    text, markup = await _channel_view(db, channel_id)
    try:
        await call.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.regexp(r"^adm:annch_tg:\d+:(enabled|daily_enabled)$"))
async def channel_toggle(call: CallbackQuery, db: Database):
    _, _, cid, field = call.data.split(":")
    c = await db.get_announce_channel(int(cid))
    if not c:
        await call.answer("Kanal topilmadi.", show_alert=True)
        return
    await db.update_announce_channel(int(cid), **{field: 0 if c[field] else 1})
    await _refresh(call, db, int(cid))
    await call.answer("Saqlandi")


@router.callback_query(F.data.regexp(r"^adm:annch_t:\d+:\d{4}$"))
async def channel_time_preset(call: CallbackQuery, db: Database):
    _, _, cid, hhmm = call.data.split(":")
    await db.update_announce_channel(int(cid), daily_time=f"{hhmm[:2]}:{hhmm[2:]}")
    await _refresh(call, db, int(cid))
    await call.answer("Vaqt saqlandi")


@router.callback_query(F.data.regexp(r"^adm:annch_tc:\d+$"))
async def channel_time_custom(call: CallbackQuery, state: FSMContext):
    await state.set_state(AnnounceStates.waiting_time)
    await state.update_data(annch_id=int(call.data.split(":")[2]))
    await call.message.answer(
        "🕐 Kunlik post vaqtini yozing (soat:daqiqa, O'zbekiston vaqti). Masalan: <code>19:30</code>",
        reply_markup=kb.cancel_menu(),
    )
    await call.answer()


@router.message(AnnounceStates.waiting_time, F.text)
async def channel_time_value(message: Message, db: Database, state: FSMContext):
    hhmm = parse_hhmm(message.text)
    if not hhmm:
        await message.answer("❗ Vaqtni <code>19:30</code> ko'rinishida yozing.")
        return
    data = await state.get_data()
    await db.update_announce_channel(data["annch_id"], daily_time=hhmm)
    await state.clear()
    await message.answer(f"✅ Kunlik vaqt: {hhmm}", reply_markup=kb.main_menu(is_admin=True))
    text, markup = await _channel_view(db, data["annch_id"])
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data.regexp(r"^adm:annch_test:\d+$"))
async def channel_test(call: CallbackQuery, db: Database, bot: Bot):
    c = await db.get_announce_channel(int(call.data.split(":")[2]))
    if not c:
        await call.answer("Kanal topilmadi.", show_alert=True)
        return
    await call.answer("Yuborilmoqda...")
    try:
        title = await announce.post_daily(bot, db, c, record=False)
    except Exception as e:  # noqa: BLE001
        await call.message.answer(f"❌ Yuborilmadi: {html.escape(str(e)[:200])}\nBot kanalda admin ekanini tekshiring.")
        return
    await call.message.answer(f"✅ Sinov posti yuborildi: «{html.escape(title)}»" if title else "ℹ️ Yuboradigan kino topilmadi (videoli kino yo'q).")


@router.callback_query(F.data.regexp(r"^adm:annch_del:\d+$"))
async def channel_delete_ask(call: CallbackQuery, db: Database):
    cid = int(call.data.split(":")[2])
    c = await db.get_announce_channel(cid)
    if not c:
        await call.answer("Kanal topilmadi.", show_alert=True)
        return
    b = InlineKeyboardBuilder()
    b.button(text="🗑 Ha, uzish", callback_data=f"adm:annch_del2:{cid}")
    b.button(text="⬅️ Yo'q", callback_data=f"adm:annch_open:{cid}")
    b.adjust(1)
    await call.message.answer(
        f"🗑 «{html.escape(c['title'])}» kanalini uzamizmi?\n"
        "Kanaldagi eski postlar o'chmaydi, faqat bot unga yangi post yubormaydi.",
        reply_markup=b.as_markup(),
    )
    await call.answer()


@router.callback_query(F.data.regexp(r"^adm:annch_del2:\d+$"))
async def channel_delete(call: CallbackQuery, db: Database):
    await db.delete_announce_channel(int(call.data.split(":")[2]))
    await call.answer("🗑 Kanal uzildi", show_alert=True)
    text, markup = await _list_view(db)
    await call.message.answer(text, reply_markup=markup)


# ================================================================== #
# 🕐 Rejalashtirilgan postlar
# ================================================================== #
def _local_to_utc(dt_local: datetime.datetime) -> datetime.datetime:
    return dt_local.astimezone(datetime.timezone.utc).replace(tzinfo=None)


def _utc_to_local_str(run_at_iso: str) -> str:
    dt = datetime.datetime.fromisoformat(run_at_iso) + datetime.timedelta(hours=config.TZ_OFFSET_HOURS)
    return dt.strftime("%d.%m %H:%M")


@router.callback_query(F.data.regexp(r"^adm:sched:\d+$"))
async def sched_menu(call: CallbackQuery, db: Database):
    anime_id = int(call.data.split(":")[2])
    anime = await db.get_anime(anime_id)
    if not anime:
        await call.answer("Kino topilmadi.", show_alert=True)
        return
    if not await db.list_announce_channels(only_enabled=True):
        await call.answer("Avval kanal ulang: Admin panel → 📣 E'lon kanallari.", show_alert=True)
        return
    now = datetime.datetime.now(config.LOCAL_TZ)

    def at(day_offset: int, hour: int) -> datetime.datetime:
        return (now + datetime.timedelta(days=day_offset)).replace(hour=hour, minute=0, second=0, microsecond=0)

    options = [("Bugun 20:00", at(0, 20)), ("Ertaga 12:00", at(1, 12)), ("Ertaga 20:00", at(1, 20))]
    b = InlineKeyboardBuilder()
    for label, dt in options:
        if dt > now:
            b.button(text=label, callback_data=f"adm:sched_at:{anime_id}:{int(dt.timestamp())}")
    b.button(text="✍️ Boshqa vaqt", callback_data=f"adm:sched_c:{anime_id}")
    b.button(text="⬅️ Kino sahifasiga", callback_data=f"adm:anime:{anime_id}")
    b.adjust(1)
    await call.message.answer(
        f"🕐 <b>{html.escape(anime['title'])}</b> qachon barcha kanallarga chiqsin?\n"
        "(O'zbekiston vaqti bilan)",
        reply_markup=b.as_markup(),
    )
    await call.answer()


@router.callback_query(F.data.regexp(r"^adm:sched_at:\d+:\d+$"))
async def sched_preset(call: CallbackQuery, db: Database):
    _, _, anime_id, ts = call.data.split(":")
    run_at = datetime.datetime.fromtimestamp(int(ts), tz=datetime.timezone.utc).replace(tzinfo=None)
    await db.add_scheduled_post(int(anime_id), run_at)
    await call.message.answer(f"✅ Post rejalashtirildi: {_utc_to_local_str(run_at.isoformat())}")
    await call.answer()


@router.callback_query(F.data.regexp(r"^adm:sched_c:\d+$"))
async def sched_custom(call: CallbackQuery, state: FSMContext):
    await state.set_state(AnnounceStates.waiting_sched_time)
    await state.update_data(sched_anime_id=int(call.data.split(":")[2]))
    await call.message.answer(
        "🕐 Vaqtni yozing:\n• <code>21:30</code> — bugun (o'tib ketgan bo'lsa ertaga)\n"
        "• <code>25.12 20:00</code> — kun.oy soat:daqiqa",
        reply_markup=kb.cancel_menu(),
    )
    await call.answer()


@router.message(AnnounceStates.waiting_sched_time, F.text)
async def sched_custom_value(message: Message, db: Database, state: FSMContext):
    raw = message.text.strip()
    now = datetime.datetime.now(config.LOCAL_TZ)
    target = None
    m = re.match(r"^(\d{1,2})\.(\d{1,2})\s+(\d{1,2}):(\d{2})$", raw)
    if m:
        try:
            target = now.replace(month=int(m[2]), day=int(m[1]), hour=int(m[3]), minute=int(m[4]), second=0, microsecond=0)
        except ValueError:
            target = None
    else:
        hhmm = parse_hhmm(raw)
        if hhmm:
            target = now.replace(hour=int(hhmm[:2]), minute=int(hhmm[3:]), second=0, microsecond=0)
            if target <= now:
                target += datetime.timedelta(days=1)
    if target is None or target <= now:
        await message.answer("❗ Vaqt noto'g'ri yoki o'tib ketgan. Masalan: <code>21:30</code> yoki <code>25.12 20:00</code>")
        return
    data = await state.get_data()
    await db.add_scheduled_post(data["sched_anime_id"], _local_to_utc(target))
    await state.clear()
    await message.answer(f"✅ Post rejalashtirildi: {target.strftime('%d.%m %H:%M')}", reply_markup=kb.main_menu(is_admin=True))


@router.callback_query(F.data == "adm:sched_list")
async def sched_list(call: CallbackQuery, db: Database):
    rows = await db.list_scheduled_posts()
    if not rows:
        await call.message.answer("🕐 Rejalashtirilgan postlar yo'q.", reply_markup=kb.admin_back_button("adm:annch"))
        await call.answer()
        return
    await call.message.answer("🕐 <b>Rejalashtirilgan postlar:</b>", reply_markup=kb.admin_back_button("adm:annch"))
    for r in rows:
        b = InlineKeyboardBuilder()
        b.button(text="❌ Bekor qilish", callback_data=f"adm:sched_x:{r['id']}")
        await call.message.answer(
            f"🎬 {html.escape(r['title'])} (ID: {r['code']})\n⏰ {_utc_to_local_str(r['run_at'])}",
            reply_markup=b.as_markup(),
        )
    await call.answer()


@router.callback_query(F.data.regexp(r"^adm:sched_x:\d+$"))
async def sched_cancel(call: CallbackQuery, db: Database):
    await db.set_scheduled_status(int(call.data.split(":")[2]), "cancelled")
    try:
        await call.message.edit_text(call.message.html_text + "\n\n❌ Bekor qilindi")
    except TelegramBadRequest:
        pass
    await call.answer("Bekor qilindi")
