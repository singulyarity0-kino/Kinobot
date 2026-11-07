"""
💳 Avtomatik to'lov — Telegram Bot Payments.

Ikki usul (ikkalasi ham to'lov tushishi bilan VIP'ni o'zi beradi, admin tasdiqlashi shart emas):
  * 💳 Karta (so'm): BotFather → Payments orqali ulangan provayder (masalan Click). Token — PAYMENT_PROVIDER_TOKEN.
  * ⭐ Telegram Stars: provayder kerak emas; narx yulduzlarda (Admin panel → 💎 VIP voronka).

Qo'lda usul (kartaga o'tkazib chek yuborish) ham qoladi — admin paneldan o'chirib qo'yish mumkin.
"""
from __future__ import annotations

import logging
import os

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, LabeledPrice, Message, PreCheckoutQuery

import funnel
import keyboards as kb
from database import Database
from handlers.common import fmt_number

logger = logging.getLogger("kino_bot.payments")
router = Router(name="payments")


def provider_token() -> str:
    return os.getenv("PAYMENT_PROVIDER_TOKEN", "").strip()


def card_enabled() -> bool:
    return bool(provider_token())


async def stars_price(db: Database, months: int) -> int:
    return max(0, await funnel.int_setting(db, f"stars_price_{months}", 0))


async def available_methods(db: Database, telegram_id: int, months: int) -> dict:
    """{'price': so'm narxi, 'card': bool, 'stars': yulduz soni (0 = yo'q), 'manual': bool}"""
    prices, _discount = await funnel.get_prices(db, telegram_id)
    price = prices.get(months, 0)
    return {
        "price": price,
        "card": card_enabled() and price > 0,
        "stars": await stars_price(db, months),
        "manual": await db.get_setting("manual_pay_enabled", "1") == "1",
    }


# ------------------------------------------------------------------ #
# Hisob-faktura yuborish
# ------------------------------------------------------------------ #
@router.callback_query(F.data.regexp(r"^pay:card:[123]$"))
async def pay_card(call: CallbackQuery, db: Database, bot: Bot):
    months = int(call.data.split(":")[2])
    m = await available_methods(db, call.from_user.id, months)
    if not m["card"]:
        await call.answer("Karta bilan to'lov hozir mavjud emas.", show_alert=True)
        return
    await call.answer()
    await bot.send_invoice(
        chat_id=call.from_user.id,
        title=f"💎 VIP — {months} oy",
        description="Barcha VIP kinolar, premyeralar va yuqori sifat. To'lovdan so'ng VIP avtomatik yoqiladi.",
        payload=f"vip:{months}:card",
        provider_token=provider_token(),
        currency="UZS",
        prices=[LabeledPrice(label=f"VIP {months} oy", amount=m["price"] * 100)],  # so'm -> tiyin
    )


@router.callback_query(F.data.regexp(r"^pay:stars:[123]$"))
async def pay_stars(call: CallbackQuery, db: Database, bot: Bot):
    months = int(call.data.split(":")[2])
    stars = await stars_price(db, months)
    if stars <= 0:
        await call.answer("Stars bilan to'lov bu tarif uchun mavjud emas.", show_alert=True)
        return
    await call.answer()
    await bot.send_invoice(
        chat_id=call.from_user.id,
        title=f"💎 VIP — {months} oy",
        description="Barcha VIP kinolar, premyeralar va yuqori sifat. To'lovdan so'ng VIP avtomatik yoqiladi.",
        payload=f"vip:{months}:stars",
        provider_token="",          # Stars uchun provayder token kerak emas
        currency="XTR",
        prices=[LabeledPrice(label=f"VIP {months} oy", amount=stars)],
    )


