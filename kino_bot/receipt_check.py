"""
🤖 Chek tekshiruvi: Gemini chek rasmini o'qiydi, summa/karta/sana to'g'ri bo'lsa — vaqtinchalik VIP.

Qoidalar (xavfsizlik uchun qattiq):
  * Vaqtinchalik VIP faqat HAMMA tekshiruv o'tsa beriladi (rasm haqiqiy chek, status muvaffaqiyatli,
    summa NOYOB summaga aynan teng, karta oxiri mos, sana yaqinda, ishonch >= 0.8).
  * Har foydalanuvchiga bir marta; bir xil chek rasmi ikki marta o'tmaydi.
  * Ishonchsiz / Gemini ishlamasa — VIP BERILMAYDI, chek oddiy tartibda adminga ketadi.
  * Doimiy VIP faqat admin tasdiqlagach beriladi.
"""
from __future__ import annotations

import datetime
import logging
import re
from dataclasses import dataclass

from aiogram import Bot

import config
import gemini
from database import Database

logger = logging.getLogger("kino_bot.receipt_check")

MIN_CONFIDENCE = 0.8


@dataclass
class ReceiptVerdict:
    ok: bool          # hamma tekshiruv o'tdimi (vaqtinchalik VIP berish mumkin)
    note: str         # adminga ko'rsatiladigan qisqa natija


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


async def evaluate(bot: Bot, db: Database, file_id: str, expected_amount: int) -> ReceiptVerdict:
    if await db.get_setting("ai_receipt_enabled", "1") != "1":
        return ReceiptVerdict(False, "🤖 AI tekshiruvi o'chiq")
    if not gemini.is_configured():
        return ReceiptVerdict(False, "🤖 AI sozlanmagan (GEMINI_API_KEY yo'q)")
    try:
        buf = await bot.download(file_id)
        data = buf.getvalue() if buf else b""
    except Exception as e:  # noqa: BLE001
        logger.warning("Chek rasmini yuklab bo'lmadi: %s", e)
        return ReceiptVerdict(False, "🤖 Rasmni yuklab bo'lmadi")
    if not data:
        return ReceiptVerdict(False, "🤖 Rasm bo'sh")

    card = await db.get_setting("payment_card_number", "")
    holder = await db.get_setting("payment_card_holder", "")
    res = await gemini.verify_receipt(data, "image/jpeg", expected_amount, card, holder)
    if not res:
        return ReceiptVerdict(False, "🤖 AI javob bermadi (limit yoki xato) — qo'lda tekshiring")

    problems: list[str] = []
    if res.get("is_receipt") is not True:
        problems.append("chek emas")
    if res.get("success") is False:
        problems.append("to'lov holati muvaffaqiyatsiz")
    try:
        amount = int(res.get("amount")) if res.get("amount") is not None else None
    except (TypeError, ValueError):
        amount = None
    if amount is None:
        problems.append("summa o'qilmadi")
    elif amount != expected_amount:
        problems.append(f"summa mos emas ({amount} ≠ {expected_amount})")

    tail_ai = _digits(str(res.get("card_tail") or ""))[-4:]
    tail_ours = _digits(card)[-4:]
    if tail_ours and tail_ai and tail_ai != tail_ours:
        problems.append(f"karta mos emas (…{tail_ai})")

    raw_date = str(res.get("date") or "")
    if raw_date:
        try:
            d = datetime.date.fromisoformat(raw_date[:10])
            today = datetime.datetime.now(config.LOCAL_TZ).date()
            if abs((today - d).days) > 1:
                problems.append(f"sana eski ({raw_date[:10]})")
        except ValueError:
            problems.append("sana noaniq")

    try:
        conf = float(res.get("confidence") or 0)
    except (TypeError, ValueError):
        conf = 0.0
    if conf < MIN_CONFIDENCE:
        problems.append(f"ishonch past ({conf:.2f})")

    notes = str(res.get("notes") or "")[:120]
    if problems:
        return ReceiptVerdict(False, "🤖 AI ishonchsiz: " + "; ".join(problems) + (f" · {notes}" if notes else ""))
    return ReceiptVerdict(True, f"🤖 AI tekshirdi: summa {amount} so'm mos, ishonch {conf:.2f}")
