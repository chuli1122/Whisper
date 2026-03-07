from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime, timezone
from urllib.parse import parse_qsl

import jwt
from fastapi import APIRouter, Cookie, Header, HTTPException, Response
from pydantic import BaseModel

from app.utils import TZ_EAST8

logger = logging.getLogger(__name__)
router = APIRouter()
JWT_ALGORITHM = "HS256"


class VerifyPasswordRequest(BaseModel):
    password: str


class VerifyPasswordResponse(BaseModel):
    success: bool
    token: str


def _get_jwt_secret() -> str:
    secret = os.getenv("WHISPER_SECRET") or os.getenv("WHISPER_PASSWORD")
    if not secret:
        logger.warning("WHISPER_SECRET and WHISPER_PASSWORD are not configured")
        raise HTTPException(status_code=500, detail="Auth secret is not configured")
    return secret


def _extract_token(
    authorization: str | None,
    x_auth_token: str | None,
    whisper_token: str | None,
) -> str | None:
    if authorization:
        parts = authorization.strip().split(" ", 1)
        if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1].strip():
            return parts[1].strip()
    if x_auth_token and x_auth_token.strip():
        return x_auth_token.strip()
    if whisper_token and whisper_token.strip():
        return whisper_token.strip()
    return None


def require_auth_token(
    authorization: str | None = Header(default=None),
    x_auth_token: str | None = Header(default=None),
    whisper_token: str | None = Cookie(default=None),
) -> str:
    token = _extract_token(authorization, x_auth_token, whisper_token)
    if not token:
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        jwt.decode(token, _get_jwt_secret(), algorithms=[JWT_ALGORITHM])
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return token


@router.post("/auth/verify", response_model=VerifyPasswordResponse)
def verify_password(payload: VerifyPasswordRequest, response: Response) -> VerifyPasswordResponse:
    expected_password = os.getenv("WHISPER_PASSWORD")
    if not expected_password:
        logger.warning("WHISPER_PASSWORD is not configured")
        raise HTTPException(status_code=500, detail="Password is not configured")

    if payload.password != expected_password:
        raise HTTPException(status_code=401, detail="Invalid password")

    token = jwt.encode(
        {"iat": int(datetime.now(TZ_EAST8).timestamp())},
        _get_jwt_secret(),
        algorithm=JWT_ALGORITHM,
    )
    response.set_cookie("whisper_token", token, httponly=True, samesite="lax")
    return VerifyPasswordResponse(success=True, token=token)


# ── Telegram Mini App auth ──────────────────────────────────────────────────


class TelegramAuthRequest(BaseModel):
    init_data: str


def _verify_telegram_init_data(init_data: str) -> dict | None:
    """Verify Telegram Mini App initData HMAC-SHA256 signature."""
    params = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = params.pop("hash", None)
    if not received_hash:
        return None

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))

    for var in ("TELEGRAM_BOT_TOKEN_AHUAI", "TELEGRAM_BOT_TOKEN_ACHENG"):
        bot_token = os.getenv(var, "")
        if not bot_token:
            continue
        secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
        if hmac.compare_digest(calc, received_hash):
            try:
                auth_date = int(params.get("auth_date", "0"))
            except ValueError:
                return None
            if time.time() - auth_date > 86400:
                return None
            return params

    return None


@router.post("/auth/telegram")
def verify_telegram(payload: TelegramAuthRequest, response: Response):
    verified = _verify_telegram_init_data(payload.init_data)
    if verified is None:
        raise HTTPException(status_code=401, detail="Invalid Telegram credentials")

    # Check user.id against TELEGRAM_CHAT_ID
    try:
        user_info = json.loads(verified.get("user", "{}"))
    except (json.JSONDecodeError, TypeError):
        user_info = {}

    allowed = os.getenv("TELEGRAM_CHAT_ID", "0")
    if allowed and allowed != "0":
        if str(user_info.get("id", "")) != allowed:
            logger.warning("Telegram user %s not authorized", user_info.get("id"))
            raise HTTPException(status_code=403, detail="User not authorized")

    token = jwt.encode(
        {"iat": int(datetime.now(TZ_EAST8).timestamp())},
        _get_jwt_secret(),
        algorithm=JWT_ALGORITHM,
    )
    response.set_cookie("whisper_token", token, httponly=True, samesite="lax")
    return {"success": True, "token": token}
