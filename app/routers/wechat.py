"""
WeChat iLink management endpoints — QR code login flow + status.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import Settings

logger = logging.getLogger(__name__)
router = APIRouter()


class WeChatStatusResponse(BaseModel):
    connected: bool
    cursor: str | None = None


class QrCodeResponse(BaseModel):
    qr_url: str
    qrcode: str  # qrcode identifier for polling


class QrStatusResponse(BaseModel):
    status: str  # "waiting" | "confirmed" | "expired"
    connected: bool


class DisconnectResponse(BaseModel):
    ok: bool


def _get_setting(db: Session, key: str) -> str | None:
    row = db.query(Settings).filter(Settings.key == key).first()
    return row.value if row else None


def _set_setting(db: Session, key: str, value: str) -> None:
    row = db.query(Settings).filter(Settings.key == key).first()
    if row:
        row.value = value
    else:
        db.add(Settings(key=key, value=value))
    db.commit()


def _del_setting(db: Session, key: str) -> None:
    row = db.query(Settings).filter(Settings.key == key).first()
    if row:
        db.delete(row)
        db.commit()


@router.get("/wechat/status", response_model=WeChatStatusResponse)
def wechat_status(db: Session = Depends(get_db)):
    """Check if WeChat bot is connected."""
    token = _get_setting(db, "wechat_bot_token")
    return WeChatStatusResponse(
        connected=bool(token),
        cursor=_get_setting(db, "wechat_poll_cursor") if token else None,
    )


@router.post("/wechat/qr-login", response_model=QrCodeResponse)
async def wechat_qr_login(db: Session = Depends(get_db)):
    """Step 1: Get QR code for WeChat login."""
    import httpx

    async with httpx.AsyncClient(timeout=15) as client:
        from app.wechat.config import ILINK_BASE_URL
        resp = await client.get(
            f"{ILINK_BASE_URL}/ilink/bot/get_bot_qrcode",
            params={"bot_type": 3},
        )
        resp.raise_for_status()
        data = resp.json()

    qr_url = data.get("qrcode_img_content", "") or data.get("qr_url", "")
    qrcode = data.get("qrcode", "")

    if not qrcode:
        from fastapi import HTTPException
        raise HTTPException(status_code=502, detail="Failed to get QR code from iLink")

    return QrCodeResponse(qr_url=qr_url, qrcode=qrcode)


@router.get("/wechat/qr-status", response_model=QrStatusResponse)
async def wechat_qr_status(qrcode: str, db: Session = Depends(get_db)):
    """Step 2: Poll QR code scan status. Call repeatedly until confirmed or expired."""
    import httpx
    from app.wechat.config import ILINK_BASE_URL

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.get(
            f"{ILINK_BASE_URL}/ilink/bot/get_qrcode_status",
            params={"qrcode": qrcode},
        )
        resp.raise_for_status()
        data = resp.json()

    raw_status = data.get("status", "wait")
    logger.info("[wechat] QR status response: %s", {k: (v[:50] + "..." if isinstance(v, str) and len(v) > 50 else v) for k, v in data.items()})

    # iLink returns "wait" while waiting, and includes bot_token on success
    bot_token = data.get("bot_token", "")
    if bot_token:
        # Confirmed — save token and start polling
        _set_setting(db, "wechat_bot_token", bot_token)
        # Save baseurl if provided
        baseurl = data.get("baseurl", "")
        if baseurl:
            _set_setting(db, "wechat_ilink_baseurl", baseurl)
        logger.info("[wechat] QR login confirmed, bot_token saved")

        try:
            from app.wechat.poller import start_polling, _poll_task
            if not _poll_task:
                await start_polling()
        except Exception as e:
            logger.warning("[wechat] Failed to start polling after login: %s", e)

        return QrStatusResponse(status="confirmed", connected=True)

    # Map iLink status to our frontend expectations
    if raw_status in ("wait", "waiting"):
        return QrStatusResponse(status="waiting", connected=False)
    else:
        # Unknown status or expired
        return QrStatusResponse(status="expired", connected=False)


@router.post("/wechat/disconnect", response_model=DisconnectResponse)
async def wechat_disconnect(db: Session = Depends(get_db)):
    """Disconnect WeChat bot — stop polling and remove token."""
    # Stop polling
    try:
        from app.wechat.poller import stop_polling
        await stop_polling()
    except Exception as e:
        logger.warning("[wechat] Error stopping poller: %s", e)

    # Remove settings
    _del_setting(db, "wechat_bot_token")
    _del_setting(db, "wechat_poll_cursor")
    logger.info("[wechat] Disconnected and token removed")

    return DisconnectResponse(ok=True)
