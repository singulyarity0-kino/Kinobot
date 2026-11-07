"""
💎 VIP voronka: foydalanuvchini majburlamasdan, qiziqtirib VIP'ga olib keladigan mexanizmlar.

  * kunlik bepul limit (oddiy foydalanuvchi kuniga N ta kino ocha oladi)
  * yangi foydalanuvchiga bir martalik bepul VIP sinov
  * qulflangan VIP kartalar: poster/tavsif/treyler ko'rinadi, video qulflangan
  * narx + "kuniga necha so'm" xabari
  * VIP'ni qayta olganlarga chegirma
  * VIP tugashiga 3 va 1 kun qolganda eslatma
Barcha qiymatlar admin paneldan: ⚙️ Admin panel → 💎 VIP voronka.
"""
from __future__ import annotations

import asyncio
import datetime
import logging
from typing import Optional

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup

import alerts
import config
import keyboards as kb
from database import Database
from handlers.common import fmt_number, has_vip_access

logger = logging.getLogger("kino_bot.funnel")


async def int_setting(db: Database, key: str, default: int = 0) -> int:
    try:
        return int(str(await db.get_setting(key, str(default))).strip() or default)
    except ValueError:
        return default


def local_str(dt_utc: datetime.datetime) -> str:
    """UTC vaqtni bot mintaqasidagi (O'zbekiston) ko'rinishga o'tkazadi."""
    return (dt_utc + datetime.timedelta(hours=config.TZ_OFFSET_HOURS)).strftime("%d.%m.%Y %H:%M")


# ------------------------------------------------------------------ #
# Narxlar va chegirma
# ------------------------------------------------------------------ #
async def get_prices(db: Database, telegram_id: int) -> tuple[dict[int, int], int]:
    """
    {oy: narx} va qo'llangan chegirma foizi.
    Chegirma: hozir VIP bo'lganlarga yoki ilgari VIP xarid qilganlarga (qayta xarid).
    """
    percent = await int_setting(db, "renewal_discount", 0)
    percent = max(0, min(percent, 90))
    discount = 0
    if percent and (await db.is_vip(telegram_id) or await db.has_paid_before(telegram_id)):
        discount = percent
    prices: dict[int, int] = {}
    for months in (1, 2, 3):
        base = await int_setting(db, f"vip_price_{months}", 0)
        price = base * (100 - discount) / 100
        prices[months] = int(round(price / 100.0)) * 100 if discount else int(base)
    return prices, discount


def per_day_text(price: int, months: int) -> str:
    days = config.VIP_DAYS.get(months, 30 * months)
    if price <= 0 or days <= 0:
        return ""
    per_day = int(round(price / days / 100.0)) * 100 or int(price / days)
    return f"≈ kuniga {fmt_number(per_day)} so'm"


async def vip_offer(db: Database, telegram_id: int, header: str) -> tuple[str, InlineKeyboardMarkup]:
    """Sarlavha + narxlar (kuniga necha so'm) + xarid tugmalari."""
    prices, discount = await get_prices(db, telegram_id)
    lines = [header, ""]
    for months in (1, 2, 3):
        if prices[months] > 0:
            tail = per_day_text(prices[months], months)
            star = " ⭐ eng foydali" if months == 3 else ""
            lines.append(f"💎 {months} oy — <b>{fmt_number(prices[months])} so'm</b> {tail}{star}".rstrip())
    if discount:
        lines.append(f"\n🎉 Sizga qayta xarid uchun <b>-{discount}%</b> chegirma qo'llandi!")
    lines.append("\n💎 VIP bilan: barcha VIP kinolar, premyeralar, cheklovsiz ko'rish.")
    return "\n".join(lines), kb.vip_menu({m: fmt_number(p) for m, p in prices.items()}, discount=discount, with_referral=True)


