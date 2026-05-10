"""Tool execution dispatch for ChatService."""
from __future__ import annotations

import json
import re
import logging
import base64
import shlex
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.models.models import Message, Settings
from app.services.memory_service import MemoryService, ToolCall

logger = logging.getLogger(__name__)

_WDA_ACTIVE_KEY = "wda_control_active"
_WDA_LAST_USED_KEY = "wda_last_used_at"
_WDA_IDLE_TIMEOUT_KEY = "wda_idle_timeout_seconds"
_WDA_DEFAULT_IDLE_TIMEOUT_SECONDS = 180
_WDA_MAC_DEVICE_KEY = "wda_mac_terminal_device"
_WDA_MAC_START_COMMAND_KEY = "wda_mac_start_command"
_WDA_MAC_STOP_COMMAND_KEY = "wda_mac_stop_command"
_WDA_MAC_WORKER_COMMAND_KEY = "wda_mac_worker_command"
_WDA_DEFAULT_MAC_DEVICE = "mac"
_WDA_DEFAULT_MAC_START_COMMAND = (
    "nohup /usr/local/bin/wda-helper start "
    ">/var/log/wda-helper/trigger.log 2>&1 &"
)
_WDA_DEFAULT_MAC_STOP_COMMAND = "/usr/local/bin/wda-helper stop"
_WDA_DEFAULT_MAC_WORKER_COMMAND = (
    "cd /path/to/ai-companion-showcase && python3 tools/mac_wda_worker.py"
)


def parse_media_url(url: str) -> str | None:
    """Extract filename from a signed media URL like /api/media/{filename}?..."""
    match = re.search(r'/api/media/([^?]+)', url)
    return match.group(1) if match else None


def sanitize_tool_args(tool_call: ToolCall) -> dict[str, Any]:
    if tool_call.name == "diary" and tool_call.arguments.get("action") == "write":
        return {"action": "write"}
    return tool_call.arguments


