"""
QQ webhook endpoint — receives OneBot v11 events from NapCat via HTTP POST.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Request, Response

from .handlers import handle_qq_event

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/qq/webhook")
async def qq_webhook(request: Request) -> Response:
    """Receive OneBot v11 event from NapCat HTTP POST."""
    try:
        data = await request.json()
        # Process in background so NapCat gets a fast 200 response
        asyncio.create_task(handle_qq_event(data))
    except Exception as exc:
        logger.error("qq_webhook error: %s", exc, exc_info=True)
    return Response(status_code=200)
