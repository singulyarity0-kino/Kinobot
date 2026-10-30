"""
Test2 Bot — Webhook va Aiohttp Server.
"""
from __future__ import annotations
import os
import asyncio
import datetime
import logging
import random
from logging.handlers import RotatingFileHandler

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

import alerts
import archive  # noqa: F401  (video zaxira kanali)
import backup_service
import config
import funnel
from button_style import ButtonColorMiddleware
from database import Database
from handlers import admin, admin_kino, admin_more, tmdb_ui, backup_ui, discover, referral_ui, user
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


async def daily_random_announcement(bot: Bot, db: Database) -> None:
    """
    Har kuni admin paneldan belgilangan vaqtda (O'zbekiston vaqti) tasodifiy kino e'loni yuboriladi.
    Har 30 soniyada tekshiriladi; kuniga bir marta; bot belgilangan vaqtdan keyin 3 soat ichida
    uyg'onsa ham yuboradi (Render uxlab qolgan holat uchun).
    """
    from handlers.admin import _post_announcement
    from handlers.common import parse_hhmm

    while True:
        await asyncio.sleep(30)
        try:
            if not config.MAIN_CHANNEL:
                continue
            if await db.get_setting("daily_post_enabled", "1") != "1":
                continue
            now = datetime.datetime.now(config.LOCAL_TZ)
            today = now.strftime("%Y-%m-%d")
            if await db.get_setting("daily_post_last_date", "") == today:
                continue
            hhmm = parse_hhmm(await db.get_setting("daily_post_time", "12:00")) or "12:00"
            hour, minute = int(hhmm[:2]), int(hhmm[3:])
            scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if now < scheduled or now - scheduled > datetime.timedelta(hours=3):
                continue
            # Avval belgilab olamiz — xatolik bo'lsa ham bir kunda qayta-qayta urinmaydi
            await db.set_setting("daily_post_last_date", today)
            rows = await db.all_anime_for_random()
            if not rows:
                continue
            anime = random.choice(rows)
            await _post_announcement(bot, db, anime["id"])
            logger.info("Kunlik random kino e'loni yuborildi: %s", anime["title"])
        except Exception as e:
            await alerts.report(bot, "daily_random_announcement", e)


def main() -> None:
    setup_logging()

    if not config.BOT_TOKEN:
        raise SystemExit("BOT_TOKEN topilmadi. .env faylida BOT_TOKEN=... qiymatini kiriting.")

    render_url = os.getenv("RENDER_EXTERNAL_URL")
    if not render_url:
        raise SystemExit("RENDER_EXTERNAL_URL topilmadi.")

    webhook_path = f"/webhook/{config.BOT_TOKEN}"
    webhook_url = f"{render_url}{webhook_path}"

    db = Database(config.DB_PATH)

    bot = Bot(
        token=config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    # Barcha tugmalarga rang beradi (yashil/qizil/ko'k) — button_style.py
    bot.session.middleware(ButtonColorMiddleware())
    dp = Dispatcher(storage=MemoryStorage())

    # db obyektini barcha handlerlarga uzatamiz
    dp["db"] = db

    # Har bir harakatda majburiy obunani qayta tekshiradigan middleware
    # (Inner) middleware — shunda data["state"] (FSMContext) kafolatlangan holda
    # mavjud bo'ladi, bu "💎 VIP olish" oqimini obunasiz ham ishlashi uchun kerak.
    dp.message.middleware(SubscriptionMiddleware())
    dp.callback_query.middleware(SubscriptionMiddleware())

    dp.include_router(admin.router)
    dp.include_router(admin_more.router)
    dp.include_router(admin_kino.router)
    dp.include_router(tmdb_ui.router)
    dp.include_router(backup_ui.router)
    dp.include_router(referral_ui.admin_router)
    dp.include_router(referral_ui.router)
    dp.include_router(discover.router)
    dp.include_router(user.router)

    # Kutilmagan xatolar haqida SUPER_ADMIN_IDS ga xabar (bir xil xato 10 daqiqada bir marta)
    dp.errors.register(alerts.build_error_handler())

    background_tasks = []

    async def on_startup(app: web.Application) -> None:
        await db.connect()
        backup = backup_service.init(bot, db)
        # allowed_updates: handlerlari bor barcha turlar (shu jumladan inline_query — "📤 Do'stlarga ulashish" uchun)
        await bot.set_webhook(
            webhook_url,
            drop_pending_updates=False,
            allowed_updates=dp.resolve_used_update_types(),
        )
        logger.info("Webhook o'rnatildi: %s", webhook_url)

        # Orqa fondagi (background) vazifalarni ishga tushiramiz
        background_tasks.extend([
            asyncio.create_task(vip_expiry_watcher(bot, db)),
            asyncio.create_task(funnel.vip_reminder_watcher(bot, db)),
            asyncio.create_task(backup.scheduler_loop()),
            asyncio.create_task(backup.startup_check()),
            asyncio.create_task(daily_random_announcement(bot, db)),
        ])

    async def on_cleanup(app: web.Application) -> None:
        for task in background_tasks:
            task.cancel()
        await db.close()
        await bot.session.close()

    app = web.Application()

    # Health endpoint: UptimeRobot shu manzilga so'rov yuborib, botni uyg'oq saqlaydi
    async def health(request: web.Request) -> web.Response:
        return web.Response(text="ok")

    app.router.add_get("/", health)

    # Aiogram tayyor webhook ishlovchisi
    webhook_requests_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
    )
    webhook_requests_handler.register(app, path=webhook_path)
    setup_application(app, dp, bot=bot)

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    port = int(os.getenv("PORT", "10000"))

    web.run_app(
        app,
        host="0.0.0.0",
        port=port
    )


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot to'xtatildi.")