# ------------------------------------------------------------------ #
# To'lovdan oldingi tekshiruv (10 soniya ichida javob berish shart)
# ------------------------------------------------------------------ #
def _parse_payload(payload: str) -> tuple[int, str] | None:
    parts = (payload or "").split(":")
    if len(parts) == 3 and parts[0] == "vip" and parts[1] in ("1", "2", "3") and parts[2] in ("card", "stars"):
        return int(parts[1]), parts[2]
    return None


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery, db: Database, bot: Bot):
    parsed = _parse_payload(query.invoice_payload)
    if not parsed:
        await bot.answer_pre_checkout_query(query.id, ok=False, error_message="To'lov ma'lumoti noto'g'ri.")
        return
    months, method = parsed
    m = await available_methods(db, query.from_user.id, months)
    if method == "stars":
        expected, currency = m["stars"], "XTR"
    else:
        expected, currency = m["price"] * 100, "UZS"
    if expected <= 0 or query.currency != currency or query.total_amount != expected:
        await bot.answer_pre_checkout_query(
            query.id, ok=False,
            error_message="Narx o'zgargan. Iltimos, «💎 VIP olish» bo'limidan qaytadan urinib ko'ring.",
        )
        return
    await bot.answer_pre_checkout_query(query.id, ok=True)


# ------------------------------------------------------------------ #
# To'lov o'tdi — VIP berish
# ------------------------------------------------------------------ #
@router.message(F.successful_payment)
async def on_paid(message: Message, db: Database, bot: Bot):
    sp = message.successful_payment
    parsed = _parse_payload(sp.invoice_payload)
    tg = message.from_user.id
    if not parsed:
        await _alert_admins(bot, db, f"⚠️ Noma'lum to'lov payload: {sp.invoice_payload} | {tg} | {sp.telegram_payment_charge_id}")
        return
    months, method = parsed
    charge_id = sp.telegram_payment_charge_id

    if not await db.record_auto_payment(charge_id, tg, months, sp.total_amount, sp.currency, method):
        return  # bu to'lov allaqachon qayta ishlangan (takroriy xabar)

    # So'mdagi ekvivalent (statistika uchun): karta — haqiqiy summa, Stars — tarifning so'm narxi
    if sp.currency == "UZS":
        som = sp.total_amount // 100
    else:
        som = await funnel.int_setting(db, f"vip_price_{months}", 0)
    try:
        until = await db.grant_vip(tg, months, som, source="auto" if method == "card" else "stars")
        await db.attribute_last_purchase(tg)
    except Exception:
        logger.exception("To'lov o'tdi, lekin VIP berilmadi: %s", charge_id)
        await db.delete_auto_payment(charge_id)
        await _alert_admins(
            bot, db,
            f"❗ To'lov o'tdi, lekin VIP berilmadi! Foydalanuvchi: {tg}, {months} oy, {sp.total_amount} {sp.currency}, "
            f"charge_id: {charge_id}. VIP'ni qo'lda bering.",
        )
        await message.answer("✅ To'lov qabul qilindi. VIP yoqilishida kechikish bo'ldi — admin tez orada yoqadi.")
        return

    user = await db.get_user(tg)
    await message.answer(
        f"✅ <b>To'lov qabul qilindi!</b>\n💎 VIP {months} oy faollashtirildi.\n"
        f"⏳ Tugash vaqti: {funnel.local_str(until)}",
        reply_markup=kb.main_menu(is_admin=bool(user and user["is_admin"])),
    )
    amount_txt = f"{fmt_number(som)} so'm" if method == "card" else f"{sp.total_amount} ⭐ (≈ {fmt_number(som)} so'm)"
    await _alert_admins(
        bot, db,
        f"💰 <b>Avtomatik to'lov</b>\n👤 {message.from_user.full_name} (<code>{tg}</code>)\n"
        f"💎 {months} oy — {amount_txt}",
    )


async def _alert_admins(bot: Bot, db: Database, text: str) -> None:
    for admin_id in await db.all_admin_ids():
        try:
            await bot.send_message(admin_id, text)
        except Exception:  # noqa: BLE001
            pass