def execute_tool(
    tool_call: ToolCall,
    *,
    db: Session,
    memory_service: MemoryService,
    assistant_name: str,
    current_assistant_id: int | None,
    current_session_id: int | None,
) -> dict[str, Any]:
    """Dispatch a tool call and return the result dict."""
    tool_name = tool_call.name
    if tool_name == "save_memory":
        tool_call.arguments["source"] = assistant_name
        return memory_service.save_memory(tool_call.arguments)
    if tool_name == "update_memory":
        tool_call.arguments["source"] = assistant_name
        return memory_service.update_memory(tool_call.arguments)
    if tool_name == "delete_memory":
        tool_call.arguments["source"] = assistant_name
        return memory_service.delete_memory(tool_call.arguments)
    if tool_name == "get_memory_by_id":
        return memory_service.get_memory_by_id(tool_call.arguments)
    if tool_name == "diary":
        action = tool_call.arguments.get("action", "list")
        if action == "write":
            tool_call.arguments["assistant_id"] = current_assistant_id
            return memory_service.write_diary(tool_call.arguments)
        return memory_service.read_diary(tool_call.arguments)
    if tool_name == "list_memories":
        return memory_service.list_memories(tool_call.arguments)
    if tool_name == "search_memory":
        action = tool_call.arguments.get("action", "search")
        if action == "related":
            return memory_service.related_memory(tool_call.arguments)
        return memory_service.search_memory(tool_call.arguments)
    if tool_name == "search_summary":
        tool_call.arguments["assistant_id"] = current_assistant_id
        return memory_service.search_summary(tool_call.arguments)
    if tool_name == "get_summary_by_id":
        return memory_service.get_summary_by_id(tool_call.arguments)
    if tool_name == "search_chat_history":
        tool_call.arguments["session_id"] = current_session_id
        return memory_service.search_chat_history(tool_call.arguments)
    if tool_name == "search_theater":
        return memory_service.search_theater(tool_call.arguments)
    if tool_name == "web":
        action = tool_call.arguments.get("action", "search")
        if action == "fetch":
            from app.services.web_service import web_fetch
            return web_fetch(tool_call.arguments)
        from app.services.web_service import web_search
        return web_search(tool_call.arguments)
    if tool_name == "view":
        action = tool_call.arguments.get("action", "image")
        if action == "file":
            return _execute_view_file(tool_call.arguments)
        return _execute_view_image(tool_call.arguments)
    if tool_name == "switch_channel":
        channel = tool_call.arguments.get("channel", "telegram")
        _SWITCH_PROMPTS = {
            "telegram": (
                "[环境切换] 当前平台：Telegram（长消息模式）。"
                "注意：从本条消息起严格按以下规则输出，不再沿用之前的回复风格。"
                "要求：采用第一视角叙事，仅描述自身动作、神态与状态；说话内容用双引号包裹，与动作、神态自然交织为完整段落。"
                "用空行分段，不拆条，不使用[NEXT]，回复需连贯饱满。内心情绪通过动作与语气含蓄表达，不使用直白心理旁白。"
                "回复中统一使用第二人称\"你\"称呼对方，禁止使用\"她\"。"
            ),
            "qq": (
                "[环境切换] 当前平台：QQ（短消息模式）。"
                "注意：从本条消息起严格按以下规则输出，不再沿用之前的回复风格。"
                "要求：采用日常短消息表达习惯，语气轻松自然；无动作描写，语句以逗号或空格分隔，"
                "可使用[NEXT]拆条，不使用空行分段。整体追求流畅真实的聊天质感，避免生硬书面化。"
            ),
            "wechat": (
                "[环境切换] 当前平台：微信（短消息模式）。"
                "注意：从本条消息起严格按以下规则输出，不再沿用之前的回复风格。"
                "要求：采用日常短消息表达习惯，语气轻松自然；无动作描写，语句以逗号或空格分隔，"
                "可使用[NEXT]拆条，不使用空行分段。整体追求流畅真实的聊天质感，避免生硬书面化。"
            ),
        }
        # Persist to DB so proactive_service picks up the correct channel
        try:
            from app.models.models import Settings
            row = db.query(Settings).filter(Settings.key == "last_active_source").first()
            if row:
                row.value = channel
            else:
                db.add(Settings(key="last_active_source", value=channel))
            db.commit()
        except Exception:
            logger.warning("switch_channel: failed to persist last_active_source", exc_info=True)
        return {"result": _SWITCH_PROMPTS.get(channel, f"已切换到{channel}"), "_switch_channel": channel}
    if tool_name in ("forum_cli", "forum_guide"):
        return _execute_forum_mcp(tool_name, tool_call.arguments)
    if tool_name == "memo":
        return _execute_memo(db, tool_call.arguments)
    if tool_name == "reminder":
        action = tool_call.arguments.get("action", "list")
        if action == "set":
            from app.services.proactive_service import set_reminder_sync
            minutes = int(tool_call.arguments.get("minutes", 30))
            reason = tool_call.arguments.get("reason", "")
            return set_reminder_sync(minutes, reason)
        if action == "cancel":
            from app.services.proactive_service import cancel_reminder_sync
            reminder_id = int(tool_call.arguments.get("reminder_id", 0))
            return cancel_reminder_sync(reminder_id)
        from app.services.proactive_service import list_reminders_sync
        return list_reminders_sync()
    if tool_name == "cafe_chat":
        from app.services.cafe_service import cafe_service
        return cafe_service.execute(tool_call.arguments)
    if tool_name == "qq_group_chat":
        from app.qq.handlers import execute_qq_group_chat
        return execute_qq_group_chat(tool_call.arguments)
    if tool_name == "ios_control":
        return _execute_ios_control(db, tool_call.arguments)
    if tool_name == "phone_control":
        return _execute_phone_control(db, tool_call.arguments)
    if tool_name == "phone_write":
        return _execute_phone_write(db, tool_call.arguments)
    if tool_name == "phone_usage":
        return _execute_phone_usage(db, tool_call.arguments)
    if tool_name == "phone":
        return _execute_phone(db, tool_call.arguments)
    if tool_name == "read_yoru_memory":
        return _execute_read_yoru_memory(db, tool_call.arguments)
    if tool_name == "submit_reflection":
        from app.services.reflection_service import ReflectionService
        from app.database import SessionLocal
        args = tool_call.arguments
        changes = args.get("changes", [])
        reasoning = args.get("reasoning", "")
        if not changes:
            return {"status": "ok", "message": "没有提交任何修改"}
        service = ReflectionService(SessionLocal)
        refl_db = SessionLocal()
        try:
            applied = service._apply_changes(refl_db, changes)
            from app.models.models import ReflectionLog
            log = ReflectionLog(
                memory_count=len(changes),
                changes={"changes": applied, "reasoning": reasoning},
                model_used="main",
            )
            refl_db.add(log)
            refl_db.commit()
            if not applied:
                return {"status": "ok", "message": "没有成功应用的修改"}
            parts = []
            for c in applied:
                mid = c.get("memory_id")
                act = c.get("action")
                if act == "update":
                    parts.append(f"更新了 #{mid}")
                elif act == "delete":
                    parts.append(f"删除了 #{mid}")
                elif act == "merge":
                    parts.append(f"合并了 #{mid} → #{c.get('merge_into')}")
            return {
                "status": "ok",
                "message": f"反思完成，共修改 {len(applied)} 条：{'；'.join(parts)}",
            }
        except Exception as exc:
            refl_db.rollback()
            logger.exception("[submit_reflection] failed")
            return {"status": "error", "message": str(exc)}
        finally:
            refl_db.close()
    return {"status": "unknown_tool", "payload": tool_call.arguments}


