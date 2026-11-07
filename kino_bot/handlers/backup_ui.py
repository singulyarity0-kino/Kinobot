"""
Admin panel: 💾 Zaxira nusxa → 📦 Zaxira kanali (videolar) va ⏱ Avto-zaxira sozlamalari.
"""
from __future__ import annotations

import asyncio
import datetime
import html
import logging
import time

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import archive
import backup_service
import config
import keyboards as kb
from database import Database
from handlers.common import IsAdminFilter
from states import AdminTextStates

logger = logging.getLogger("anime_bot.backup_ui")
router = Router(name="backup_ui")
router.message.filter(IsAdminFilter())
router.callback_query.filter(IsAdminFilter())

_bg_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


async def _show(call: CallbackQuery, text: str, markup) -> None:
    try:
        await call.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        await call.message.answer(text, reply_markup=markup)


# ================================================================== #
# 📦 Zaxira kanali
# ================================================================== #
async def _channel_screen(db: Database) -> tuple[str, object]:
    chat = await archive.get_storage_chat(db)
    title = await db.get_setting("storage_channel_title", "")
    archived, total = await db.archive_counts()
    lines = ["📦 <b>Zaxira kanali</b>", ""]
    if chat is not None:
        lines.append("Holati: ✅ ulangan")
        lines.append(f"Kanal: <b>{html.escape(title or str(chat))}</b>")
        lines.append(f"📼 Kanalga saqlangan videolar: <b>{archived} / {total}</b>")
        if archived < total:
            lines.append("⚠️ Hammasi saqlanmagan — «Eski videolarni kanalga ko'chirish» ni bosing.")
        lines += [
            "",
            "ℹ️ Yangi videolar kanalga avtomatik saqlanadi. Bazaning avto-zaxira nusxalari ham shu kanalga yuboriladi.",
        ]
        if config.BACKUP_CHANNEL and not await db.get_setting("storage_channel_id", ""):
            lines.append("(Kanal .env dagi BACKUP_CHANNEL orqali berilgan.)")
    else:
        lines += [
            "Holati: ❌ ulanmagan",
            "",
            "Nima uchun kerak? Video <code>file_id</code> lari faqat shu bot uchun ishlaydi. Token yoki bot "
            "almashsa, videolar yo'qolishi mumkin. Videolarning haqiqiy nusxasi kanalda tursa, hammasini "
            "tiklash mumkin.",
            "",
            "Qanday ulanadi:",
            "1) Yangi <b>YOPIQ (private)</b> kanal oching",
            "2) Botni kanalga <b>admin</b> qilib qo'shing («Xabar yuborish» ruxsati bilan)",
            "3) «🔗 Kanalni ulash» ni bosing",
        ]
    return "\n".join(lines), kb.backup_channel_keyboard(chat is not None)


@router.callback_query(F.data == "adm:bk_channel")
async def bk_channel_open(call: CallbackQuery, db: Database, state: FSMContext):
    await state.clear()
    text, markup = await _channel_screen(db)
    await _show(call, text, markup)
    await call.answer()


@router.callback_query(F.data == "adm:bk_channel_set")
async def bk_channel_set(call: CallbackQuery, state: FSMContext):
    await state.set_state(AdminTextStates.waiting_backup_channel)
    await call.message.answer(
        "🔗 <b>Zaxira kanalini ulash</b>\n\n"
        "1) <b>Yopiq (private)</b> kanal oching — zaxirada foydalanuvchilar ma'lumotlari ham bor.\n"
        "2) Botni shu kanalga <b>admin</b> qiling («Xabar yuborish» ruxsati bilan).\n"
        "3) Quyidagilardan birini yuboring:\n"
        "   • kanaldan istalgan xabarni <b>forward</b> qiling (eng oson), yoki\n"
        "   • kanal ID sini (<code>-100...</code>), yoki\n"
        "   • ochiq kanal bo'lsa @username ni.",
        reply_markup=kb.cancel_menu(),
    )
    await call.answer()


