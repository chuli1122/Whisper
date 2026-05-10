"""iOS 快捷指令集成 API — 指令队列 + 上报"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import IosCommand, IosReport

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/ios")

TZ_EAST8 = timezone(timedelta(hours=8))

# ── 鉴权：固定 token ──
IOS_TOKEN = os.getenv("IOS_TOKEN", "change-me-ios-token")
PUSHCUT_NOTIFICATION = os.getenv("PUSHCUT_NOTIFICATION", "ios_command")

# 指令默认过期时间（分钟）
DEFAULT_EXPIRE_MINUTES = 5

# Pushcut 合批：最近一次发送时间
_last_pushcut_time: float = 0
_pushcut_lock = asyncio.Lock()
PUSHCUT_BATCH_SECONDS = 2


def _require_ios_token(authorization: str = Header(None)):
    """验证 iOS 端固定 token"""
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing token")
    token = authorization.replace("Bearer ", "").strip()
    if token != IOS_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid token")


# ── Pushcut 通知 ──

def _get_pushcut_url(db: Session) -> str | None:
    """从 Settings 表读 Pushcut secret"""
    from app.models.models import Settings
    row = db.query(Settings).filter(Settings.key == "pushcut_secret").first()
    if not row or not row.value:
        return None
    return f"https://api.pushcut.io/{row.value}/notifications/{PUSHCUT_NOTIFICATION}"


async def _send_pushcut(url: str, title: str = "助手A", text: str = "有新指令"):
    """发送 Pushcut 通知，带 2 秒合批窗口"""
    import time
    global _last_pushcut_time
    async with _pushcut_lock:
        now = time.time()
        if now - _last_pushcut_time < PUSHCUT_BATCH_SECONDS:
            logger.info("[pushcut] 合批跳过，距上次 %.1fs", now - _last_pushcut_time)
            return
        _last_pushcut_time = now

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json={
                "title": title,
                "text": text,
            })
            logger.info("[pushcut] sent, status=%d", resp.status_code)
    except Exception:
        logger.exception("[pushcut] 发送失败")


# ── 请求模型 ──

class CommandCreate(BaseModel):
    action: str
    params: dict[str, Any] = {}

class AckItem(BaseModel):
    id: int
    result: str  # ok / failed / expired
    reason: str | None = None

class ReportCreate(BaseModel):
    report_type: str
    data: dict[str, Any] = {}


# ── 指令 API（服务端内部调用 + iPhone 调用）──

@router.post("/command")
async def create_command(body: CommandCreate, db: Session = Depends(get_db)):
    """创建指令（助手A工具内部调用，不需要 iOS token）"""
    now = datetime.now(TZ_EAST8)
    cmd = IosCommand(
        action=body.action,
        params=body.params,
        status="pending",
        expires_at=now + timedelta(minutes=DEFAULT_EXPIRE_MINUTES),
        created_at=now,
    )
    # 同类型的旧指令直接过期，只保留最新的
    old = db.query(IosCommand).filter(
        IosCommand.action == body.action,
        IosCommand.status == "pending",
    ).all()
    for o in old:
        o.status = "expired"

    db.add(cmd)
    db.commit()
    db.refresh(cmd)
    logger.info("[ios-cmd] created id=%d action=%s (expired %d old)", cmd.id, cmd.action, len(old))

    # 发邮件触发 iOS 自动化（不发 Pushcut，Pushcut 只用于通知类）
    try:
        from app.models.models import Settings
        row = db.query(Settings).filter(Settings.key == "ios_email_auth").first()
        if row and row.value:
            import smtplib
            from email.mime.text import MIMEText
            msg = MIMEText("trigger")
            msg["Subject"] = "acheng_cmd"
            msg["From"] = "ios-command@example.com"
            msg["To"] = "ios-command@example.com"
            s = smtplib.SMTP_SSL("smtp.163.com", 465, timeout=10)
            s.login("ios-command@example.com", row.value)
            s.sendmail("ios-command@example.com", ["ios-command@example.com"], msg.as_string())
            s.quit()
            logger.info("[ios-cmd] email trigger sent")
    except Exception:
        logger.warning("[ios-cmd] email trigger failed", exc_info=True)

    return {"id": cmd.id, "status": "pending"}


@router.get("/command/pending")
def get_pending(db: Session = Depends(get_db), _=Depends(_require_ios_token)):
    """iPhone 拉取待执行指令"""
    now = datetime.now(TZ_EAST8)

    # 自动过期
    expired = (
        db.query(IosCommand)
        .filter(IosCommand.status == "pending", IosCommand.expires_at < now)
        .all()
    )
    for cmd in expired:
        cmd.status = "expired"
    if expired:
        db.commit()
        logger.info("[ios-cmd] expired %d commands", len(expired))

    # 返回待执行，拉取后自动标记为 executed（避免重复执行）。
    # 截图命令例外：必须等 /screenshot 真收到图片后才算完成，避免旧自动化抢先拉取后吞掉命令。
    pending = (
        db.query(IosCommand)
        .filter(IosCommand.status == "pending")
        .order_by(IosCommand.created_at.asc())
        .all()
    )
    for cmd in pending:
        if cmd.action != "take_screenshot":
            cmd.status = "executed"
            cmd.executed_at = now
        else:
            cmd.executed_at = now
    if pending:
        db.commit()
    return {
        "commands": [
            {
                "id": c.id,
                "action": c.action,
                "display_title": c.params.get("title", "助手A"),
                "display_message": c.params.get("message", ""),
                "display_value": str(int(c.params.get("value", 0)) / 100) if c.action in ("set_brightness", "set_volume") and c.params.get("value", "").isdigit() else c.params.get("value", ""),
                "display_time": c.params.get("time", ""),
                "display_url": c.params.get("url", ""),
                "display_app": c.params.get("app", ""),
                "display_clipboard": c.params.get("clipboard", ""),
                "display_control": c.params.get("control", ""),
                "display_song": c.params.get("song", ""),
                "expires_at": c.expires_at.isoformat(),
            }
            for c in pending
        ]
    }


@router.get("/command/run")
def run_pending(db: Session = Depends(get_db), _=Depends(_require_ios_token)):
    """快捷指令专用：拉取一条待执行指令，返回纯文本，按行分隔。
    第1行: action
    第2行: title
    第3行: content
    第4行: time
    第5行: app
    没有指令时返回 'none'。"""
    from fastapi.responses import PlainTextResponse
    now = datetime.now(TZ_EAST8)

    # 自动过期
    for cmd in db.query(IosCommand).filter(
        IosCommand.status == "pending", IosCommand.expires_at < now,
    ).all():
        cmd.status = "expired"
    db.commit()

    cmd = (
        db.query(IosCommand)
        .filter(IosCommand.status == "pending")
        .order_by(IosCommand.created_at.asc())
        .first()
    )
    if not cmd:
        return PlainTextResponse("none")

    screenshot_deferred = cmd.action == "take_screenshot"
    if screenshot_deferred:
        cmd.executed_at = now
        db.commit()
    else:
        cmd.status = "executed"
        cmd.executed_at = now
        db.commit()

    SEP = "|||"
    fields = [
        cmd.action,
        cmd.params.get("title", ""),
        cmd.params.get("content", ""),
        cmd.params.get("time", ""),
        cmd.params.get("app", ""),
    ]
    logger.info(
        "[ios-cmd-run] action=%s title=%s deferred_until_upload=%s",
        cmd.action,
        fields[1][:30],
        screenshot_deferred,
    )
    return PlainTextResponse(SEP.join(fields))


@router.post("/command/ack")
def ack_commands(items: list[AckItem], db: Session = Depends(get_db), _=Depends(_require_ios_token)):
    """iPhone 批量确认执行结果"""
    now = datetime.now(TZ_EAST8)
    results = []
    for item in items:
        cmd = db.query(IosCommand).filter(IosCommand.id == item.id).first()
        if not cmd:
            results.append({"id": item.id, "error": "not found"})
            continue
        cmd.status = "executed" if item.result == "ok" else item.result
        cmd.executed_at = now
        cmd.result = {"result": item.result}
        if item.reason:
            cmd.result["reason"] = item.reason
        results.append({"id": item.id, "status": cmd.status})
    db.commit()
    logger.info("[ios-cmd] acked %d commands", len(items))
    return {"results": results}


# ── 上报 API（iPhone 调用）──

APP_EVENT_SECRET = os.getenv("IOS_APP_EVENT_SECRET", "change-me-app-event-secret")


@router.get("/app-event")
@router.post("/app-event")
def app_event(
    secret: str,
    app: str,
    event: str,
    db: Session = Depends(get_db),
):
    """iPhone 自动化上报 app 打开/关闭（不需要 Bearer token，URL 参数鉴权）
    同一个 app 5 分钟内重复 open 只记第一次。"""
    if secret != APP_EVENT_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret")
    if event not in ("open", "close"):
        raise HTTPException(status_code=400, detail="event must be open or close")
    now = datetime.now(TZ_EAST8)
    if event == "open":
        from sqlalchemy import cast
        from sqlalchemy.dialects.postgresql import JSONB as _JSONB
        recent = (
            db.query(IosReport)
            .filter(
                IosReport.report_type == "app_event",
                IosReport.created_at >= now - timedelta(minutes=5),
                IosReport.data["app"].astext == app,
                IosReport.data["event"].astext == "open",
            )
            .order_by(IosReport.created_at.desc())
            .first()
        )
        if recent:
            logger.info("[ios-app-event] dedup skip %s (last open %.0fs ago)", app, (now - recent.created_at).total_seconds())
            return {"status": "ok", "dedup": True}
    report = IosReport(
        report_type="app_event",
        data={"app": app, "event": event},
        created_at=now,
    )
    db.add(report)
    db.commit()
    logger.info("[ios-app-event] %s %s", event, app)
    return {"status": "ok"}


@router.get("/app-events")
def get_app_events(
    limit: int = 20,
    hours: int = 24,
    db: Session = Depends(get_db),
):
    """查最近 app 使用事件（proactive 注入 + 助手A工具调用）"""
    since = datetime.now(TZ_EAST8) - timedelta(hours=hours)
    rows = (
        db.query(IosReport)
        .filter(
            IosReport.report_type == "app_event",
            IosReport.created_at >= since,
        )
        .order_by(IosReport.created_at.desc())
        .limit(min(limit, 50))
        .all()
    )
    return {
        "events": [
            {
                "app": r.data.get("app", ""),
                "event": r.data.get("event", ""),
                "time": r.created_at.strftime("%H:%M"),
                "date": r.created_at.strftime("%Y-%m-%d"),
            }
            for r in reversed(rows)
        ]
    }


SCREENSHOT_DIR = "/srv/ai-companion/screenshots"


def _looks_like_image(data: bytes) -> bool:
    return (
        data.startswith(b"\x89PNG\r\n\x1a\n")
        or data.startswith(b"\xff\xd8\xff")
        or data.startswith(b"GIF87a")
        or data.startswith(b"GIF89a")
        or (data.startswith(b"RIFF") and data[8:12] == b"WEBP")
    )


def _decode_screenshot_payload(value: Any) -> bytes | None:
    """Accept raw image bytes or common Shortcuts base64 string shapes."""
    if value is None:
        return None
    if isinstance(value, bytes):
        if _looks_like_image(value):
            return value
        try:
            text = value.decode("utf-8").strip()
        except UnicodeDecodeError:
            return None
    elif isinstance(value, str):
        text = value.strip()
    else:
        return None

    if not text:
        return None

    if "," in text and text.lower().startswith("data:"):
        text = text.split(",", 1)[1]
    if text.lower().startswith("base64://"):
        text = text[len("base64://"):]
    text = re.sub(r"\s+", "", text)
    text += "=" * (-len(text) % 4)

    try:
        decoded = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        return None
    if not _looks_like_image(decoded):
        return None
    return decoded


async def _read_screenshot_upload(request: Request) -> tuple[bytes, str | None]:
    content_type = request.headers.get("content-type", "").lower()
    logger.info("[ios-screenshot] incoming: content-type=%s", content_type)

    if "multipart/form-data" in content_type or "application/x-www-form-urlencoded" in content_type:
        form = await request.form()
        secret = form.get("secret")
        for key in ("image", "screenshot", "file", "data", "base64", "image_base64"):
            body = _decode_screenshot_payload(form.get(key))
            if body:
                return body, str(secret) if secret else None
        for field in form.values():
            if hasattr(field, "read"):
                body = _decode_screenshot_payload(await field.read())
            else:
                body = _decode_screenshot_payload(field)
            if body:
                return body, str(secret) if secret else None
        raise HTTPException(status_code=400, detail="No valid image in form data")

    media_type = content_type.split(";", 1)[0].strip()
    if media_type == "application/json" or media_type.endswith("+json"):
        try:
            data = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}") from exc
        secret = data.get("secret") if isinstance(data, dict) else None
        candidates = []
        if isinstance(data, dict):
            candidates = [
                data.get("image"),
                data.get("screenshot"),
                data.get("file"),
                data.get("data"),
                data.get("base64"),
                data.get("image_base64"),
            ]
        elif isinstance(data, str):
            candidates = [data]
        for candidate in candidates:
            body = _decode_screenshot_payload(candidate)
            if body:
                return body, str(secret) if secret else None
        raise HTTPException(status_code=400, detail="No valid base64 image in JSON")

    raw = await request.body()
    body = _decode_screenshot_payload(raw)
    if body:
        return body, None

    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="Body is not an image or valid base64 image")
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Body is not an image or valid base64 image")

    secret = data.get("secret")
    for key in ("image", "screenshot", "file", "data", "base64", "image_base64"):
        body = _decode_screenshot_payload(data.get(key))
        if body:
            return body, str(secret) if secret else None
    raise HTTPException(status_code=400, detail="No valid base64 image in JSON body")


def _save_latest_screenshot(body: bytes) -> dict[str, Any]:
    os.makedirs(SCREENSHOT_DIR, exist_ok=True)
    path = os.path.join(SCREENSHOT_DIR, "latest.png")
    with open(path, "wb") as f:
        f.write(body)
    logger.info("[ios-screenshot] saved %d bytes", len(body))
    return {"status": "ok", "size": len(body)}


def _mark_latest_screenshot_command_done(db: Session) -> None:
    cmd = (
        db.query(IosCommand)
        .filter(IosCommand.action == "take_screenshot", IosCommand.status == "pending")
        .order_by(IosCommand.created_at.desc())
        .first()
    )
    if not cmd:
        return
    cmd.status = "executed"
    cmd.executed_at = datetime.now(TZ_EAST8)
    db.commit()
    logger.info("[ios-screenshot] marked command id=%d executed after upload", cmd.id)


@router.post("/screenshot")
async def upload_screenshot(request: Request, secret: str | None = None, db: Session = Depends(get_db)):
    """iPhone 快捷指令上传截图，兼容 raw/form/json/base64。"""
    body, body_secret = await _read_screenshot_upload(request)
    if (secret or body_secret or request.headers.get("x-ios-secret")) != APP_EVENT_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret")
    result = _save_latest_screenshot(body)
    _mark_latest_screenshot_command_done(db)
    return result


@router.post("/screenshot-b64")
async def upload_screenshot_b64(request: Request, secret: str | None = None, db: Session = Depends(get_db)):
    """兼容旧快捷指令配置；实际解析逻辑与 /screenshot 相同。"""
    body, body_secret = await _read_screenshot_upload(request)
    if (secret or body_secret or request.headers.get("x-ios-secret")) != APP_EVENT_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret")
    result = _save_latest_screenshot(body)
    _mark_latest_screenshot_command_done(db)
    return result


@router.get("/screenshot/latest")
def get_latest_screenshot():
    """获取最新截图"""
    path = os.path.join(SCREENSHOT_DIR, "latest.png")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="No screenshot available")
    from fastapi.responses import FileResponse
    return FileResponse(path, media_type="image/png")


@router.post("/report")
def create_report(body: ReportCreate, db: Session = Depends(get_db), _=Depends(_require_ios_token)):
    """iPhone 上报数据"""
    report = IosReport(
        report_type=body.report_type,
        data=body.data,
        created_at=datetime.now(TZ_EAST8),
    )
    db.add(report)
    db.commit()
    logger.info("[ios-report] type=%s", body.report_type)
    return {"status": "ok"}


@router.get("/report/latest")
def get_latest_reports(type: str, db: Session = Depends(get_db)):
    """查最新上报数据，支持多 type 逗号分隔"""
    types = [t.strip() for t in type.split(",") if t.strip()]
    result = {}
    for t in types:
        row = (
            db.query(IosReport)
            .filter(IosReport.report_type == t)
            .order_by(IosReport.created_at.desc())
            .first()
        )
        if row:
            result[t] = {
                "data": row.data,
                "created_at": row.created_at.isoformat(),
            }
    return result


@router.get("/report/history")
def get_report_history(type: str, limit: int = 20, db: Session = Depends(get_db)):
    """查历史上报数据"""
    rows = (
        db.query(IosReport)
        .filter(IosReport.report_type == type)
        .order_by(IosReport.created_at.desc())
        .limit(min(limit, 100))
        .all()
    )
    return {
        "items": [
            {"id": r.id, "data": r.data, "created_at": r.created_at.isoformat()}
            for r in rows
        ]
    }
