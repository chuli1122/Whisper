from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import logging
import os
import time
from datetime import datetime, timezone
from urllib.parse import parse_qsl

import jwt
from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import Settings
from app.utils import TZ_EAST8

logger = logging.getLogger(__name__)
router = APIRouter()
JWT_ALGORITHM = "HS256"
_TOKEN_EXPIRY_DAYS = 7


# ── Helpers ──────────────────────────────────────────────────────────────────

class VerifyPasswordRequest(BaseModel):
    password: str
    totp_code: str | None = None


class VerifyPasswordResponse(BaseModel):
    success: bool
    token: str
    whitelisted: bool = False


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


def _get_client_ip(request: Request) -> str:
    """Extract real client IP, respecting X-Forwarded-For behind nginx."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


def _get_whitelist(db: Session) -> list[str]:
    row = db.query(Settings).filter(Settings.key == "auth_whitelist_ips").first()
    if not row or not row.value:
        return []
    return [ip.strip() for ip in row.value.split(",") if ip.strip()]


def _is_whitelisted(ip: str, db: Session) -> bool:
    return ip in _get_whitelist(db)


def _get_totp_secret(db: Session) -> str | None:
    row = db.query(Settings).filter(Settings.key == "totp_secret").first()
    return row.value if row and row.value else None


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
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return token


# ── Auth endpoints ───────────────────────────────────────────────────────────

@router.post("/auth/verify", response_model=VerifyPasswordResponse)
def verify_password(
    payload: VerifyPasswordRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> VerifyPasswordResponse:
    expected_password = os.getenv("WHISPER_PASSWORD")
    if not expected_password:
        raise HTTPException(status_code=500, detail="Password is not configured")

    if payload.password != expected_password:
        raise HTTPException(status_code=401, detail="Invalid password")

    client_ip = _get_client_ip(request)
    whitelisted = _is_whitelisted(client_ip, db)

    if whitelisted:
        # Whitelisted IP: no expiry
        token = jwt.encode(
            {"iat": int(datetime.now(TZ_EAST8).timestamp())},
            _get_jwt_secret(),
            algorithm=JWT_ALGORITHM,
        )
    else:
        # Non-whitelisted: require TOTP if configured
        totp_secret = _get_totp_secret(db)
        if totp_secret:
            if not payload.totp_code:
                raise HTTPException(status_code=403, detail="TOTP code required")
            import pyotp
            totp = pyotp.TOTP(totp_secret)
            if not totp.verify(payload.totp_code, valid_window=1):
                raise HTTPException(status_code=401, detail="Invalid TOTP code")
        # 7-day expiry token
        token = jwt.encode(
            {
                "iat": int(datetime.now(TZ_EAST8).timestamp()),
                "exp": int(datetime.now(TZ_EAST8).timestamp()) + 86400 * _TOKEN_EXPIRY_DAYS,
            },
            _get_jwt_secret(),
            algorithm=JWT_ALGORITHM,
        )

    response.set_cookie("whisper_token", token, httponly=True, samesite="lax")
    return VerifyPasswordResponse(success=True, token=token, whitelisted=whitelisted)


@router.get("/auth/check-ip")
def check_ip(request: Request, db: Session = Depends(get_db)):
    """Check if current IP is whitelisted and if TOTP is configured."""
    client_ip = _get_client_ip(request)
    whitelisted = _is_whitelisted(client_ip, db)
    totp_configured = _get_totp_secret(db) is not None
    return {
        "ip": client_ip,
        "whitelisted": whitelisted,
        "totp_required": not whitelisted and totp_configured,
    }


# ── TOTP setup ───────────────────────────────────────────────────────────────

@router.post("/auth/totp-setup")
def totp_setup(
    _token: str = Depends(require_auth_token),
    db: Session = Depends(get_db),
):
    """Generate TOTP secret and QR code. Only works if TOTP not yet configured."""
    existing = _get_totp_secret(db)
    if existing:
        raise HTTPException(status_code=400, detail="TOTP already configured. Delete first to reconfigure.")

    import pyotp
    import qrcode

    secret = pyotp.random_base32()
    totp = pyotp.TOTP(secret)
    uri = totp.provisioning_uri(name="demo-user", issuer_name="AICompanion")

    # Generate QR code as base64 PNG
    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    qr_b64 = base64.b64encode(buf.getvalue()).decode()

    # Save secret to DB
    row = db.query(Settings).filter(Settings.key == "totp_secret").first()
    if row:
        row.value = secret
    else:
        db.add(Settings(key="totp_secret", value=secret))
    db.commit()

    return {"secret": secret, "qr_code": f"data:image/png;base64,{qr_b64}"}


@router.delete("/auth/totp")
def totp_delete(
    _token: str = Depends(require_auth_token),
    db: Session = Depends(get_db),
):
    """Remove TOTP configuration."""
    row = db.query(Settings).filter(Settings.key == "totp_secret").first()
    if row:
        db.delete(row)
        db.commit()
    return {"success": True}


@router.get("/auth/totp-status")
def totp_status(
    _token: str = Depends(require_auth_token),
    db: Session = Depends(get_db),
):
    """Check if TOTP is configured."""
    return {"configured": _get_totp_secret(db) is not None}


# ── IP Whitelist management ──────────────────────────────────────────────────

@router.get("/auth/whitelist")
def get_whitelist(
    request: Request,
    _token: str = Depends(require_auth_token),
    db: Session = Depends(get_db),
):
    ips = _get_whitelist(db)
    client_ip = _get_client_ip(request)
    return {"ips": ips, "current_ip": client_ip}


class WhitelistAddRequest(BaseModel):
    ip: str


@router.post("/auth/whitelist")
def add_whitelist(
    payload: WhitelistAddRequest,
    _token: str = Depends(require_auth_token),
    db: Session = Depends(get_db),
):
    ips = _get_whitelist(db)
    new_ip = payload.ip.strip()
    if not new_ip:
        raise HTTPException(status_code=400, detail="IP cannot be empty")
    if new_ip in ips:
        return {"ips": ips, "message": "Already whitelisted"}
    ips.append(new_ip)
    _save_whitelist(ips, db)
    return {"ips": ips}


class WhitelistDeleteRequest(BaseModel):
    ip: str


@router.delete("/auth/whitelist")
def remove_whitelist(
    payload: WhitelistDeleteRequest,
    _token: str = Depends(require_auth_token),
    db: Session = Depends(get_db),
):
    ips = _get_whitelist(db)
    new_ip = payload.ip.strip()
    if new_ip in ips:
        ips.remove(new_ip)
        _save_whitelist(ips, db)
    return {"ips": ips}


def _save_whitelist(ips: list[str], db: Session) -> None:
    value = ",".join(ips)
    row = db.query(Settings).filter(Settings.key == "auth_whitelist_ips").first()
    if row:
        row.value = value
    else:
        db.add(Settings(key="auth_whitelist_ips", value=value))
    db.commit()


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


# ── Device tokens ─────────────────────────────────────────────────────────

import secrets

def _get_device_tokens(db: Session) -> list[dict]:
    row = db.query(Settings).filter(Settings.key == "device_tokens").first()
    if not row or not row.value:
        return []
    try:
        return json.loads(row.value)
    except (json.JSONDecodeError, TypeError):
        return []

def _save_device_tokens(tokens: list[dict], db: Session) -> None:
    value = json.dumps(tokens, ensure_ascii=False)
    row = db.query(Settings).filter(Settings.key == "device_tokens").first()
    if row:
        row.value = value
    else:
        db.add(Settings(key="device_tokens", value=value))
    db.commit()

def verify_device_token(token: str, db: Session) -> bool:
    devices = _get_device_tokens(db)
    for d in devices:
        if d.get("token") == token:
            d["last_used"] = datetime.now(TZ_EAST8).strftime("%Y-%m-%d %H:%M")
            _save_device_tokens(devices, db)
            return True
    return False

@router.get("/auth/devices")
def list_devices(
    _token: str = Depends(require_auth_token),
    db: Session = Depends(get_db),
):
    devices = _get_device_tokens(db)
    return {"devices": [{"name": d["name"], "token": d["token"], "created_at": d.get("created_at"), "last_used": d.get("last_used")} for d in devices]}

class DeviceCreateRequest(BaseModel):
    name: str

@router.post("/auth/devices")
def create_device(
    payload: DeviceCreateRequest,
    _token: str = Depends(require_auth_token),
    db: Session = Depends(get_db),
):
    devices = _get_device_tokens(db)
    new_token = secrets.token_urlsafe(32)
    devices.append({
        "name": payload.name.strip(),
        "token": new_token,
        "created_at": datetime.now(TZ_EAST8).strftime("%Y-%m-%d %H:%M"),
        "last_used": None,
    })
    _save_device_tokens(devices, db)
    return {"token": new_token, "name": payload.name.strip()}

class DeviceDeleteRequest(BaseModel):
    token: str

@router.delete("/auth/devices")
def delete_device(
    payload: DeviceDeleteRequest,
    _token: str = Depends(require_auth_token),
    db: Session = Depends(get_db),
):
    devices = _get_device_tokens(db)
    devices = [d for d in devices if d.get("token") != payload.token]
    _save_device_tokens(devices, db)
    return {"success": True}
