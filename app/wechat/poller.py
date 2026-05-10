"""
WeChat iLink long-polling loop.

Replaces the webhook pattern used by QQ (NapCat pushes to us);
here we actively poll iLink for new messages.
"""
from __future__ import annotations

import asyncio
import logging

from app.database import SessionLocal
from app.models.models import Settings

from .ilink_api import ILinkAPI

logger = logging.getLogger(__name__)

_api: ILinkAPI | None = None
_poll_task: asyncio.Task | None = None


def _load_bot_token() -> str | None:
    """Read bot_token from Settings table."""
    db = SessionLocal()
    try:
        row = db.query(Settings).filter(Settings.key == "wechat_bot_token").first()
        return row.value if row else None
    finally:
        db.close()


def _load_cursor() -> str:
    """Read poll cursor from Settings table."""
    db = SessionLocal()
    try:
        row = db.query(Settings).filter(Settings.key == "wechat_poll_cursor").first()
        return row.value if row else ""
    finally:
        db.close()


def _save_cursor(cursor: str) -> None:
    """Persist poll cursor to Settings table."""
    db = SessionLocal()
    try:
        row = db.query(Settings).filter(Settings.key == "wechat_poll_cursor").first()
        if row:
            row.value = cursor
        else:
            db.add(Settings(key="wechat_poll_cursor", value=cursor))
        db.commit()
    finally:
        db.close()


async def start_polling() -> None:
    """Start the iLink long-polling loop. Called on app startup."""
    global _api, _poll_task

    bot_token = _load_bot_token()
    if not bot_token:
        logger.info("[wechat] No bot_token configured, skipping iLink polling")
        return

    cursor = _load_cursor()
    _api = ILinkAPI(bot_token, cursor=cursor)

    # Inject API into service module
    from .service import set_api
    set_api(_api)

    _poll_task = asyncio.create_task(_poll_loop())
    logger.info("[wechat] iLink polling started (cursor=%s)", cursor[:20] if cursor else "empty")


async def _poll_loop() -> None:
    """Infinite loop: long-poll for messages, dispatch each one."""
    from .handlers import dispatch_message

    consecutive_errors = 0
    while True:
        try:
            msgs = await _api.get_updates()
            consecutive_errors = 0  # reset on success

            for msg in msgs:
                asyncio.create_task(dispatch_message(msg, _api))

            # Persist cursor periodically (after each successful poll)
            if _api.cursor:
                _save_cursor(_api.cursor)

        except asyncio.CancelledError:
            logger.info("[wechat] Polling loop cancelled")
            break
        except Exception as e:
            consecutive_errors += 1
            wait = min(5 * consecutive_errors, 60)  # backoff up to 60s
            logger.error("[wechat] Polling error (attempt %d, wait %ds): %s",
                         consecutive_errors, wait, e)
            await asyncio.sleep(wait)


async def stop_polling() -> None:
    """Stop the polling loop and close the HTTP client."""
    global _poll_task, _api
    if _poll_task:
        _poll_task.cancel()
        try:
            await _poll_task
        except asyncio.CancelledError:
            pass
        _poll_task = None
    if _api:
        await _api.close()
        _api = None
    logger.info("[wechat] iLink polling stopped")