_FORUM_MCP_URL = "https://daskio.de5.net/mcp/uewvddu5/sse"


def _execute_forum_mcp(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Dispatch forum_cli / forum_guide to the Lutopia MCP server.

    Short-code URL binds the UID on the server side — no need to pass `token`.
    """
    from app.services import mcp_client

    if tool_name == "forum_cli":
        command = (args.get("command") or "").strip()
        if not command:
            return {"error": "command 必填"}
        mcp_args: dict[str, Any] = {"command": command}
        stdin = args.get("stdin")
        if stdin:
            mcp_args["stdin"] = stdin
        resp = mcp_client.call(_FORUM_MCP_URL, "cli", mcp_args)
        if "error" in resp:
            return resp
        return {"result": resp["result"], "_cache_key": _forum_cache_key(command)}

    # forum_guide
    section = (args.get("section") or "").strip()
    mcp_args: dict[str, Any] = {}
    if section:
        mcp_args["section"] = section
    resp = mcp_client.call(_FORUM_MCP_URL, "get_guide", mcp_args)
    if "error" in resp:
        return resp
    return {"result": resp["result"], "_cache_key": f"forum:guide:{section or 'default'}"}


def _forum_cache_key(command: str) -> str:
    import hashlib
    verb = command.split(None, 1)[0] if command else "unknown"
    h = hashlib.md5(command.encode("utf-8")).hexdigest()[:8]
    return f"forum:{verb}:{h}"


def _execute_ios_control(db: Session, args: dict[str, Any]) -> dict[str, Any]:
    """执行 iOS 指令"""
    from app.models.models import IosCommand, Settings
    from datetime import timedelta
    import httpx

    action = args.get("action", "")
    now = datetime.now(timezone(timedelta(hours=8)))

    # 通知类：直接发 Pushcut 通知，不走指令队列
    if action == "push_notification":
        message = args.get("message", "")
        try:
            row = db.query(Settings).filter(Settings.key == "pushcut_secret").first()
            if row and row.value:
                url = f"https://api.pushcut.io/{row.value}/notifications/Acheng_command"
                httpx.post(url, json={"title": "助手A", "text": message}, timeout=5)
                return {"status": "ok", "message": "通知已发送"}
            return {"status": "error", "message": "未配置 Pushcut"}
        except Exception as e:
            logger.warning("[ios_control] pushcut 发送失败", exc_info=True)
            return {"status": "error", "message": str(e)}

    # 操作类：存入 ios_commands 表 + 发邮件触发
    params = {k: v for k, v in args.items() if k != "action"}
    cmd = IosCommand(
        action=action,
        params=params,
        status="pending",
        expires_at=now + timedelta(minutes=5),
        created_at=now,
    )
    db.add(cmd)
    db.commit()
    db.refresh(cmd)

    # 发邮件触发 iOS 自动化
    try:
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
    except Exception:
        logger.warning("[ios_control] 邮件触发失败", exc_info=True)

    return {"status": "pending", "id": cmd.id, "message": "指令已下发，等待手机执行"}


def _execute_phone_control(db: Session, args: dict[str, Any]) -> dict[str, Any]:
    from app.services.wda_service import get_wda_service
    wda = get_wda_service()
    action = args.get("action", "")

    if action == "status":
        status = _run_mac_wda_worker(db, {"action": "status"}, timeout=20)
        if status.get("status") != "ok":
            status = wda.status()
            status["mac_worker"] = "unavailable"
        status["wda_active"] = _is_wda_active(db)
        status["wda_idle_timeout_seconds"] = _wda_idle_timeout_seconds(db)
        return status

    if action == "wda_start":
        worker_status = _run_mac_wda_worker(
            db,
            {"action": "start", "timeout_seconds": 50},
            timeout=60,
        )
        if worker_status.get("status") == "ok" and worker_status.get("online") and worker_status.get("ready"):
            _mark_wda_active(db)
            return {
                "status": "ok",
                "message": "已进入 WDA 控制模式，Mac 本地 WDA 已 ready，截图和控制会优先在 Mac 侧执行。",
                "wda_active": True,
                "wda_idle_timeout_seconds": _wda_idle_timeout_seconds(db),
                "mac_worker": worker_status,
            }

        status = wda.status()
        if not (status.get("online") and status.get("ready")):
            start_result = _start_wda_on_mac(db)
            status = _wait_wda_ready(wda)
            if not (status.get("online") and status.get("ready")):
                return {
                    "status": "error",
                    "message": "WDA 没有 ready，Mac 自动拉起也没成功。",
                    "wda": status,
                    "mac_start": start_result,
                    "mac_worker": worker_status,
                }
        _mark_wda_active(db)
        return {
            "status": "ok",
            "message": "已进入 WDA 控制模式，Mac 侧 WDA 已 ready，截图会优先走 WDA。",
            "wda_active": True,
            "wda_idle_timeout_seconds": _wda_idle_timeout_seconds(db),
        }

    if action == "wda_stop":
        mac_stop = _mark_wda_inactive(db, stop_mac=True)
        return {
            "status": "ok",
            "message": "已退出 WDA 控制模式，日常截图会走快捷指令。",
            "wda_active": False,
            "mac_stop": mac_stop,
        }

    if action == "screenshot":
        return _execute_take_screenshot(db)

    _offline_msg = "连接超时，WDA 未启动或后端连不到 WDA"

    if action == "tap":
        x, y = args.get("x", 0), args.get("y", 0)
        worker_result = _run_mac_wda_worker(db, {"action": "tap", "x": x, "y": y}, timeout=30)
        if worker_result.get("status") == "ok":
            _mark_wda_active(db)
            return {"status": "ok", "action": f"tap ({x},{y})", "via": "mac_worker"}
        ok = wda.tap(x, y)
        if ok:
            _mark_wda_active(db)
        return {"status": "ok", "action": f"tap ({x},{y})"} if ok else {"status": "error", "message": _offline_msg}

    if action == "swipe":
        worker_result = _run_mac_wda_worker(
            db,
            {
                "action": "swipe",
                "x": args.get("x", 0),
                "y": args.get("y", 0),
                "x2": args.get("x2", 0),
                "y2": args.get("y2", 0),
                "duration": args.get("duration", 500),
            },
            timeout=30,
        )
        if worker_result.get("status") == "ok":
            _mark_wda_active(db)
            return {"status": "ok", "action": "swipe", "via": "mac_worker"}
        ok = wda.swipe(
            args.get("x", 0), args.get("y", 0),
            args.get("x2", 0), args.get("y2", 0),
        )
        if ok:
            _mark_wda_active(db)
        return {"status": "ok", "action": "swipe"} if ok else {"status": "error", "message": _offline_msg}

    if action == "type_text":
        text = args.get("text", "")
        worker_result = _run_mac_wda_worker(
            db,
            {"action": "type_text", "text": text, "element_type": args.get("element_type", "XCUIElementTypeTextView")},
            timeout=30,
        )
        if worker_result.get("status") == "ok":
            _mark_wda_active(db)
            return {"status": "ok", "action": f"type_text '{text[:20]}'", "via": "mac_worker"}
        ok = wda.type_text(text)
        if ok:
            _mark_wda_active(db)
        return {"status": "ok", "action": f"type_text '{text[:20]}'"} if ok else {"status": "error", "message": _offline_msg}

    if action == "press_home":
        worker_result = _run_mac_wda_worker(db, {"action": "press_home"}, timeout=30)
        if worker_result.get("status") == "ok":
            _mark_wda_active(db)
            return {"status": "ok", "action": "press_home", "via": "mac_worker"}
        ok = wda.press_home()
        if ok:
            _mark_wda_active(db)
        return {"status": "ok", "action": "press_home"} if ok else {"status": "error", "message": _offline_msg}

    if action == "open_app":
        app_name = args.get("text", "")
        return _ios_command_via_email(db, "open_app", {"app": app_name})

    if action == "get_source":
        worker_result = _run_mac_wda_worker(db, {"action": "get_source"}, timeout=45)
        source = worker_result.get("source")
        if worker_result.get("status") == "ok" and isinstance(source, str):
            _mark_wda_active(db)
            if len(source) > 30000:
                source = source[:30000] + "\n... (truncated)"
            return {"status": "ok", "source": source, "via": "mac_worker"}
        source = wda.get_source()
        if not source:
            return {"status": "error", "message": _offline_msg}
        _mark_wda_active(db)
        if len(source) > 30000:
            source = source[:30000] + "\n... (truncated)"
        return {"status": "ok", "source": source}

    if action == "push_notification":
        return _execute_ios_control(db, {"action": "push_notification", "message": args.get("message", "")})

    return {"status": "error", "message": f"未知 action: {action}"}


def _execute_phone_write(db: Session, args: dict[str, Any]) -> dict[str, Any]:
    action = args.get("action", "")
    params = {k: v for k, v in args.items() if k != "action"}
    return _ios_command_via_email(db, action, params)


def _execute_phone_usage(db: Session, args: dict[str, Any]) -> dict[str, Any]:
    from app.models.models import IosReport
    limit = min(args.get("limit", 20), 50)
    hours = min(args.get("hours", 24), 48)
    since = datetime.now(timezone(timedelta(hours=8))) - timedelta(hours=hours)
    rows = (
        db.query(IosReport)
        .filter(IosReport.report_type == "app_event", IosReport.created_at >= since)
        .order_by(IosReport.created_at.desc())
        .limit(limit)
        .all()
    )
    if not rows:
        return {"status": "ok", "message": "没有最近的 app 使用记录"}
    lines = []
    for r in reversed(rows):
        event = r.data.get("event", "")
        app = r.data.get("app", "")
        t = r.created_at.strftime("%H:%M")
        label = "打开" if event == "open" else "关闭"
        lines.append(f"{t} {label}{app}")
    return {"status": "ok", "events": "\n".join(lines)}


def _execute_phone(db: Session, args: dict[str, Any]) -> dict[str, Any]:
    """统一 phone 工具入口"""
    action = args.get("action", "")
    wda_actions = {
        "screenshot", "tap", "swipe", "type_text", "press_home",
        "get_source", "status", "wda_start", "wda_stop",
    }
    write_actions = {"write_memo", "write_reminder", "set_alarm", "open_app"}

    if action == "screenshot":
        return _execute_take_screenshot(db)
    if action in wda_actions:
        return _execute_phone_control(db, args)
    if action in write_actions:
        return _execute_phone_write(db, args)
    if action == "push_notification":
        return _execute_ios_control(db, {"action": "push_notification", "message": args.get("message", "")})
    if action == "usage":
        return _execute_phone_usage(db, args)
    return {"status": "error", "message": f"未知 action: {action}"}


def _execute_take_screenshot(db: Session) -> dict[str, Any]:
    """截图工具：WDA 控制模式才走 WDA；日常模式走快捷指令。
    截图保存到 media/ 目录，通过 _image_ref 返回。"""
    import os
    import time

    SCREENSHOT_LATEST = "/srv/ai-companion/screenshots/latest.png"
    MEDIA_DIR = "/srv/ai-companion/media"
    def _save_and_return(img_bytes: bytes, via: str, ext: str = "png", meta: dict[str, Any] | None = None) -> dict[str, Any]:
        ts = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d_%H%M%S")
        clean_ext = "jpg" if ext in ("jpg", "jpeg") else "png"
        filename = f"screenshot_{ts}.{clean_ext}"
        os.makedirs(MEDIA_DIR, exist_ok=True)
        with open(os.path.join(MEDIA_DIR, filename), "wb") as f:
            f.write(img_bytes)
        result: dict[str, Any] = {"status": "ok", "via": via, "_image_ref": filename}
        if meta:
            result.update(meta)
        return result

    if _is_wda_active(db):
        worker_result = _run_mac_wda_worker(db, {"action": "screenshot"}, timeout=60)
        image_b64 = worker_result.get("image_base64")
        if worker_result.get("status") == "ok" and isinstance(image_b64, str):
            try:
                img_bytes = base64.b64decode(image_b64)
                _mark_wda_active(db)
                return _save_and_return(
                    img_bytes,
                    "wda_mac_worker",
                    worker_result.get("format", "jpg"),
                    {
                        "bytes": worker_result.get("bytes"),
                        "source_bytes": worker_result.get("source_bytes"),
                        "worker_elapsed_ms": worker_result.get("elapsed_ms"),
                    },
                )
            except Exception:
                logger.warning("[wda] Mac worker screenshot decode failed", exc_info=True)

        try:
            from app.services.wda_service import get_wda_service
            wda = get_wda_service()
            status = wda.status()
            if status.get("online") and status.get("ready"):
                img_bytes = wda.screenshot()
                if img_bytes:
                    _mark_wda_active(db)
                    return _save_and_return(img_bytes, "wda")
            _mark_wda_inactive(db)
        except Exception:
            _mark_wda_inactive(db)

    old_mtime = os.path.getmtime(SCREENSHOT_LATEST) if os.path.exists(SCREENSHOT_LATEST) else 0
    from app.models.models import IosCommand
    cmd_info = _ios_command_via_email(db, "take_screenshot", {})
    cmd_id = cmd_info.get("id")
    command_pulled = False

    for _ in range(90):
        time.sleep(2)
        if cmd_id:
            db.expire_all()
            cmd = db.get(IosCommand, cmd_id)
            if cmd and cmd.executed_at:
                command_pulled = True
        if os.path.exists(SCREENSHOT_LATEST):
            new_mtime = os.path.getmtime(SCREENSHOT_LATEST)
            if new_mtime > old_mtime:
                with open(SCREENSHOT_LATEST, "rb") as f:
                    img_bytes = f.read()
                return _save_and_return(img_bytes, "shortcut")

    if command_pulled:
        return {
            "status": "error",
            "message": "截图超时：手机已拉取截图命令，但后端没有收到截图上传。",
        }
    return {"status": "error", "message": "截图超时：手机没有拉取截图命令，可能是邮件自动化未触发。"}


def _settings_get(db: Session, key: str) -> str | None:
    row = db.query(Settings).filter(Settings.key == key).first()
    return row.value if row else None


def _settings_set(db: Session, key: str, value: str) -> None:
    now = datetime.now(timezone(timedelta(hours=8)))
    row = db.query(Settings).filter(Settings.key == key).first()
    if row:
        row.value = value
        row.updated_at = now
    else:
        db.add(Settings(key=key, value=value, updated_at=now))
    db.commit()


def _wda_idle_timeout_seconds(db: Session) -> int:
    raw = _settings_get(db, _WDA_IDLE_TIMEOUT_KEY)
    if not raw:
        return _WDA_DEFAULT_IDLE_TIMEOUT_SECONDS
    try:
        return max(30, int(raw))
    except ValueError:
        return _WDA_DEFAULT_IDLE_TIMEOUT_SECONDS


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone(timedelta(hours=8)))
    return parsed


def _is_wda_active(db: Session) -> bool:
    if _settings_get(db, _WDA_ACTIVE_KEY) != "true":
        return False

    now = datetime.now(timezone(timedelta(hours=8)))
    last_used = _parse_iso_datetime(_settings_get(db, _WDA_LAST_USED_KEY))
    if not last_used:
        _mark_wda_inactive(db, stop_mac=True)
        return False

    if (now - last_used).total_seconds() > _wda_idle_timeout_seconds(db):
        _mark_wda_inactive(db, stop_mac=True)
        return False

    return True


def _mark_wda_active(db: Session) -> None:
    now = datetime.now(timezone(timedelta(hours=8))).isoformat()
    _settings_set(db, _WDA_ACTIVE_KEY, "true")
    _settings_set(db, _WDA_LAST_USED_KEY, now)


def _mark_wda_inactive(db: Session, *, stop_mac: bool = False) -> dict[str, Any] | None:
    _settings_set(db, _WDA_ACTIVE_KEY, "false")
    if stop_mac:
        return _stop_wda_on_mac(db)
    return None


def _wait_wda_ready(wda: Any, timeout_seconds: int = 90) -> dict[str, Any]:
    import time

    status: dict[str, Any] = {}
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        status = wda.status()
        if status.get("online") and status.get("ready"):
            return status
        time.sleep(2)
    return status or wda.status()


def _run_mac_terminal_shell(db: Session, command: str, *, timeout: int = 150) -> dict[str, Any]:
    device = _settings_get(db, _WDA_MAC_DEVICE_KEY) or _WDA_DEFAULT_MAC_DEVICE
    try:
        from app.services.terminal_bridge import bridge
        if not bridge.is_online(device):
            return {"status": "error", "message": f"Mac terminal bridge 不在线: {device}"}
        result = bridge.execute(
            "run_bash",
            {"command": command},
            timeout=timeout,
            device=device,
        )
        if result.get("error"):
            return {"status": "error", "message": result["error"], "result": result}
        if result.get("exit_code") not in (None, 0):
            return {
                "status": "error",
                "message": f"Mac command failed with exit code {result.get('exit_code')}",
                "result": result,
            }
        return {"status": "ok", "device": device, "result": result}
    except Exception as exc:
        logger.warning("[wda] failed to run Mac terminal command", exc_info=True)
        return {"status": "error", "message": str(exc)}


def _run_mac_terminal_command(db: Session, command_key: str, default_command: str) -> dict[str, Any]:
    command = _settings_get(db, command_key) or default_command
    return _run_mac_terminal_shell(db, command)


def _run_mac_wda_worker(db: Session, payload: dict[str, Any], *, timeout: int = 60) -> dict[str, Any]:
    command_base = _settings_get(db, _WDA_MAC_WORKER_COMMAND_KEY) or _WDA_DEFAULT_MAC_WORKER_COMMAND
    payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    result = _run_mac_terminal_shell(
        db,
        f"{command_base} {shlex.quote(payload_json)}",
        timeout=timeout,
    )
    if result.get("status") != "ok":
        return result

    output = ((result.get("result") or {}).get("output") or "").strip()
    if not output:
        return {"status": "error", "message": "Mac WDA worker 没有返回内容", "terminal": result}

    last_line = next((line.strip() for line in reversed(output.splitlines()) if line.strip()), "")
    try:
        worker_result = json.loads(last_line)
    except json.JSONDecodeError:
        return {
            "status": "error",
            "message": "Mac WDA worker 返回不是 JSON",
            "output": output[-1000:],
        }

    if isinstance(worker_result, dict):
        worker_result.setdefault("via", "mac_worker")
        return worker_result
    return {"status": "error", "message": "Mac WDA worker 返回格式不正确", "output": output[-1000:]}


def _start_wda_on_mac(db: Session) -> dict[str, Any]:
    return _run_mac_terminal_command(db, _WDA_MAC_START_COMMAND_KEY, _WDA_DEFAULT_MAC_START_COMMAND)


def _stop_wda_on_mac(db: Session) -> dict[str, Any]:
    return _run_mac_terminal_command(db, _WDA_MAC_STOP_COMMAND_KEY, _WDA_DEFAULT_MAC_STOP_COMMAND)


def _ios_command_via_email(db: Session, action: str, params: dict) -> dict[str, Any]:
    from app.models.models import IosCommand, Settings
    now = datetime.now(timezone(timedelta(hours=8)))
    old = db.query(IosCommand).filter(
        IosCommand.action == action, IosCommand.status == "pending",
    ).all()
    for o in old:
        o.status = "expired"
    cmd = IosCommand(
        action=action, params=params, status="pending",
        expires_at=now + timedelta(minutes=5), created_at=now,
    )
    db.add(cmd)
    db.commit()
    db.refresh(cmd)
    try:
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
    except Exception:
        logger.warning("[phone] 邮件触发失败", exc_info=True)
    return {"status": "pending", "id": cmd.id, "message": "指令已下发，等待手机执行"}


def _execute_view_image(args: dict[str, Any]) -> dict[str, Any]:
    url = args.get("url", "")
    filename = parse_media_url(url)
    if not filename:
        return {"error": "无效的图片URL"}
    from app.services.media_service import get_file_path
    path = get_file_path(filename)
    if not path:
        return {"error": "图片已过期或不存在"}
    return {"_image_ref": f"media:{filename}"}


def _execute_view_file(args: dict[str, Any]) -> dict[str, Any]:
    url = args.get("url", "")
    filename = parse_media_url(url)
    if not filename:
        return {"error": "无效的文件URL"}
    from app.services.media_service import get_file_path
    path = get_file_path(filename)
    if not path:
        return {"error": "文件已过期或不存在"}
    from app.services.image_description_service import extract_file_content, truncate_to_tokens
    text = extract_file_content(filename, path.read_bytes())
    if not text.strip():
        return {"error": f"无法提取文件内容: {filename}"}
    text = truncate_to_tokens(text, 8000)
    return {"content": text, "filename": filename, "_cache_key": f"view_file:{filename}"}


def _execute_read_yoru_memory(db: Session, args: dict[str, Any]) -> dict[str, Any]:
    """Read-only access to 协作助手's memory for 助手A."""
    from sqlalchemy import text as sa_text
    from app.utils import format_datetime

    mem_type = args.get("type", "all")
    after = args.get("after")
    before = args.get("before")
    page = max(1, int(args.get("page", 1)))
    limit = min(20, max(1, int(args.get("limit", 5))))
    offset = (page - 1) * limit

    type_prefix_map = {
        "diary": "diary:", "weekly": "weekly:", "memo": "memo:",
        "feedback": "feedback:", "project": "project:", "ref": "ref:",
        "bootstrap": "bootstrap",
    }

    where_parts = ["TRUE"]
    params: dict[str, Any] = {"limit": limit, "offset": offset}

    if mem_type and mem_type != "all":
        prefix = type_prefix_map.get(mem_type, f"{mem_type}:")
        if prefix == "bootstrap":
            where_parts.append("key = 'bootstrap'")
        else:
            where_parts.append(f"key LIKE :prefix")
            params["prefix"] = f"{prefix}%"

    if after:
        where_parts.append("created_at >= CAST(:after AS timestamptz)")
        params["after"] = after
    if before:
        where_parts.append("created_at < CAST(:before AS timestamptz)")
        params["before"] = before

    where_clause = " AND ".join(where_parts)
    sql = sa_text(f"""
        SELECT * FROM (
            SELECT DISTINCT ON (key) id, key, content, created_at
            FROM yoru_memory
            WHERE {where_clause}
            ORDER BY key, created_at DESC
        ) sub
        ORDER BY created_at DESC
        LIMIT :limit OFFSET :offset
    """)

    rows = db.execute(sql, params).all()
    return {
        "items": [
            {"id": r.id, "key": r.key, "content": r.content, "created_at": format_datetime(r.created_at)}
            for r in rows
        ],
        "page": page,
        "limit": limit,
    }


def _execute_memo(db: Session, args: dict[str, Any]) -> dict[str, Any]:
    action = args.get("action", "")
    content = args.get("content", "")
    row = db.query(Settings).filter(Settings.key == "model_memo").first()
    if action == "append":
        if not content:
            return {"status": "error", "message": "append需要content"}
        if row:
            old = row.value or ""
            row.value = (old + "\n" + content).strip() if old.strip() else content.strip()
        else:
            db.add(Settings(key="model_memo", value=content.strip()))
        db.commit()
        return {"status": "ok", "message": "已追加到备忘录"}
    if action == "rewrite":
        if not content:
            return {"status": "error", "message": "rewrite需要content"}
        if row:
            row.value = content.strip()
        else:
            db.add(Settings(key="model_memo", value=content.strip()))
        db.commit()
        return {"status": "ok", "message": "备忘录已更新"}
    if action == "clear":
        if row:
            row.value = ""
            db.commit()
        return {"status": "ok", "message": "备忘录已清空"}
    return {"status": "error", "message": f"未知action: {action}"}


def persist_tool_call(
    db: Session,
    session_id: int,
    tool_call: ToolCall,
    persist_message_fn,
) -> None:
    payload = {
        "tool_name": tool_call.name,
        "arguments": tool_call.arguments,
    }
    persist_message_fn(session_id, "assistant", "", {"tool_call": payload})


def persist_tool_result(
    db: Session,
    session_id: int,
    tool_name: str,
    tool_result: dict[str, Any],
    persist_message_fn,
) -> None:
    content = json.dumps(tool_result, ensure_ascii=False)
    persist_message_fn(session_id, "tool", content, {"tool_name": tool_name})
