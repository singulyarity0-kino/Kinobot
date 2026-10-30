"""
Referal tizimi mantig'i (handlerlarga bog'liq emas — user.py va common.py shu yerdan foydalanadi).

Qoidalar:
  * Har bir foydalanuvchining shaxsiy havolasi: t.me/<bot>?start=ref_<uning Telegram ID'si>
  * Do'st botga BIRINCHI MARTA kirganda taklif qiluvchi yoziladi (mavjud foydalanuvchilar hisoblanmaydi).
  * Mukofot do'st majburiy kanallarga obuna bo'lgach beriladi (soxta akkauntlardan himoya).
  * Taklif qilgan odamga referral_inviter_days, taklif qilingan do'stga referral_invitee_days kun VIP.
  * Taklif qiluvchi bir oyda ko'pi bilan referral_monthly_limit ta do'st uchun mukofot oladi.
"""
from __future__ import annotations

import html
import logging
from typing import Optional

from aiogram import Bot

from database import Database

logger = logging.getLogger("anime_bot.referral")
_username_cache: dict[str, str] = {}


async def get_settings(db: Database) -> dict:
    async def _int(key: str, default: int) -> int:
        try:
            return max(0, int(await db.get_setting(key, str(default))))
        except ValueError:
            return default

    return {
        "enabled": (await db.get_setting("referral_enabled", "1")) == "1",
        "inviter_days": await _int("referral_inviter_days", 7),
        "invitee_days": await _int("referral_invitee_days", 2),
        "monthly_limit": await _int("referral_monthly_limit", 30),
    }


async def _bot_username(bot: Bot) -> str:
    if "u" not in _username_cache:
        _username_cache["u"] = (await bot.get_me()).username
    return _username_cache["u"]


async def build_link(bot: Bot, telegram_id: int) -> str:
    return f"https://t.me/{await _bot_username(bot)}?start=ref_{telegram_id}"


async def build_plain_link(bot: Bot) -> str:
    """Referalsiz oddiy bot havolasi (referal tizimi o'chiq bo'lganda ulashish uchun)."""
    return f"https://t.me/{await _bot_username(bot)}"


async def register_from_payload(db: Database, invitee_id: int, payload: str) -> bool:
    """/start ref_<id> dan taklif qiluvchini yozadi. Faqat YANGI foydalanuvchi uchun chaqiriladi."""
    if not payload.startswith("ref_"):
        return False
    raw = payload[len("ref_"):]
    if not raw.isdigit():
        return False
    cfg = await get_settings(db)
    if not cfg["enabled"]:
        return False
    return await db.register_referral(invitee_id, int(raw))


async def _safe_send(bot: Bot, chat_id: int, text: str) -> None:
    try:
        await bot.send_message(chat_id, text)
    except Exception:
        pass  # foydalanuvchi botni bloklagan bo'lishi mumkin


async def process_referral_reward(bot: Bot, db: Database, invitee_id: int) -> None:
    """
    Agar bu foydalanuvchi taklif orqali kelgan va mukofot hali berilmagan bo'lsa — berib, ikkala tomonga xabar yuboradi.
    Faqat obuna tekshiruvidan o'tgan foydalanuvchi uchun chaqirilishi kerak. Xatolik botni to'xtatmaydi.
    """
    try:
        if not await db.get_pending_referral(invitee_id):  # arzon tekshiruv: ko'pchilik uchun shu yerda tugaydi
            return
        cfg = await get_settings(db)
        if not cfg["enabled"]:
            return
        res = await db.claim_referral_reward(
            invitee_id, cfg["inviter_days"], cfg["invitee_days"], cfg["monthly_limit"]
        )
        if not res:
            return
        invitee = await db.get_user(invitee_id)
        name = html.escape((invitee["full_name"] if invitee else "") or "Do'stingiz")

        if res["invitee_days"] > 0 and res["invitee_until"]:
            await _safe_send(
                bot, invitee_id,
                f"🎁 Siz do'stingiz taklifi orqali kirdingiz va <b>{res['invitee_days']} kun VIP</b> sovg'a oldingiz!\n"
                f"⏳ VIP tugash sanasi: {res['invitee_until'].strftime('%Y-%m-%d')}",
            )
        if res["inviter_days"] > 0 and res["inviter_until"]:
            await _safe_send(
                bot, res["inviter_id"],
                f"🎉 <b>{name}</b> sizning havolangiz orqali botga qo'shildi!\n"
                f"💎 Sizga <b>+{res['inviter_days']} kun VIP</b> berildi.\n"
                f"⏳ VIP tugash sanasi: {res['inviter_until'].strftime('%Y-%m-%d')}",
            )
        else:
            await _safe_send(
                bot, res["inviter_id"],
                f"✅ <b>{name}</b> sizning havolangiz orqali qo'shildi, lekin shu oydagi taklif "
                f"chegarasiga yetganingiz uchun VIP berilmadi. Yangi oyda yana davom eting!",
            )
    except Exception:
        logger.exception("Referal mukofotini berishda xatolik (invitee=%s)", invitee_id)
