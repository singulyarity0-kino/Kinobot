"""Telegram bilan ishlashda umumiy yordamchi funksiyalar."""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, TypeVar

from aiogram.exceptions import TelegramRetryAfter

logger = logging.getLogger("anime_bot.tg_utils")
T = TypeVar("T")


async def call_with_retry(factory: Callable[[], Awaitable[T]], tries: int = 3) -> T:
    """
    Telegram "flood limit" (429) bersa, ko'rsatilgan vaqt kutib, qayta urinadi.
    `factory` — har safar YANGI coroutine qaytaradigan funksiya (masalan: lambda: bot.send_message(...)).
    """
    last: Exception | None = None
    for _ in range(max(1, tries)):
        try:
            return await factory()
        except TelegramRetryAfter as e:
            last = e
            wait = min(float(e.retry_after) + 1.0, 120.0)
            logger.warning("Telegram limit: %s soniya kutilmoqda", wait)
            await asyncio.sleep(wait)
    assert last is not None
    raise last