# ------------------------------------------------------------------ #
# Kirish huquqi (kunlik limit / VIP)
# ------------------------------------------------------------------ #
async def early_unlock(db: Database, anime) -> Optional[datetime.datetime]:
    """Oddiy foydalanuvchiga qachon ochilishi (UTC); kechikish yo'q yoki o'tgan bo'lsa None."""
    hours = await int_setting(db, "early_access_hours", 0)
    return db.early_unlock_at(anime, hours)


def fmt_remaining(unlock_utc: datetime.datetime) -> str:
    left = max(0, int((unlock_utc - datetime.datetime.utcnow()).total_seconds()))
    days, rem = divmod(left, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days} kun {hours} soat"
    if hours:
        return f"{hours} soat {minutes} daqiqa"
    return f"{max(minutes, 1)} daqiqa"


async def lock_info(db: Database, telegram_id: int, anime) -> tuple[str, str]:
    """(sabab, karta izohi): sabab '' | 'vip' | 'early'. VIP foydalanuvchida har doim ('', '')."""
    if await has_vip_access(db, telegram_id):
        return "", ""
    if anime["is_vip"]:
        return "vip", "🔒 Ko'rish uchun VIP kerak"
    unlock = await early_unlock(db, anime)
    if unlock:
        return "early", (
            f"⏳ Yangi kino! Hamma uchun {fmt_remaining(unlock)} dan keyin ochiladi.\n"
            "💎 VIP bilan hoziroq ko'ring."
        )
    return "", ""


async def check_access(db: Database, telegram_id: int, anime) -> tuple[bool, str]:
    """
    (ruxsat, sabab). sabab: '' | 'vip' (VIP kino) | 'early' (yangi kino, kechikish) | 'limit' (kunlik limit).
    Bir kinoni bugun qayta ko'rish limitga qo'shilmaydi.
    """
    reason, _note = await lock_info(db, telegram_id, anime)
    if reason:
        return False, reason
    if await has_vip_access(db, telegram_id):
        return True, ""
    limit = await int_setting(db, "free_daily_limit", 0)
    if limit <= 0:
        return True, ""
    if await db.has_viewed_today(telegram_id, anime["id"]):
        return True, ""
    if await db.count_free_views_today(telegram_id) >= limit:
        return False, "limit"
    return True, ""


async def quality_locked(db: Database, telegram_id: int, version) -> bool:
    """Oddiy foydalanuvchi uchun bu sifat yopiqmi (sozlama: free_max_quality, 0 = cheklov yo'q)."""
    q = version["quality"] if "quality" in version.keys() else None
    if not q:
        return False
    max_free = await int_setting(db, "free_max_quality", 0)
    if max_free <= 0 or q <= max_free:
        return False
    return not await has_vip_access(db, telegram_id)


async def free_quota_line(db: Database, telegram_id: int) -> str:
    """Kartada ko'rsatiladigan '🎟 Bugungi bepul limit: 1/2' qatori (VIP'larga bo'sh)."""
    if await has_vip_access(db, telegram_id):
        return ""
    limit = await int_setting(db, "free_daily_limit", 0)
    if limit <= 0:
        return ""
    used = min(await db.count_free_views_today(telegram_id), limit)
    return f"🎟 Bugungi bepul limit: {used}/{limit}"


async def send_vip_offer(bot: Bot, db: Database, chat_id: int, telegram_id: int, reason: str, title: str = "",
                         anime_id: Optional[int] = None, quality: Optional[int] = None) -> None:
    limit = await int_setting(db, "free_daily_limit", 0)
    name = f"«{title}» — " if title else ""
    if reason == "limit":
        header = (
            f"⏳ <b>Bugungi bepul limit tugadi</b> ({limit}/{limit}).\n"
            "Ertaga yangi limit beriladi, yoki 💎 VIP bilan hoziroq cheksiz tomosha qiling:"
        )
    elif reason == "early":
        header = f"⏳ <b>{name}yangi kino</b>\nBu kino hamma uchun hali ochilmagan. 💎 VIP bilan hoziroq ko'rishingiz mumkin:"
        if anime_id:
            anime = await db.get_anime(anime_id)
            unlock = await early_unlock(db, anime) if anime else None
            if unlock:
                header = (
                    f"⏳ <b>{name}yangi kino</b>\n"
                    f"Hamma uchun <b>{fmt_remaining(unlock)}</b> dan keyin ochiladi "
                    f"({local_str(unlock)}).\n💎 VIP bilan hoziroq ko'rishingiz mumkin:"
                )
    elif reason == "quality":
        header = (
            f"💎 <b>{quality}p sifat — VIP uchun</b>\n"
            "Oddiy foydalanuvchilar pastroq sifatda ko'ra oladi. VIP bilan eng yuqori sifat ochiladi:"
        )
    else:
        header = f"💎 <b>{name}VIP kino</b>\nKo'rish uchun VIP obuna kerak:"
    if anime_id:
        await db.set_vip_trigger(telegram_id, anime_id, reason)
    text, markup = await vip_offer(db, telegram_id, header)
    await bot.send_message(chat_id, text, reply_markup=markup)