def _chat_ref_from_message(message: Message):
    origin = getattr(message, "forward_origin", None)
    chat = getattr(origin, "chat", None) if origin is not None else None
    if chat is not None:
        return chat.id
    legacy = getattr(message, "forward_from_chat", None)
    if legacy is not None:
        return legacy.id
    return archive.parse_chat_ref(message.text or "")


@router.message(AdminTextStates.waiting_backup_channel)
async def bk_channel_receive(message: Message, state: FSMContext, db: Database, bot: Bot):
    ref = _chat_ref_from_message(message)
    if ref is None:
        await message.answer(
            "❗ Kanalni aniqlab bo'lmadi. Kanaldan xabar forward qiling, yoki <code>-100...</code> ID / "
            "@username yuboring. (Yopiq kanalning taklif havolasi ishlamaydi.)"
        )
        return
    ok, chat, err = await archive.verify_channel(bot, ref)
    if not ok:
        await message.answer(f"❌ {html.escape(err, quote=False)}\n\nTuzatib, qaytadan yuboring yoki ❌ Bekor qilish ni bosing.")
        return
    try:
        await bot.send_message(chat.id, "✅ Zaxira kanali ulandi. Bu yerga videolar va baza nusxalari saqlanadi.")
    except Exception as e:
        await message.answer(f"❌ Kanalga yozib bo'lmadi: {html.escape(str(e)[:200], quote=False)}")
        return
    await db.set_setting("storage_channel_id", str(chat.id))
    await db.set_setting("storage_channel_title", getattr(chat, "title", None) or str(chat.id))
    backup_service.request_backup("zaxira kanali ulandi")
    await state.clear()
    archived, total = await db.archive_counts()
    extra = (
        f"\n\n📼 Hozircha {total - archived} ta eski video kanalga saqlanmagan — "
        "«📤 Eski videolarni kanalga ko'chirish» ni bosing." if total > archived else ""
    )
    await message.answer(f"✅ Zaxira kanali ulandi: <b>{html.escape(getattr(chat, 'title', '') or str(chat.id))}</b>{extra}",
                         reply_markup=kb.main_menu(is_admin=True))
    text, markup = await _channel_screen(db)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "adm:bk_channel_off")
async def bk_channel_off(call: CallbackQuery, db: Database):
    await db.set_setting("storage_channel_id", "")
    await db.set_setting("storage_channel_title", "")
    note = ""
    if config.BACKUP_CHANNEL:
        note = "\n\n⚠️ .env da BACKUP_CHANNEL berilgan — uni ham o'chiring, aks holda kanal qayta ishlatiladi."
    text, markup = await _channel_screen(db)
    await _show(call, "🔌 Zaxira kanali uzildi (kanaldagi videolar o'chirilmaydi)." + note + "\n\n" + text, markup)
    await call.answer()


@router.callback_query(F.data == "adm:bk_migrate")
async def bk_migrate(call: CallbackQuery, db: Database, bot: Bot):
    if await archive.get_storage_chat(db) is None:
        await call.answer("Avval kanalni ulang.", show_alert=True)
        return
    if archive.is_busy():
        await call.answer("Boshqa zaxira jarayoni ketmoqda. Tugashini kuting.", show_alert=True)
        return
    await call.answer("Boshlandi")
    _spawn(archive.migrate_old(bot, db, call.message.chat.id))


@router.callback_query(F.data == "adm:bk_resync")
async def bk_resync(call: CallbackQuery, db: Database, bot: Bot):
    if await archive.get_storage_chat(db) is None:
        await call.answer("Avval kanalni ulang.", show_alert=True)
        return
    if archive.is_busy():
        await call.answer("Boshqa zaxira jarayoni ketmoqda. Tugashini kuting.", show_alert=True)
        return
    await call.answer("Boshlandi")
    await call.message.answer(
        "♻️ Yangi bot/token uchun <code>file_id</code> lar kanaldan qayta olinmoqda. "
        "Bot har bir videoni vaqtincha sizga forward qilib, darrov o'chiradi — bu normal."
    )
    _spawn(archive.resync_file_ids(bot, db, call.message.chat.id))


