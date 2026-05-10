import asyncio
import logging

from app.cot_broadcaster import cot_broadcaster
from app.database import engine
from app.models.models import Base
from app.startup_migrations import run_migrations
from app.telegram.bot_instance import bots
from app.telegram.config import BOTS_CONFIG, MINI_APP_URL, WEBHOOK_BASE_URL

logger = logging.getLogger(__name__)


async def on_startup() -> None:
    try:
        Base.metadata.create_all(bind=engine)
    except Exception as exc:
        logger.warning("create_all failed (tables may already exist): %s", exc)
    try:
        run_migrations(engine)
    except Exception as exc:
        logger.warning("migration failed: %s", exc)

    cot_broadcaster.set_loop(asyncio.get_running_loop())
    await _configure_telegram_bots()
    _start_background_tasks()

    from app.wechat.poller import start_polling as wechat_start
    await wechat_start()


async def on_shutdown() -> None:
    from app.wechat.poller import stop_polling as wechat_stop

    await wechat_stop()
    for bot in bots.values():
        try:
            await bot.delete_webhook()
        except Exception:
            pass
        try:
            await bot.session.close()
        except Exception:
            pass


async def _configure_telegram_bots() -> None:
    from aiogram.types import MenuButtonWebApp, WebAppInfo

    print(f"[startup] bots to register: {list(bots.keys())}")
    for key, bot in bots.items():
        webhook_url = f"{WEBHOOK_BASE_URL}{BOTS_CONFIG[key]['webhook_path']}"
        try:
            await bot.set_webhook(webhook_url, drop_pending_updates=True)
            print(f"[startup] Webhook set for {key}: {webhook_url}")
            logger.info("Telegram webhook set for %s: %s", key, webhook_url)
        except Exception as exc:
            print(f"[startup] Webhook FAILED for {key}: {exc}")
            logger.warning("Failed to set webhook for %s: %s", key, exc)
        try:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="WHISPER",
                    web_app=WebAppInfo(url=MINI_APP_URL),
                ),
            )
            print(f"[startup] Menu button set for {key}")
        except Exception as exc:
            print(f"[startup] Menu button FAILED for {key}: {exc}")
            logger.warning("Failed to set menu button for %s: %s", key, exc)


def _start_background_tasks() -> None:
    from app.routers.cot import cot_cleanup_loop
    from app.services.proactive_service import proactive_loop
    from app.services.reflection_service import reflection_loop
    from app.services.summary_service import daily_merge_cron

    asyncio.create_task(proactive_loop())
    logger.info("Proactive message loop started")

    from app.services.cafe_service import cafe_service
    cafe_service.start()
    logger.info("Cafe group chat service started")

    asyncio.create_task(reflection_loop())
    logger.info("Auto-reflection loop started")

    asyncio.create_task(daily_merge_cron())
    logger.info("Daily summary merge cron started")

    asyncio.create_task(cot_cleanup_loop())
    logger.info("COT cleanup loop started")
