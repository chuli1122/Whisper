from __future__ import annotations

import logging

from aiogram.types import Update
from fastapi import APIRouter, Request, Response

from .bot_instance import bots, dp
from .config import BOTS_CONFIG
from .handlers import router as handlers_router

logger = logging.getLogger(__name__)

router = APIRouter()

# Register handlers with the shared dispatcher
dp.include_router(handlers_router)


@router.post("/telegram/webhook/{bot_key}")
async def telegram_webhook(bot_key: str, request: Request) -> Response:
    """Receive Telegram update and feed it to the correct bot."""
    if bot_key not in bots or bot_key not in BOTS_CONFIG:
        return Response(status_code=404)

    try:
        data = await request.json()
        update = Update.model_validate(data)
        await dp.feed_update(
            bot=bots[bot_key],
            update=update,
            bot_key=bot_key,
            assistant_id=BOTS_CONFIG[bot_key]["assistant_id"],
        )
    except Exception as exc:
        logger.error("telegram_webhook error (%s): %s", bot_key, exc, exc_info=True)
    return Response()
