"""
Test2 Bot — ishga tushirish nuqtasi.

    python main.py

Ishga tushirishdan oldin:
    1. requirements.txt dagi paketlarni o'rnating: pip install -r requirements.txt
    2. .env.example asosida .env fayl yarating va BOT_TOKEN, SUPER_ADMIN_IDS
       qiymatlarini to'ldiring.
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import random
from logging.handlers import RotatingFileHandler

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

import alerts
import archive  # noqa: F401  (video zaxira kanali)
import backup_service
import config
import announce
import funnel
import payments
from button_style import ButtonColorMiddleware
from database import Database
from handlers import admin, admin_channels, admin_kino, admin_more, tmdb_ui, backup_ui, discover, referral_ui, user
from handlers.common import SubscriptionMiddleware

logger = logging.getLogger("anime_bot")


def setup_logging() -> None:
    handlers = [
        logging.StreamHandler(),
        RotatingFileHandler(config.LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"),
    ]
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        handlers=handlers,
    )
    # Kutubxonalarning haddan tashqari batafsil loglarini kamaytiramiz
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)


async def vip_expiry_watcher(bot: Bot, db: Database) -> None:
    """Har 10 daqiqada VIP muddati tugagan foydalanuvchilarni tekshiradi."""
    while True:
        try:
            expired_ids = await db.expire_vips()
            for tg_id in expired_ids:
                try:
                    await bot.send_message(
                        tg_id,
                        "⏳ 💎 VIP obunangiz muddati tugadi. Davom ettirish uchun "
                        "👤 Profil → 💎 VIP olish bo'limidan foydalaning.",
                    )
                except Exception:
                    pass
            if expired_ids:
                logger.info("VIP muddati tugagan foydalanuvchilar: %s", expired_ids)
        except Exception as e:
            await alerts.report(bot, "vip_expiry_watcher", e)
        await asyncio.sleep(600)


async def main() -> None:
    setup_logging()

    if not config.BOT_TOKEN:
        raise SystemExit(
            "BOT_TOKEN topilmadi. .env faylida BOT_TOKEN=... qiymatini kiriting."
        )

    db = Database(config.DB_PATH)
    await db.connect()

    bot = Bot(
        token=config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    # Barcha tugmalarga rang beradi (yashil/qizil/ko'k) — button_style.py
    bot.session.middleware(ButtonColorMiddleware())
    dp = Dispatcher(storage=MemoryStorage())

    # db obyektini barcha handlerlarga avtomatik uzatish
    dp["db"] = db

    # Har bir harakatda majburiy obunani qayta tekshiradigan middleware
    # (Inner) middleware — shunda data["state"] (FSMContext) kafolatlangan holda
    # mavjud bo'ladi, bu "💎 VIP olish" oqimini obunasiz ham ishlashi uchun kerak.
    dp.message.middleware(SubscriptionMiddleware())
    dp.callback_query.middleware(SubscriptionMiddleware())

    dp.include_router(admin.router)
    dp.include_router(admin_more.router)
    dp.include_router(admin_kino.router)
    dp.include_router(admin_channels.router)
    dp.include_router(tmdb_ui.router)
    dp.include_router(backup_ui.router)
    dp.include_router(referral_ui.admin_router)
    dp.include_router(referral_ui.router)
    dp.include_router(discover.router)  # admin filtri o'zida bo'lgani uchun avval ulaymiz
    dp.include_router(payments.router)
    dp.include_router(user.router)

    # Kutilmagan xatolar haqida SUPER_ADMIN_IDS ga xabar (bir xil xato 10 daqiqada bir marta)
    dp.errors.register(alerts.build_error_handler())

    dp.startup.register(lambda: logger.info("Bot ishga tushdi."))

    backup = backup_service.init(bot, db)
    background_tasks = [
        asyncio.create_task(vip_expiry_watcher(bot, db)),
        asyncio.create_task(funnel.vip_reminder_watcher(bot, db)),
        asyncio.create_task(backup.scheduler_loop()),
        asyncio.create_task(backup.startup_check()),
        asyncio.create_task(announce.daily_announcer(bot, db)),
        asyncio.create_task(announce.scheduled_poster(bot, db)),
        asyncio.create_task(announce.unlock_notifier(bot, db)),
    ]

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        for task in background_tasks:
            task.cancel()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot to'xtatildi.")