_bot_username: Optional[str] = None


async def get_bot_username(bot: Bot) -> str:
    """Bot username'i (ulashish havolasi uchun); bir marta so'raladi va eslab qolinadi."""
    global _bot_username
    if not _bot_username:
        _bot_username = (await bot.get_me()).username or ""
    return _bot_username


async def share_url(bot: Bot, anime) -> str:
    """Telegramning ulashish oynasini ochadigan havola: do'st bossa shu kinoga to'g'ri kiradi."""
    from urllib.parse import quote
    link = f"https://t.me/{await get_bot_username(bot)}?start=anime_{anime['anime_code']}"
    return f"https://t.me/share/url?url={quote(link, safe='')}&text={quote('🎬 ' + anime['title'], safe='')}"


# ------------------------------------------------------------------ #
# Bepul sinov
# ------------------------------------------------------------------ #
async def maybe_grant_trial(bot: Bot, db: Database, telegram_id: int) -> None:
    """Yangi foydalanuvchiga (majburiy obunadan o'tgach) bir martalik VIP sinov beradi."""
    try:
        if await db.get_setting("trial_enabled", "1") != "1":
            return
        days = await int_setting(db, "trial_days", 1)
        until = await db.grant_trial_if_new(telegram_id, days)
        if not until:
            return
        await bot.send_message(
            telegram_id,
            f"🎁 <b>Xush kelibsiz sovg'a!</b>\n\n"
            f"Sizga <b>{days} kunlik bepul VIP</b> berildi — barcha VIP kinolar ochiq.\n"
            f"⏳ Tugash vaqti: {local_str(until)}",
        )
    except Exception:  # noqa: BLE001 — sovg'a xabari yuborilmasa ham bot ishlayveradi
        logger.exception("Bepul sinov berishda xato: %s", telegram_id)


# ------------------------------------------------------------------ #
# VIP tugash eslatmalari
# ------------------------------------------------------------------ #
async def vip_reminder_watcher(bot: Bot, db: Database) -> None:
    """Har 15 daqiqada tekshiradi: VIP tugashiga 3 kun va 1 kun qolganlarga eslatma yuboradi."""
    while True:
        await asyncio.sleep(900)
        try:
            if await db.get_setting("vip_reminders_enabled", "1") != "1":
                continue
            for telegram_id, vip_until, kind in await db.due_vip_reminders():
                left = "3 kundan keyin" if kind == "3d" else "1 kundan kamroq vaqtda"
                header = f"⏳ <b>VIP obunangiz {left} tugaydi.</b>\nUzilishsiz tomosha qilish uchun muddatni uzaytiring:"
                try:
                    text, markup = await vip_offer(db, telegram_id, header)
                    await bot.send_message(telegram_id, text, reply_markup=markup)
                except Exception:  # noqa: BLE001 — bot bloklangan bo'lishi mumkin
                    pass
                await db.mark_vip_reminder(telegram_id, vip_until, kind)
                await asyncio.sleep(0.05)
        except Exception as e:  # noqa: BLE001
            await alerts.report(bot, "vip_reminder_watcher", e)
