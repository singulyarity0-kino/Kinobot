"""
Tugmalarni rangli qilish (Telegram Bot API 9.4+: InlineKeyboardButton/KeyboardButton "style").

Telegram faqat 3 xil rangni qo'llab-quvvatlaydi:
  "success" — yashil, "danger" — qizil, "primary" — ko'k.

Har bir tugma uchun rang tugma MATNIga qarab avtomatik tanlanadi (pick_style).
Bot yuboradigan har bir xabardagi klaviatura shu yerdagi middleware orqali ranglanadi,
shuning uchun keyboards.py dagi tugmalarni birma-bir o'zgartirish shart emas.
Rangni qo'lda belgilash kerak bo'lsa, tugmaga style="..." berish yetarli (u o'zgartirilmaydi).
Eski Telegram ilovalarida rang ko'rinmaydi, lekin tugmalar odatdagidek ishlayveradi.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup

logger = logging.getLogger("anime_bot.button_style")

# Qizil: o'chirish, bekor qilish, rad etish va shunga o'xshash "xavfli" amallar
_DANGER = ("❌", "🗑", "🛑", "bekor", "rad etish", "olib tashlash", "to'xtat", "o'chir")
# Yashil: ko'rish/boshlash, tasdiqlash, qo'shish, VIP
_SUCCESS = ("✅", "▶️", "💎", "➕", "🎬", "🛡", "tayyor", "davom")
# Rangsiz (odatiy) qoladigan navigatsiya tugmalari: ⬅️ ➡️ 🔜
_NAV_EXACT = {"⬅️", "➡️", "🔜", "◀️", "⬅", "➡"}


def pick_style(text: Optional[str]) -> Optional[str]:
    """Tugma matniga qarab rang tanlaydi. None = odatiy (rangsiz)."""
    t = (text or "").strip()
    if not t:
        return None
    # Raqamli tugmalar (qism raqamlari, "2/10" sahifa, "5⭐" baho) va orqaga/oldinga — rangsiz
    if re.fullmatch(r"[\d\s/\-.,]+", t) or re.match(r"^\d+\s*⭐", t):
        return None
    if t in _NAV_EXACT or t.startswith(("⬅️", "⬅")):
        return None
    low = t.lower()
    if any(k in low for k in _DANGER):
        return "danger"
    if any(k in low for k in _SUCCESS):
        return "success"
    return "primary"


def _colorize_rows(rows):
    new_rows = []
    for row in rows:
        new_row = []
        for btn in row:
            if getattr(btn, "style", None):  # qo'lda berilgan rang saqlanadi
                new_row.append(btn)
                continue
            style = pick_style(getattr(btn, "text", ""))
            new_row.append(btn.model_copy(update={"style": style}) if style else btn)
        new_rows.append(new_row)
    return new_rows


def colorize_markup(markup):
    if isinstance(markup, InlineKeyboardMarkup):
        return markup.model_copy(update={"inline_keyboard": _colorize_rows(markup.inline_keyboard)})
    if isinstance(markup, ReplyKeyboardMarkup):
        return markup.model_copy(update={"keyboard": _colorize_rows(markup.keyboard)})
    return markup


class ButtonColorMiddleware(BaseRequestMiddleware):
    """Bot yuboradigan har bir so'rovdagi klaviaturaga rang beradi. Xatolik bo'lsa so'rov o'zgarishsiz yuboriladi."""

    async def __call__(self, make_request, bot, method):
        try:
            markup = getattr(method, "reply_markup", None)
            if isinstance(markup, (InlineKeyboardMarkup, ReplyKeyboardMarkup)):
                method = method.model_copy(update={"reply_markup": colorize_markup(markup)})
        except Exception:
            logger.exception("Tugmalarni ranglashda xatolik (so'rov o'zgarishsiz yuboriladi)")
        return await make_request(bot, method)
