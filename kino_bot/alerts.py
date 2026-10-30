"""
Xatolar va muhim hodisalar haqida SUPER_ADMIN_IDS ga Telegram orqali xabar berish.

- Bir xil xato 10 daqiqada ko'pi bilan bir marta yuboriladi (xabarlar to'lib ketmasligi uchun).
- Soatiga ko'pi bilan 20 ta xabar.
- Foydalanuvchi botni bloklagani, "message is not modified" kabi zararsiz xatolar yuborilmaydi.
"""
from __future__ import annotations

import html
import logging
import time
import traceback
from typing import Optional

import config

logger = logging.getLogger("anime_bot.alerts")

COOLDOWN_SECONDS = 600
MAX_PER_HOUR = 20

_last_sent: dict[str, float] = {}
_sent_times: list[float] = []

_NOISE = (
    "message is not modified",
    "query is too old",
    "query id is invalid",
    "message to edit not found",
    "message to delete not found",
    "message can't be deleted",
    "message can't be edited",
    "bot was blocked by the user",
    "user is deactivated",
    "bot can't initiate conversation",
    "have no rights to send a message",
)


def _esc(value: object, limit: int = 300) -> str:
    return html.escape(str(value)[:limit], quote=False)


def is_noise(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(part in text for part in _NOISE)


def _allowed(key: str, cooldown: float) -> bool:
    global _sent_times
    now = time.time()
    if now - _last_sent.get(key, 0.0) < cooldown:
        return False
    _sent_times = [t for t in _sent_times if now - t < 3600]
    if len(_sent_times) >= MAX_PER_HOUR:
        return False
    _last_sent[key] = now
    _sent_times.append(now)
    return True


async def notify_admins(bot, text: str, key: Optional[str] = None, cooldown: float = COOLDOWN_SECONDS) -> None:
    """`text` — HTML formatida bo'lishi mumkin (dinamik qismlar html.escape qilingan bo'lishi kerak)."""
    if not _allowed(key or text[:80], cooldown):
        return
    for admin_id in config.SUPER_ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text, disable_web_page_preview=True)
        except Exception:
            logger.warning("Adminga ogohlantirish yuborib bo'lmadi: %s", admin_id)


async def report(bot, where: str, exc: BaseException) -> None:
    """Xatoni logga yozadi va (zararsiz bo'lmasa) adminlarga yuboradi."""
    logger.error("%s: %s: %s", where, type(exc).__name__, exc, exc_info=exc)
    if is_noise(exc):
        return
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-1200:]
    key = f"{where}|{type(exc).__name__}|{str(exc)[:60]}"
    text = (
        "⚠️ <b>Bot xatosi</b>\n"
        f"📍 {_esc(where, 120)}\n"
        f"<code>{_esc(type(exc).__name__, 80)}: {_esc(exc)}</code>\n\n"
        f"<pre>{_esc(tb, 1200)}</pre>"
    )
    await notify_admins(bot, text, key=key)


def _describe_update(update) -> str:
    try:
        if update is None:
            return "noma'lum"
        cq = getattr(update, "callback_query", None)
        if cq is not None:
            return f"tugma: {getattr(cq, 'data', '')}"
        msg = getattr(update, "message", None)
        if msg is not None:
            txt = (getattr(msg, "text", None) or getattr(msg, "caption", None) or "").strip()
            return f"xabar: {txt[:40]}" if txt else "xabar (matnsiz)"
        iq = getattr(update, "inline_query", None)
        if iq is not None:
            return "inline so'rov"
    except Exception:
        pass
    return "update"


def build_error_handler():
    """dp.errors.register(...) uchun umumiy xato ushlagich."""

    async def on_error(event, bot) -> bool:
        exc = getattr(event, "exception", None)
        if exc is not None:
            await report(bot, _describe_update(getattr(event, "update", None)), exc)
        return True

    return on_error