# ================================================================== #
# ⏱ Avto-zaxira sozlamalari
# ================================================================== #
async def _settings_screen(db: Database) -> tuple[str, object]:
    enabled = await db.get_setting("backup_enabled", "1") == "1"
    interval = int(await db.get_setting("backup_interval_hours", "2") or 2)
    keep = int(await db.get_setting("backup_keep", "10") or 10)
    last = int(await db.get_setting("backup_last_ts", "0") or 0)
    if last > 0:
        when = datetime.datetime.fromtimestamp(last, config.LOCAL_TZ).strftime("%d.%m.%Y %H:%M")
        ago = max(0, int(time.time()) - last) // 60
        last_txt = f"{when} ({ago} daqiqa oldin)"
    else:
        last_txt = "hali olinmagan"
    chat = await archive.get_storage_chat(db)
    lines = [
        "⏱ <b>Avto-zaxira sozlamalari</b>",
        "",
        "Holati: " + ("✅ yoqilgan" if enabled else "⏸ o'chirilgan"),
        f"Har <b>{interval} soatda</b> nusxa olinadi",
        "Kino yoki qismlar saqlanganda ham (oxirgi o'zgarishdan 1 daqiqa keyin, bitta nusxa)",
        f"Serverda saqlanadigan nusxalar: {keep} ta",
        f"Oxirgi nusxa: {last_txt}",
        "Yuboriladi: adminlarga" + (" va zaxira kanaliga" if chat is not None else " (zaxira kanali ulanmagan)"),
        "",
        "Zaxira ichida: foydalanuvchilar, VIP, referal, kinolar, qismlar, sozlamalar.",
    ]
    return "\n".join(lines), kb.backup_settings_keyboard(enabled, interval, keep)


@router.callback_query(F.data == "adm:bk_settings")
async def bk_settings_open(call: CallbackQuery, db: Database):
    text, markup = await _settings_screen(db)
    await _show(call, text, markup)
    await call.answer()


@router.callback_query(F.data == "adm:bk_toggle")
async def bk_toggle(call: CallbackQuery, db: Database):
    enabled = await db.get_setting("backup_enabled", "1") == "1"
    await db.set_setting("backup_enabled", "0" if enabled else "1")
    text, markup = await _settings_screen(db)
    await _show(call, text, markup)
    await call.answer("Yoqildi" if not enabled else "O'chirildi")


@router.callback_query(F.data.regexp(r"^adm:bk_int:\d+$"))
async def bk_interval(call: CallbackQuery, db: Database):
    hours = int(call.data.split(":")[2])
    if hours not in (1, 2, 6, 12, 24):
        await call.answer()
        return
    await db.set_setting("backup_interval_hours", str(hours))
    text, markup = await _settings_screen(db)
    await _show(call, text, markup)
    await call.answer(f"Har {hours} soatda")


@router.callback_query(F.data.regexp(r"^adm:bk_keep:\d+$"))
async def bk_keep(call: CallbackQuery, db: Database):
    keep = int(call.data.split(":")[2])
    if keep not in (5, 10, 20):
        await call.answer()
        return
    await db.set_setting("backup_keep", str(keep))
    text, markup = await _settings_screen(db)
    await _show(call, text, markup)
    await call.answer(f"{keep} ta nusxa saqlanadi")


@router.callback_query(F.data == "adm:bk_now")
async def bk_now(call: CallbackQuery, db: Database):
    svc = backup_service.get()
    if svc is None:
        await call.answer("Zaxira xizmati ishga tushmagan.", show_alert=True)
        return
    await call.answer("Nusxa olinmoqda...")
    try:
        path = await svc.run("qo'lda", force=True)
    except Exception as e:
        logger.exception("Qo'lda zaxira xato")
        await call.message.answer(f"❌ Zaxira olishda xatolik: {html.escape(str(e)[:200], quote=False)}")
        return
    if path:
        await call.message.answer("✅ Zaxira nusxa olindi va yuborildi.")
    text, markup = await _settings_screen(db)
    await _show(call, text, markup)
