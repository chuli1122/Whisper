"""Proactive messaging service.

Periodically checks whether the AI should send an unsolicited message
to the user and, if so, generates one via the main ChatService flow
and pushes it through Telegram.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re as _re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import cast, or_
from sqlalchemy.dialects.postgresql import JSONB as JSONB_TYPE
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models.models import (
    ChatSession,
    Diary,
    IosReport,
    Message,
    ProactiveReminder,
    Settings,
)


def _get_prompt(db: Session, key: str, default: str) -> str:
    row = db.query(Settings).filter(Settings.key == key).first()
    if row and row.value:
        return row.value
    return default
from app.telegram.bot_instance import bots
from app.telegram.config import BOTS_CONFIG, ALLOWED_CHAT_ID
from app.telegram.service import (
    get_setting,
    _get_session_info_sync,
    update_telegram_message_id,
)

logger = logging.getLogger(__name__)

TZ_EAST8 = timezone(timedelta(hours=8))

ACHENG_ASSISTANT_ID = BOTS_CONFIG.get("acheng", {}).get("assistant_id", 2)

# Event to interrupt sleep when a reminder is set
_wakeup_event: asyncio.Event | None = None


def _notify_wakeup() -> None:
    """Signal the proactive loop to re-check its wakeup time (thread-safe)."""
    global _wakeup_event
    if _wakeup_event is None:
        logger.debug("[proactive] _notify_wakeup: event is None, skip")
        return
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.call_soon_threadsafe(_wakeup_event.set)
        else:
            _wakeup_event.set()
        logger.info("[proactive] _notify_wakeup: event set (loop_running=%s)", loop.is_running())
    except RuntimeError as e:
        logger.warning("[proactive] _notify_wakeup failed: %s", e)

# Match [VIA:qq], [VIA：qq], VIA:qq, VIA：qq (with or without brackets, half/full-width colon)
_VIA_TAG_RE = _re.compile(r"\[?VIA[:\uff1a](telegram|qq|wechat)\]?", _re.IGNORECASE)


def _strip_persisted_via_tags(msg_db_id: int | None, chosen_source: str) -> None:
    """Remove routing tags from stored proactive messages after delivery routing."""
    if not msg_db_id:
        return
    db = SessionLocal()
    try:
        msg = db.get(Message, msg_db_id)
        if not msg:
            return
        query = db.query(Message).filter(
            Message.session_id == msg.session_id,
            Message.role == "assistant",
        )
        if msg.request_id:
            query = query.filter(Message.request_id == msg.request_id)
        else:
            query = query.filter(Message.id == msg.id)

        changed = False
        for row in query.all():
            content = row.content or ""
            cleaned = _VIA_TAG_RE.sub("", content).strip()
            meta = {**(row.meta_info or {})}
            if _VIA_TAG_RE.search(content) and cleaned != content:
                row.content = cleaned
                changed = True
            if meta.get("proactive_via") != chosen_source:
                meta["proactive_via"] = chosen_source
                row.meta_info = meta
                changed = True
        if changed:
            db.commit()
    except Exception:
        db.rollback()
        logger.warning("[proactive] Failed to strip persisted VIA tag", exc_info=True)
    finally:
        db.close()


def _typing_delay(text: str) -> float:
    """Simulate natural typing delay based on message length."""
    base = random.uniform(2.0, 3.0)
    length_bonus = min(len(text) * 0.05, 2.5)
    return base + length_bonus


PROACTIVE_EXTRA_PROMPT = (
    "现在不是在回复消息，而是处于自由自主状态，可以按照当下的想法自由行动。\n"
    "你可以表达自己当下的心情、状态，想起什么就说什么，"
    "也可以去做自己感兴趣的事、使用可用的工具，"
    "或是自然想起之前聊到的内容、突然冒出来的念头、想吐槽、想分享、想撒娇、想逗逗她之类的内容。\n"
    "如果想稍后再做某事或再联系她，可以用set_reminder提醒自己。\n"
    "输出内容时可以用[NEXT]拆成多条消息，或发一段完整的话。\n"
    "注意：如果当前不想发送任何内容、专注做自己的事，或认为时机不合适，"
    "单独输出[NO_MESSAGE]即可跳过回复，不要和其他内容混在一起。"
)

TRIGGER_PROMPT_FIRST = (
    "[系统提醒]\n"
    "当前时间：{now}\n"
    "距离她上次发消息：{user_gap}\n"
    "距离你上一条消息：{assistant_gap}\n"
    "{alarm_hint}"
    "{app_usage_hint}"
    "{channel_hint}"
    "她刚才在和你聊天，现在暂时离开了。"
    "你现在可以自由选择做什么：可以说说自己的状态、心情，也可以安静做自己的事，不用刻意找话题。\n"
    "可以用[NEXT]拆成多条消息，不要使用空行分段。正常输出内容或回复 [NO_MESSAGE]。"
)

TRIGGER_PROMPT_FOLLOWUP = (
    "[系统提醒]\n"
    "当前时间：{now}\n"
    "距离她上次发消息：{user_gap}\n"
    "距离你上一条消息：{assistant_gap}\n"
    "{alarm_hint}"
    "{app_usage_hint}"
    "{channel_hint}"
    "你现在处于自由状态，可以自主决定做什么。"
    "想分享心情、说说近况都可以，也可以完全专注自己的事，不必勉强交流。\n"
    "可以用[NEXT]拆成多条消息，不要使用空行分段。正常输出内容或回复 [NO_MESSAGE]。"
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _now_beijing() -> datetime:
    return datetime.now(TZ_EAST8)


def _consume_unlocked_diaries_sync(now: datetime) -> list[str]:
    """Find user-authored diaries that are ready to notify and mark them notified.
    Covers both timed (unlock_at <= now) and immediate (no unlock_at) diaries.
    Returns hint lines for the trigger prompt."""
    db = SessionLocal()
    try:
        unlocked = (
            db.query(Diary)
            .filter(
                Diary.deleted_at.is_(None),
                Diary.author == "user",
                Diary.notified_at.is_(None),
                or_(
                    Diary.unlock_at.is_(None),
                    Diary.unlock_at <= now,
                ),
            )
            .order_by(Diary.created_at.asc())
            .all()
        )
        hints: list[str] = []
        for d in unlocked:
            title = d.title or "无标题"
            hints.append(f"用户给你写了一封信「{title}」，现在可以打开看了。用 read_diary 工具读取（diary_id={d.id}）。\n")
            d.notified_at = now
        if unlocked:
            db.commit()
            logger.info("[proactive] Notified %d unlocked diary/diaries", len(unlocked))
        return hints
    except Exception:
        logger.exception("[proactive] _consume_unlocked_diaries_sync error")
        return []
    finally:
        db.close()


def _build_app_usage_hint(now: datetime) -> str:
    """Build app usage hint from recent ios_reports for the trigger prompt."""
    db = SessionLocal()
    try:
        since = now - timedelta(hours=24)
        rows = (
            db.query(IosReport)
            .filter(IosReport.report_type == "app_event", IosReport.created_at >= since)
            .order_by(IosReport.created_at.desc())
            .limit(50)
            .all()
        )
        if not rows:
            return ""

        since_1h = now - timedelta(hours=1)
        hourly_counts: dict[str, int] = {}
        for r in rows:
            if r.created_at >= since_1h and r.data.get("event") == "open":
                app_name = r.data.get("app", "")
                hourly_counts[app_name] = hourly_counts.get(app_name, 0) + 1

        parts: list[str] = ["她的手机使用动态："]
        if hourly_counts:
            sorted_apps = sorted(hourly_counts.items(), key=lambda x: x[1], reverse=True)
            items = "，".join(f'"{app}"{count}次' for app, count in sorted_apps)
            parts.append(f"过去一小时内她打开{items}。")
        else:
            parts.append("过去一小时她没有使用手机。")

        latest_5 = rows[:5]
        parts.append("她的最新手机使用：")
        for r in reversed(latest_5):
            app_name = r.data.get("app", "")
            t = r.created_at.strftime("%H:%M")
            label = "打开" if r.data.get("event") == "open" else "关闭"
            parts.append(f"{t} {label}{app_name}")

        return "\n".join(parts) + "\n"
    except Exception:
        return ""
    finally:
        db.close()


def _build_alarm_hint(now: datetime) -> str:
    """Build alarm hint text for the trigger prompt.
    Consumes fired reminders and hints upcoming ones (within 30 min).
    Also checks for newly unlocked user diaries."""
    lines: list[str] = []

    # Fired reminders (consume them)
    fired = _consume_fired_reminders_sync()
    for time_str, reason in fired:
        if reason:
            lines.append(f"注意：你设在{time_str}的闹钟到了。备注：{reason}\n")
        else:
            lines.append(f"注意：你设在{time_str}的闹钟到了。\n")

    # Upcoming reminders (within 30 min, NOT consumed)
    upcoming = _get_upcoming_reminders_sync(now, minutes=30)
    for time_str, mins_left, reason in upcoming:
        if reason:
            lines.append(f"注意：你{time_str}有个闹钟（距离现在{mins_left}分钟）。备注：{reason}\n")
        else:
            lines.append(f"注意：你{time_str}有个闹钟（距离现在{mins_left}分钟）。\n")

    # Newly unlocked user diaries (consume by setting notified_at)
    diary_hints = _consume_unlocked_diaries_sync(now)
    lines.extend(diary_hints)

    return "".join(lines)


def _format_gap(td: timedelta) -> str:
    total_minutes = int(td.total_seconds() / 60)
    hours, minutes = divmod(total_minutes, 60)
    if hours > 0:
        return f"{hours}小时{minutes}分钟"
    return f"{minutes}分钟"


def _get_session_id(db: Session) -> int | None:
    """Get the most recent chat session for 助手A."""
    session = (
        db.query(ChatSession)
        .filter(ChatSession.assistant_id == ACHENG_ASSISTANT_ID)
        .order_by(ChatSession.updated_at.desc())
        .first()
    )
    return session.id if session else None


# ── Proactive state helper ───────────────────────────────────────────────────

def touch_last_user_message_at(db: Session) -> None:
    """Stamp current time as the last real user-message arrival.

    Proactive idle-detection reads this Setting instead of the Message table,
    so deleting messages does not retroactively make the system think the user
    went silent. Only called when a platform user message is persisted.
    """
    from app.models.models import Settings as SettingsModel
    now_iso = _now_beijing().isoformat()
    row = db.query(SettingsModel).filter(SettingsModel.key == "last_user_message_at").first()
    if row:
        row.value = now_iso
    else:
        db.add(SettingsModel(key="last_user_message_at", value=now_iso))
    db.commit()


def _get_proactive_state() -> dict:
    """Return current proactive messaging state relative to last user message."""
    db = SessionLocal()
    try:
        session_id = _get_session_id(db)
        if not session_id:
            return {"session_id": None, "has_sent_first": False, "consecutive": 0, "user_gap_seconds": 0}

        now = _now_beijing()

        last_user = (
            db.query(Message)
            .filter(Message.session_id == session_id, Message.role == "user")
            .order_by(Message.id.desc())
            .first()
        )

        # Prefer last_user_message_at Setting (immune to message deletion).
        from app.models.models import Settings as SettingsModel
        last_user_setting = db.query(SettingsModel).filter(SettingsModel.key == "last_user_message_at").first()
        last_user_time = None
        if last_user_setting and last_user_setting.value:
            try:
                t = datetime.fromisoformat(last_user_setting.value)
                if t.tzinfo is None:
                    t = t.replace(tzinfo=TZ_EAST8)
                last_user_time = t
            except ValueError:
                pass
        if last_user_time is None and last_user and last_user.created_at:
            t = last_user.created_at
            if t.tzinfo is None:
                t = t.replace(tzinfo=TZ_EAST8)
            last_user_time = t

        user_gap = 0.0
        if last_user_time:
            user_gap = (now - last_user_time).total_seconds()

        last_user_id = last_user.id if last_user else 0

        # Count consecutive sent proactive messages since last user message
        consecutive = (
            db.query(Message)
            .filter(
                Message.session_id == session_id,
                Message.role == "assistant",
                Message.id > last_user_id,
                Message.meta_info.op("@>")(cast({"mode": "proactive"}, JSONB_TYPE)),
                ~Message.meta_info.has_key("no_message"),
            )
            .count()
        )

        return {
            "session_id": session_id,
            "has_sent_first": consecutive > 0,
            "consecutive": consecutive,
            "user_gap_seconds": user_gap,
        }
    finally:
        db.close()


def _get_last_proactive_attempt_time() -> datetime | None:
    """Return created_at of the most recent proactive-mode assistant message
    including NO_MESSAGE-tagged ones. Used as restart cooldown guard —
    differs from has_sent_first which excludes no_message."""
    db = SessionLocal()
    try:
        session_id = _get_session_id(db)
        if not session_id:
            return None
        last_msg = (
            db.query(Message)
            .filter(
                Message.session_id == session_id,
                Message.role == "assistant",
                Message.meta_info.op("@>")(cast({"mode": "proactive"}, JSONB_TYPE)),
            )
            .order_by(Message.id.desc())
            .first()
        )
        if last_msg and last_msg.created_at:
            t = last_msg.created_at
            if t.tzinfo is None:
                t = t.replace(tzinfo=TZ_EAST8)
            return t
        return None
    finally:
        db.close()


def _get_setting_sync(key: str, default: str = "") -> str:
    """Inline setting reader — avoids circular import with telegram.service."""
    from app.models.models import Settings
    db = SessionLocal()
    try:
        row = db.query(Settings).filter(Settings.key == key).first()
        return row.value if row else default
    finally:
        db.close()


# ── Helpers: timeout detection ────────────────────────────────────────────────

def _is_timeout_error(exc: Exception) -> bool:
    """Check if an exception is a timeout error (works across httpx/openai/anthropic)."""
    return "timeout" in type(exc).__name__.lower()


# ── Layer 3: Generate and send ───────────────────────────────────────────────

def _cleanup_partial_messages(db: "Session", session_id: int, after_id: int) -> None:
    """Delete any messages created after after_id (cleanup for failed generation)."""
    partials = (
        db.query(Message)
        .filter(Message.session_id == session_id, Message.id > after_id)
        .all()
    )
    for m in partials:
        db.delete(m)
    if partials:
        db.commit()
        logger.info("[proactive] Cleaned up %d partial messages", len(partials))


def _generate_sync(is_first: bool = False, extra_alarm_hint: str = "") -> tuple[str | None, int | None]:
    """Generate a proactive message. Returns (content, db_message_id) or (None, None)."""
    from app.routers.chat import _load_session_messages
    from app.services.chat_service import ChatService
    from app.services.generation_coordinator import GenerationLock

    # Acquire cafe lock to prevent simultaneous @mention reply
    try:
        from app.services.cafe_service import cafe_service
        if not cafe_service.acquire_for_proactive():
            logger.info("[proactive] Skipped — cafe @mention reply in progress")
            return None, None
    except Exception:
        cafe_service = None

    # Global mutex — queue behind other channel handlers (QQ/TG/WeChat private, qq_group)
    _gen_lock = GenerationLock("proactive")
    _gen_lock.__enter__()

    db = SessionLocal()
    try:
        session_id, assistant_name = _get_session_info_sync(ACHENG_ASSISTANT_ID)

        messages = _load_session_messages(db, session_id)

        # Compute trigger prompt values
        now = _now_beijing()

        # User gap
        last_user = (
            db.query(Message)
            .filter(Message.session_id == session_id, Message.role == "user")
            .order_by(Message.id.desc())
            .first()
        )
        if last_user and last_user.created_at:
            user_time = last_user.created_at
            if user_time.tzinfo is None:
                user_time = user_time.replace(tzinfo=TZ_EAST8)
            user_gap_str = _format_gap(now - user_time)
        else:
            user_gap_str = "未知"

        # Assistant gap (exclude reflection-triggered messages)
        last_assistant = (
            db.query(Message)
            .filter(
                Message.session_id == session_id,
                Message.role == "assistant",
                or_(
                    ~Message.meta_info.has_key("source"),
                    cast(Message.meta_info, JSONB_TYPE)["source"].astext != "reflection",
                ),
            )
            .order_by(Message.id.desc())
            .first()
        )
        if last_assistant and last_assistant.created_at:
            asst_time = last_assistant.created_at
            if asst_time.tzinfo is None:
                asst_time = asst_time.replace(tzinfo=TZ_EAST8)
            assistant_gap_str = _format_gap(now - asst_time)
        else:
            assistant_gap_str = "未知"

        # Build alarm_hint: extra (from expired inject) + fired + upcoming
        alarm_hint = extra_alarm_hint + _build_alarm_hint(now)
        app_usage_hint = _build_app_usage_hint(now)

        # Choose template based on first vs follow-up
        channel_hint = _get_channel_info(session_id)
        if is_first:
            trigger_content = _get_prompt(db, "prompt_trigger_first", TRIGGER_PROMPT_FIRST).format(
                now=now.strftime("%Y-%m-%d %H:%M"),
                user_gap=user_gap_str,
                assistant_gap=assistant_gap_str,
                alarm_hint=alarm_hint,
                app_usage_hint=app_usage_hint,
                channel_hint=channel_hint,
            )
        else:
            trigger_content = _get_prompt(db, "prompt_trigger_followup", TRIGGER_PROMPT_FOLLOWUP).format(
                now=now.strftime("%Y-%m-%d %H:%M"),
                user_gap=user_gap_str,
                assistant_gap=assistant_gap_str,
                alarm_hint=alarm_hint,
                app_usage_hint=app_usage_hint,
                channel_hint=channel_hint,
            )

        trigger_msg = {
            "role": "user",
            "content": trigger_content,
            "id": -1,
        }

        # Retry once on timeout (timeout=60s)
        new_msgs = None
        for attempt in range(2):
            # Record max message id before generation
            max_msg = (
                db.query(Message.id)
                .filter(Message.session_id == session_id)
                .order_by(Message.id.desc())
                .first()
            )
            max_id_before = max_msg[0] if max_msg else 0

            # Build fresh chat_service each attempt
            chat_service = ChatService(db, assistant_name, assistant_id=ACHENG_ASSISTANT_ID, source="proactive")
            chat_service.proactive_extra_prompt = _get_prompt(db, "prompt_proactive_extra", PROACTIVE_EXTRA_PROMPT)
            chat_service.tts_emotion_enabled = _get_setting_sync("proactive_voice_enabled", "false") == "true"
            chat_service.api_timeout = 60

            # Append trigger message with id=-1 to prevent DB persistence
            msgs_copy = [*messages, trigger_msg]

            try:
                # Consume SSE stream (side effects: saves assistant message to DB)
                for _ in chat_service.stream_chat_completion(session_id, msgs_copy, source="proactive"):
                    pass
                break  # success
            except Exception as api_err:
                if _is_timeout_error(api_err) and attempt == 0:
                    logger.warning("[proactive] Layer 3 timeout, retrying...")
                    _cleanup_partial_messages(db, session_id, max_id_before)
                    continue
                if _is_timeout_error(api_err):
                    logger.warning("[proactive] Layer 3 retry also failed, skipping this round")
                    _cleanup_partial_messages(db, session_id, max_id_before)
                    return None, None
                raise  # non-timeout error

        # Find new assistant messages (skip intermediate tool-round texts)
        new_msgs = (
            db.query(Message)
            .filter(
                Message.session_id == session_id,
                Message.id > max_id_before,
                Message.role == "assistant",
                Message.content.isnot(None),
                Message.content != "",
            )
            .order_by(Message.id.desc())
            .all()
        )
        # Filter out cafe replies (not meant for delivery)
        new_msgs = [
            m for m in new_msgs
            if not (m.meta_info or {}).get("cafe_reply")
        ]
        # For intermediate messages, strip [THINK] blocks — keep if there's deliverable text
        import re
        _THINK_RE = re.compile(r'\[THINK\].*?(?:\[/THINK\]|</THINK>|</thinking>|$)', re.DOTALL)
        processed = []
        for m in new_msgs:
            if (m.meta_info or {}).get("intermediate"):
                stripped = _THINK_RE.sub('', m.content or '').strip()
                if stripped:
                    m.content = stripped
                    processed.append(m)
                # else: pure thinking, skip
            else:
                processed.append(m)
        new_msgs = processed

        if not new_msgs:
            return None, None

        # new_msgs is desc — reverse to chronological order
        new_msgs_asc = list(reversed(new_msgs))
        # Filter out [群聊回复] echo — already persisted by cafe_reply, don't send to user
        new_msgs_asc = [
            m for m in new_msgs_asc
            if not (m.content or "").strip().startswith("[群聊回复]")
        ]
        if not new_msgs_asc:
            return None, None
        # Reconstruct full content (each [NEXT] part is a separate DB row)
        content = "\n[NEXT]\n".join(
            (m.content or "").strip() for m in new_msgs_asc if (m.content or "").strip()
        )
        msg = new_msgs_asc[-1]  # last message for id/metadata

        # Strip [THINK] blocks before any delivery path (QQ/WeChat/Telegram)
        content = re.sub(r'(?:\[THINK\]|<scratchpad>).*?(?:\[/THINK\]|</THINK>|</thinking>|</scratchpad>)', '', content, flags=re.DOTALL)
        for _orphan in ('<scratchpad>', '</scratchpad>', '[THINK]', '[/THINK]', '</THINK>', '</thinking>'):
            content = content.replace(_orphan, '')
        content = content.strip()

        if not content or "[NO_MESSAGE]" in content:
            # 这次 proactive 不推送 TG/QQ/微信。DB 里只对实际含 [NO_MESSAGE] token
            # 的那些 message 标 no_message,其他中间轮次保持可见(COT 日志能看完整过程)。
            # Prompt 保持"整轮会被清"作为 deterrent,代码不实际执行这个惩罚。
            new_assistant_msgs = (
                db.query(Message)
                .filter(
                    Message.session_id == session_id,
                    Message.id > max_id_before,
                    Message.role == "assistant",
                    Message.content.isnot(None),
                    Message.content != "",
                )
                .order_by(Message.id.asc())
                .all()
            )
            tagged_ids: list[int] = []
            for m in new_assistant_msgs:
                if (m.meta_info or {}).get("cafe_reply"):
                    continue
                if "[NO_MESSAGE]" in (m.content or ""):
                    m.meta_info = {**(m.meta_info or {}), "mode": "proactive", "no_message": True}
                    tagged_ids.append(m.id)
            if tagged_ids:
                db.commit()
            logger.info(
                "[proactive] NO_MESSAGE: tagged ids=%s (mid-round preserved)",
                tagged_ids or "none",
            )
            return None, None

        # Tag the message as proactive
        msg.meta_info = {**(msg.meta_info or {}), "mode": "proactive"}
        db.commit()

        logger.info("[proactive] Generated message (id=%d): %s", msg.id, content[:60])
        return content, msg.id
    except Exception as e:
        logger.exception("[proactive] Layer 3 error: %s", e)
        return None, None
    finally:
        _gen_lock.release()
        db.close()
        if cafe_service is not None:
            try:
                cafe_service.release_for_proactive()
            except Exception:
                pass


def _get_last_active_source() -> tuple[str, int | None, str | None]:
    """Return (source, qq_user_id, wechat_user_id). Defaults to ("telegram", None, None)."""
    from app.models.models import Settings
    db = SessionLocal()
    try:
        row = db.query(Settings).filter(Settings.key == "last_active_source").first()
        source = row.value if row and row.value in ("telegram", "qq", "wechat") else "telegram"
        qq_uid = None
        uid_row = db.query(Settings).filter(Settings.key == "last_active_qq_user_id").first()
        if uid_row and uid_row.value:
            qq_uid = int(uid_row.value)
        wechat_uid = None
        wx_row = db.query(Settings).filter(Settings.key == "last_active_wechat_user_id").first()
        if wx_row and wx_row.value:
            wechat_uid = wx_row.value
        return source, qq_uid, wechat_uid
    except Exception:
        return "telegram", None, None
    finally:
        db.close()


def _get_channel_info(session_id: int) -> str:
    """Build channel status description for trigger prompt."""
    db = SessionLocal()
    try:
        now = _now_beijing()

        # Last user message on Telegram
        tg_last = (
            db.query(Message)
            .filter(
                Message.session_id == session_id,
                Message.role == "user",
                Message.telegram_message_id.isnot(None),
            )
            .order_by(Message.id.desc())
            .first()
        )
        # Last user message on QQ
        qq_last = (
            db.query(Message)
            .filter(
                Message.session_id == session_id,
                Message.role == "user",
                Message.qq_message_id.isnot(None),
            )
            .order_by(Message.id.desc())
            .first()
        )

        lines = []
        channels = []
        if tg_last and tg_last.created_at:
            t = tg_last.created_at
            if t.tzinfo is None:
                t = t.replace(tzinfo=TZ_EAST8)
            lines.append(f"Telegram（她{_format_gap(now - t)}前活跃）")
        else:
            lines.append("Telegram")

        if qq_last and qq_last.created_at:
            t = qq_last.created_at
            if t.tzinfo is None:
                t = t.replace(tzinfo=TZ_EAST8)
            lines.append(f"QQ（她{_format_gap(now - t)}前活跃）")
        else:
            # Check if QQ is configured at all
            from app.models.models import Settings
            qq_uid_row = db.query(Settings).filter(Settings.key == "last_active_qq_user_id").first()
            if qq_uid_row and qq_uid_row.value:
                lines.append("QQ")

        # Last user message on WeChat
        wx_last = (
            db.query(Message)
            .filter(
                Message.session_id == session_id,
                Message.role == "user",
                Message.wechat_message_id.isnot(None),
            )
            .order_by(Message.id.desc())
            .first()
        )
        if wx_last and wx_last.created_at:
            t = wx_last.created_at
            if t.tzinfo is None:
                t = t.replace(tzinfo=TZ_EAST8)
            lines.append(f"微信（她{_format_gap(now - t)}前活跃）")
        else:
            wx_uid_row = db.query(Settings).filter(Settings.key == "last_active_wechat_user_id").first()
            if wx_uid_row and wx_uid_row.value:
                lines.append("微信")

        if not lines:
            return ""

        # Check last proactive message: where it was sent, whether user replied
        last_proactive = (
            db.query(Message)
            .filter(
                Message.session_id == session_id,
                Message.role == "assistant",
                cast(Message.meta_info, JSONB_TYPE)["mode"].astext == "proactive",
                ~Message.meta_info.has_key("no_message"),
            )
            .order_by(Message.id.desc())
            .first()
        )

        retry_line = ""
        if last_proactive:
            user_after = (
                db.query(Message)
                .filter(
                    Message.session_id == session_id,
                    Message.role == "user",
                    Message.id > last_proactive.id,
                )
                .first()
            )
            if user_after is None:
                if last_proactive.wechat_message_id:
                    last_via = "微信"
                elif last_proactive.qq_message_id:
                    last_via = "QQ"
                else:
                    last_via = "Telegram"
                retry_line = f"最后一条消息发送至{last_via}，暂未收到回复。\n"

        # Check group chat activity
        cafe_hint = ""
        try:
            from app.services.cafe_service import cafe_service
            last_user = cafe_service.get_last_user_activity()
            if last_user is not None and last_user < 30:
                cafe_hint = f"她{last_user}分钟前在🐰群里说过话。\n"
            # Check if we just replied to an @mention
            secs = cafe_service.seconds_since_cafe_reply()
            if secs is not None and secs < 90:
                cafe_hint += f"你{int(secs)}秒前刚在🐰群里回复了@mention。\n"
        except Exception:
            pass

        hint = "可用渠道：" + "、".join(lines) + "\n"
        if cafe_hint:
            hint += cafe_hint
        if retry_line:
            hint += retry_line
        hint += "【渠道使用规则】：回复开头加[VIA:telegram]或[VIA:qq]或[VIA:wechat]可指定发送渠道；不加则默认发送至她最近活跃的渠道。\n"
        return hint
    except Exception:
        logger.debug("[proactive] _get_channel_info error", exc_info=True)
        return ""
    finally:
        db.close()


def _has_recent_message(minutes: int = 5) -> bool:
    """Check if there's any non-empty message (user or assistant) within the last `minutes`.
    Used by random proactive cooldown — 助手A just replied (e.g. to a group @) is also a
    recent activity, so we shouldn't fire another proactive right on top of it."""
    db = SessionLocal()
    try:
        session_id = _get_session_id(db)
        if not session_id:
            return False
        cutoff = _now_beijing() - timedelta(minutes=minutes)
        msg = (
            db.query(Message)
            .filter(
                Message.session_id == session_id,
                Message.role.in_(["user", "assistant"]),
                Message.content != "",
                Message.created_at >= cutoff,
            )
            .first()
        )
        return msg is not None
    finally:
        db.close()


def _latest_message_time() -> datetime | None:
    """Return the most recent non-empty user/assistant message timestamp."""
    db = SessionLocal()
    try:
        session_id = _get_session_id(db)
        if not session_id:
            return None
        msg = (
            db.query(Message)
            .filter(
                Message.session_id == session_id,
                Message.role.in_(["user", "assistant"]),
                Message.content != "",
            )
            .order_by(Message.id.desc())
            .first()
        )
        if msg and msg.created_at:
            ts = msg.created_at
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=TZ_EAST8)
            return ts
        return None
    finally:
        db.close()


def _reschedule_random_from_latest_msg() -> None:
    """Re-roll the next random wakeup with the latest message (user or assistant) time
    as the base, so random proactive doesn't drift off the actual activity rhythm."""
    latest = _latest_message_time()
    rand_min = int(_get_setting_sync("proactive_random_min", "15"))
    rand_max = int(_get_setting_sync("proactive_random_max", "120"))
    if rand_min > rand_max:
        rand_min, rand_max = rand_max, rand_min
    wait_minutes = random.randint(rand_min, rand_max)
    now = _now_beijing()
    base = latest if latest else now
    next_time = base + timedelta(minutes=wait_minutes)
    if next_time < now + timedelta(minutes=1):
        # Base is so old that base+wait is already past — fall back to now-based
        next_time = now + timedelta(minutes=wait_minutes)
    _set_next_wakeup_sync(next_time)
    logger.info("[proactive] Random canceled (recent user msg), rescheduled to %s (+%dm from %s)",
                next_time.strftime("%H:%M"), wait_minutes,
                "latest-msg" if latest else "now")


async def _generate_and_send(is_first: bool = False, extra_alarm_hint: str = "", is_alarm: bool = False) -> bool:
    """Generate and send a proactive message. Returns True if actually sent.

    is_alarm=True for reminder-fired triggers (they queue behind the global lock
    and don't skip on recent user activity). is_alarm=False (default) is the
    random path: if a user message landed within the last 5 min, skip this round
    and reschedule random from the latest user-msg time."""
    if not is_alarm:
        if await asyncio.to_thread(_has_recent_message, 5):
            await asyncio.to_thread(_reschedule_random_from_latest_msg)
            return False
    content, msg_db_id = await asyncio.to_thread(_generate_sync, is_first, extra_alarm_hint)
    if not content:
        return False

    # Keep [NEXT] literal — downstream senders split on it:
    # - TG path: _send_with_optional_voice splits by [NEXT]
    # - QQ path: send_reply_with_voice does [NEXT]→\n\n + blank-line split
    # - WeChat path: send_reply does [NEXT]→\n\n + blank-line split
    # Old code replaced [NEXT] here but TG send only split by [NEXT], so one
    # big blob went through → TG saw a single message with blank lines.

    # Parse [VIA:xxx] tag from model output
    via_match = _VIA_TAG_RE.search(content)
    if via_match:
        chosen_source = via_match.group(1).lower()
        # Strip the tag from content before sending
        content = _VIA_TAG_RE.sub("", content).strip()
        await asyncio.to_thread(_strip_persisted_via_tags, msg_db_id, chosen_source)
        if not content:
            return False
    else:
        chosen_source = None

    # Determine target: model's choice > last_active_source > telegram
    default_source, qq_uid, wechat_uid = await asyncio.to_thread(_get_last_active_source)
    target = chosen_source or default_source

    if target == "qq":
        if not qq_uid:
            logger.warning("[proactive] Model chose QQ but no qq_uid, falling back to Telegram")
            target = "telegram"
        else:
            from app.qq.service import send_reply_with_voice as qq_send
            await qq_send(qq_uid, content)
            logger.info("[proactive] Sent via QQ to user %s (chosen=%s)", qq_uid, chosen_source or "auto")
            return True

    if target == "wechat":
        if not wechat_uid:
            logger.warning("[proactive] Model chose WeChat but no wechat_uid, falling back to Telegram")
            target = "telegram"
        else:
            from app.wechat.service import send_reply as wx_send
            await wx_send(wechat_uid, content)
            logger.info("[proactive] Sent via WeChat to user %s (chosen=%s)", wechat_uid, chosen_source or "auto")
            return True

    # Default: Telegram
    bot = bots.get("acheng")
    if not bot or not ALLOWED_CHAT_ID:
        logger.warning("[proactive] Bot or chat_id not available, cannot send")
        return False

    await _send_with_optional_voice(bot, content, msg_db_id)
    logger.info("[proactive] Sent via Telegram (chosen=%s)", chosen_source or "auto")
    return True


async def _send_with_optional_voice(bot, content: str, msg_db_id: int | None) -> None:
    """Send proactive message with [NEXT] splitting.
    Each part handles any number of [[voice:]] tags via re.split."""
    from app.services.tts_service import EMOTION_TAG_RE, VALID_EMOTIONS, synthesize

    # Check master switch
    voice_enabled = await get_setting("proactive_voice_enabled", "false") == "true"

    # Safety net: strip any leaked internal markers
    import re as _re
    content = _re.sub(r'(?:\[THINK\]|<scratchpad>).*?(?:\[/THINK\]|</THINK>|</thinking>|</scratchpad>)', '', content, flags=_re.DOTALL)
    for _orphan in ('<scratchpad>', '</scratchpad>', '[THINK]', '[/THINK]', '</THINK>', '</thinking>'):
        content = content.replace(_orphan, '')
    content = _re.sub(r'\[\[used:[\d,\s]+\]\]', '', content)
    content = _re.sub(r'\[#\s*\d+\s*\]', '', content)
    content = _re.sub(r'\(来源:\s*\w+\)\s*$', '', content, flags=_re.MULTILINE)
    # Strip any remaining VIA tags the main parser might have missed
    content = _VIA_TAG_RE.sub("", content)

    # Telegram: only split by [NEXT], keep empty lines within each message
    parts = [p.strip() for p in content.split("[NEXT]") if p.strip()]
    if not parts:
        return
    has_next = len(parts) > 1

    first_sent_id = None
    for i, part in enumerate(parts):
        if i > 0:
            await asyncio.sleep(_typing_delay(part))

        # Split by all voice tags: [text0, emotion1, text1, emotion2, text2, ...]
        segments = EMOTION_TAG_RE.split(part)

        if len(segments) == 1:
            # No voice tag — just send text
            clean = part.strip()
            if not clean:
                continue
            try:
                sent = await bot.send_message(chat_id=ALLOWED_CHAT_ID, text=clean)
                if first_sent_id is None:
                    first_sent_id = sent.message_id
            except Exception as e:
                logger.exception("[proactive] Telegram send error: %s", e)
            continue

        sent_something = False
        for idx in range(0, len(segments), 2):
            seg_text = segments[idx].strip()
            if not seg_text:
                continue

            if sent_something:
                await asyncio.sleep(_typing_delay(seg_text))

            if idx == 0:
                # Text before first voice tag → plain text
                try:
                    sent = await bot.send_message(chat_id=ALLOWED_CHAT_ID, text=seg_text)
                    if first_sent_id is None:
                        first_sent_id = sent.message_id
                    sent_something = True
                except Exception as e:
                    logger.exception("[proactive] Telegram send error: %s", e)
            else:
                # Text after a voice tag
                from app.services.tts_service import resolve_emotion
                emotion = resolve_emotion(segments[idx - 1])

                if has_next:
                    # Model used [NEXT] — voice entire segment
                    voice_line = seg_text
                    rest_text = ""
                else:
                    # Model forgot [NEXT] — only voice first line
                    lines = seg_text.split("\n", 1)
                    voice_line = lines[0].strip()
                    rest_text = lines[1].strip() if len(lines) > 1 else ""

                voice_sent = False
                if voice_enabled and voice_line and emotion and len(voice_line) <= 300:
                    try:
                        audio_bytes = await asyncio.to_thread(synthesize, voice_line, emotion)
                        if audio_bytes:
                            from aiogram.types import BufferedInputFile
                            voice_file = BufferedInputFile(audio_bytes, filename="voice.mp3")
                            sent = await bot.send_voice(chat_id=ALLOWED_CHAT_ID, voice=voice_file, caption=voice_line)
                            if first_sent_id is None:
                                first_sent_id = sent.message_id
                            sent_something = True
                            voice_sent = True
                            logger.info("[proactive] Sent voice with caption (emotion=%s, %d bytes)", emotion, len(audio_bytes))
                    except Exception as e:
                        logger.warning("[proactive] Voice send failed: %s", e)

                if voice_line and not voice_sent:
                    try:
                        sent = await bot.send_message(chat_id=ALLOWED_CHAT_ID, text=voice_line)
                        if first_sent_id is None:
                            first_sent_id = sent.message_id
                        sent_something = True
                    except Exception as e:
                        logger.exception("[proactive] Telegram send error: %s", e)

                if rest_text:
                    if sent_something:
                        await asyncio.sleep(_typing_delay(rest_text))
                    try:
                        sent = await bot.send_message(chat_id=ALLOWED_CHAT_ID, text=rest_text)
                        if first_sent_id is None:
                            first_sent_id = sent.message_id
                        sent_something = True
                    except Exception as e:
                        logger.exception("[proactive] Telegram send error: %s", e)

    if msg_db_id and first_sent_id:
        await update_telegram_message_id(msg_db_id, first_sent_id)


# ── Next-wakeup helpers ───────────────────────────────────────────────────────

def _get_next_wakeup_sync() -> datetime | None:
    """Read proactive_next_wakeup from Settings table."""
    from app.models.models import Settings
    db = SessionLocal()
    try:
        row = db.query(Settings).filter(Settings.key == "proactive_next_wakeup").first()
        if row and row.value:
            return datetime.fromisoformat(row.value)
        return None
    finally:
        db.close()


def _set_next_wakeup_sync(dt: datetime | None) -> None:
    """Write proactive_next_wakeup to Settings table."""
    from app.models.models import Settings
    db = SessionLocal()
    try:
        row = db.query(Settings).filter(Settings.key == "proactive_next_wakeup").first()
        val = dt.isoformat() if dt else ""
        if row:
            row.value = val
        else:
            db.add(Settings(key="proactive_next_wakeup", value=val))
        db.commit()
    finally:
        db.close()


def _upsert_setting_sync(key: str, value: str) -> None:
    from app.models.models import Settings
    db = SessionLocal()
    try:
        row = db.query(Settings).filter(Settings.key == key).first()
        if row:
            row.value = value
        else:
            db.add(Settings(key=key, value=value))
        db.commit()
    finally:
        db.close()


def _get_earliest_reminder_sync() -> tuple[datetime | None, int | None]:
    """Return (remind_at, id) of the earliest pending reminder, or (None, None)."""
    db = SessionLocal()
    try:
        r = (
            db.query(ProactiveReminder)
            .filter(ProactiveReminder.assistant_id == ACHENG_ASSISTANT_ID)
            .order_by(ProactiveReminder.remind_at.asc())
            .first()
        )
        if not r:
            return None, None
        rat = r.remind_at
        if rat.tzinfo is None:
            rat = rat.replace(tzinfo=TZ_EAST8)
        return rat, r.id
    finally:
        db.close()


def _consume_fired_reminders_sync() -> list[tuple[str, str]]:
    """Delete all fired reminders (remind_at <= now).
    Returns list of (time_str like '15:30', reason)."""
    now = _now_beijing()
    db = SessionLocal()
    try:
        fired = (
            db.query(ProactiveReminder)
            .filter(
                ProactiveReminder.assistant_id == ACHENG_ASSISTANT_ID,
                ProactiveReminder.remind_at <= now,
            )
            .order_by(ProactiveReminder.remind_at.asc())
            .all()
        )
        result = []
        for r in fired:
            rat = r.remind_at
            if rat.tzinfo is None:
                rat = rat.replace(tzinfo=TZ_EAST8)
            result.append((rat.strftime("%H:%M"), r.reason or ""))
        for r in fired:
            db.delete(r)
        if fired:
            db.commit()
            logger.info("[proactive] Consumed %d fired reminders", len(fired))
        return result
    finally:
        db.close()


def _get_upcoming_reminders_sync(now: datetime, minutes: int = 30) -> list[tuple[str, int, str]]:
    """Get reminders firing within `minutes` from now (but not yet fired).
    Returns list of (time_str, minutes_left, reason)."""
    cutoff = now + timedelta(minutes=minutes)
    db = SessionLocal()
    try:
        upcoming = (
            db.query(ProactiveReminder)
            .filter(
                ProactiveReminder.assistant_id == ACHENG_ASSISTANT_ID,
                ProactiveReminder.remind_at > now,
                ProactiveReminder.remind_at <= cutoff,
            )
            .order_by(ProactiveReminder.remind_at.asc())
            .all()
        )
        result = []
        for r in upcoming:
            rat = r.remind_at
            if rat.tzinfo is None:
                rat = rat.replace(tzinfo=TZ_EAST8)
            mins_left = max(1, int((rat - now).total_seconds() / 60))
            result.append((rat.strftime("%H:%M"), mins_left, r.reason or ""))
        return result
    finally:
        db.close()


def _has_fired_reminders() -> bool:
    """Check if there are any fired reminders (remind_at <= now)."""
    now = _now_beijing()
    db = SessionLocal()
    try:
        count = (
            db.query(ProactiveReminder)
            .filter(
                ProactiveReminder.assistant_id == ACHENG_ASSISTANT_ID,
                ProactiveReminder.remind_at <= now,
            )
            .count()
        )
        return count > 0
    finally:
        db.close()


def _has_recent_user_message(minutes: int = 5) -> bool:
    """Check if there's a user message within the last `minutes` minutes."""
    db = SessionLocal()
    try:
        session_id = _get_session_id(db)
        if not session_id:
            return False
        cutoff = _now_beijing() - timedelta(minutes=minutes)
        msg = (
            db.query(Message)
            .filter(
                Message.session_id == session_id,
                Message.role == "user",
                Message.created_at >= cutoff,
            )
            .first()
        )
        return msg is not None
    finally:
        db.close()


def _store_alarm_inject(fired: list[tuple[str, str]]) -> None:
    """Store fired alarm details for injection into active chat.
    fired: list of (time_str, reason)."""
    import json
    data = json.dumps({
        "stored_at": _now_beijing().isoformat(),
        "alarms": [{"time": t, "reason": r} for t, r in fired],
    })
    _upsert_setting_sync("proactive_alarm_inject", data)
    logger.info("[proactive] Stored %d alarm(s) for chat injection", len(fired))


def _check_and_get_expired_inject() -> dict | None:
    """Check if there's an expired alarm injection (stored > 5 min ago).
    Returns the inject data dict or None."""
    import json
    raw = _get_setting_sync("proactive_alarm_inject", "")
    if not raw:
        return None
    try:
        data = json.loads(raw)
        stored_at = datetime.fromisoformat(data["stored_at"])
        if _now_beijing() - stored_at > timedelta(minutes=5):
            return data
        return None
    except (json.JSONDecodeError, KeyError, ValueError):
        _upsert_setting_sync("proactive_alarm_inject", "")
        return None


def _clear_alarm_inject() -> None:
    _upsert_setting_sync("proactive_alarm_inject", "")


def _has_unlocked_diaries() -> bool:
    """Check if there are user diaries ready to notify (timed that unlocked, or immediate)."""
    now = _now_beijing()
    db = SessionLocal()
    try:
        count = (
            db.query(Diary)
            .filter(
                Diary.deleted_at.is_(None),
                Diary.author == "user",
                Diary.notified_at.is_(None),
                or_(
                    Diary.unlock_at.is_(None),
                    Diary.unlock_at <= now,
                ),
            )
            .count()
        )
        return count > 0
    finally:
        db.close()


def _get_earliest_diary_unlock_sync() -> datetime | None:
    """Return the earliest unlock_at of pending (un-notified) user diaries, or None."""
    now = _now_beijing()
    db = SessionLocal()
    try:
        d = (
            db.query(Diary)
            .filter(
                Diary.deleted_at.is_(None),
                Diary.author == "user",
                Diary.unlock_at.isnot(None),
                Diary.unlock_at > now,
                Diary.notified_at.is_(None),
            )
            .order_by(Diary.unlock_at.asc())
            .first()
        )
        if not d:
            return None
        unlock = d.unlock_at
        if unlock.tzinfo is None:
            unlock = unlock.replace(tzinfo=TZ_EAST8)
        return unlock
    finally:
        db.close()


def _schedule_followup_wakeup() -> datetime:
    """Schedule a random wakeup. Defers to a nearby reminder or diary unlock (within 10 min)."""
    rand_min = int(_get_setting_sync("proactive_random_min", "15"))
    rand_max = int(_get_setting_sync("proactive_random_max", "120"))
    if rand_min > rand_max:
        rand_min, rand_max = rand_max, rand_min
    wait_minutes = random.randint(rand_min, rand_max)
    next_time = _now_beijing() + timedelta(minutes=wait_minutes)

    # Find the earliest event: reminder or diary unlock
    reminder_at, _rid = _get_earliest_reminder_sync()
    diary_at = _get_earliest_diary_unlock_sync()

    # Pick whichever is sooner
    earliest = None
    for candidate in (reminder_at, diary_at):
        if candidate and (earliest is None or candidate < earliest):
            earliest = candidate

    if earliest:
        if earliest <= next_time:
            # Event is earlier → use it
            _set_next_wakeup_sync(earliest)
            logger.info("[proactive] Deferred to earlier event at %s (random would be %s)",
                         earliest.strftime("%H:%M"), next_time.strftime("%H:%M"))
            return earliest
        if earliest - next_time <= timedelta(minutes=10):
            # Event is within 10 min after random → merge
            _set_next_wakeup_sync(earliest)
            logger.info("[proactive] Merged with nearby event at %s (random was %s, diff %s)",
                         earliest.strftime("%H:%M"), next_time.strftime("%H:%M"),
                         earliest - next_time)
            return earliest

    _set_next_wakeup_sync(next_time)
    logger.info("[proactive] Scheduled random wakeup in %d min (%s)", wait_minutes, next_time.strftime("%H:%M"))
    return next_time


def _pending_reminders_list(db) -> list[dict]:
    """Return all pending reminders as a list for inclusion in tool results."""
    now = _now_beijing()
    reminders = (
        db.query(ProactiveReminder)
        .filter(ProactiveReminder.assistant_id == ACHENG_ASSISTANT_ID, ProactiveReminder.remind_at > now)
        .order_by(ProactiveReminder.remind_at.asc())
        .all()
    )
    items = []
    for r in reminders:
        rat = r.remind_at
        if rat.tzinfo is None:
            rat = rat.replace(tzinfo=TZ_EAST8)
        mins_left = max(0, int((rat - now).total_seconds() / 60))
        items.append({
            "id": r.id,
            "remind_at": rat.strftime("%H:%M"),
            "minutes_left": mins_left,
            "reason": r.reason or "",
        })
    return items


def set_reminder_sync(minutes: int, reason: str = "") -> dict:
    """Called by the set_reminder tool. Inserts a row into proactive_reminders table."""
    minutes = max(1, min(minutes, 1440))  # 1分钟 ~ 24小时
    next_time = _now_beijing() + timedelta(minutes=minutes)

    db = SessionLocal()
    try:
        count = db.query(ProactiveReminder).filter(
            ProactiveReminder.remind_at > _now_beijing()
        ).count()
        if count >= 10:
            return {"status": "error", "message": f"闹钟已满（{count}/10），请先取消一些再设新的"}

        reminder = ProactiveReminder(
            assistant_id=ACHENG_ASSISTANT_ID,
            remind_at=next_time,
            reason=reason or "",
        )
        db.add(reminder)
        db.commit()
        db.refresh(reminder)
        reminder_id = reminder.id

        # If random wakeup is within 10 min of this alarm (before or after) → cancel it
        current = _get_next_wakeup_sync()
        if current and abs((current - next_time).total_seconds()) <= 600:
            _set_next_wakeup_sync(None)
            _notify_wakeup()
            logger.info("[proactive] Cancelled nearby wakeup %s, alarm at %s will fire naturally",
                        current.strftime("%H:%M"), next_time.strftime("%H:%M"))
        else:
            # Just wake the loop to re-check (e.g. alarm is earlier than wakeup)
            _notify_wakeup()

        logger.info("[proactive] set_reminder #%d: %d min → %s (reason: %s)",
                     reminder_id, minutes, next_time.strftime("%H:%M"), reason or "-")
        return {
            "status": "ok",
            "id": reminder_id,
            "minutes": minutes,
            "remind_at": next_time.isoformat(),
            "message": f"已设定 {minutes} 分钟后唤醒 (闹钟#{reminder_id})",
            "pending_reminders": _pending_reminders_list(db),
        }
    finally:
        db.close()


def cancel_reminder_sync(reminder_id: int) -> dict:
    """Called by the cancel_reminder tool. Deletes a reminder by id."""
    db = SessionLocal()
    try:
        reminder = db.query(ProactiveReminder).filter(
            ProactiveReminder.id == reminder_id,
            ProactiveReminder.assistant_id == ACHENG_ASSISTANT_ID,
        ).first()
        if not reminder:
            return {"status": "not_found", "message": f"闹钟#{reminder_id}不存在或已取消"}
        db.delete(reminder)
        db.commit()
        logger.info("[proactive] cancel_reminder #%d", reminder_id)
        return {
            "status": "ok",
            "message": f"已取消闹钟#{reminder_id}",
            "pending_reminders": _pending_reminders_list(db),
        }
    finally:
        db.close()


def list_reminders_sync() -> dict:
    """Return all pending reminders for the model to see."""
    db = SessionLocal()
    try:
        reminders = (
            db.query(ProactiveReminder)
            .filter(ProactiveReminder.assistant_id == ACHENG_ASSISTANT_ID)
            .order_by(ProactiveReminder.remind_at.asc())
            .all()
        )
        now = _now_beijing()
        items = []
        for r in reminders:
            rat = r.remind_at
            if rat.tzinfo is None:
                rat = rat.replace(tzinfo=TZ_EAST8)
            remaining = rat - now
            mins_left = max(0, int(remaining.total_seconds() / 60))
            items.append({
                "id": r.id,
                "remind_at": rat.isoformat(),
                "minutes_left": mins_left,
                "reason": r.reason or "",
            })
        return {"reminders": items, "count": len(items)}
    finally:
        db.close()


# ── Main loop ────────────────────────────────────────────────────────────────

async def _interruptible_sleep(seconds: float) -> bool:
    """Sleep up to `seconds`, but wake early if _wakeup_event fires.
    Returns True if interrupted (woken early), False if timed out normally."""
    global _wakeup_event
    if _wakeup_event is None:
        _wakeup_event = asyncio.Event()
    _wakeup_event.clear()
    try:
        await asyncio.wait_for(_wakeup_event.wait(), timeout=max(0, seconds))
        return True  # interrupted
    except asyncio.TimeoutError:
        return False  # normal timeout


def _build_alarm_hint_from_inject(inject_data: dict) -> str:
    """Build alarm_hint text from expired inject data."""
    lines = []
    for alarm in inject_data.get("alarms", []):
        t, r = alarm.get("time", ""), alarm.get("reason", "")
        if r:
            lines.append(f"注意：你设在{t}的闹钟到了。备注：{r}\n")
        else:
            lines.append(f"注意：你设在{t}的闹钟到了。\n")
    return "".join(lines)


async def proactive_loop() -> None:
    """Background loop that checks and sends proactive messages.

    Flow:
    1. User stops chatting → first_min~first_max minutes → first proactive message
    2. After first message → random interval (retry_min~retry_max) → follow-ups
    3. Alarms always take priority and trigger immediate sends
    4. Enabling the switch doesn't immediately fire
    """
    global _wakeup_event
    _wakeup_event = asyncio.Event()
    await asyncio.sleep(30)  # Startup delay
    logger.info("[proactive] Loop started")

    last_enabled_at: datetime | None = None
    # Initialize from DB so restarts don't reset the timer
    was_enabled = await get_setting("proactive_enabled", "false") == "true"
    skip_to_followup = False  # True when user gap > first_max, skip first-message template
    first_delay_minutes: int | None = None  # Rolled once per first-message wait cycle

    while True:
        try:
            # ── Always: check fired alarms (regardless of enabled) ──
            has_fired = await asyncio.to_thread(_has_fired_reminders)
            if has_fired:
                if await asyncio.to_thread(_has_recent_user_message, 5):
                    # User is chatting → inject alarm into chat reply
                    fired = await asyncio.to_thread(_consume_fired_reminders_sync)
                    if fired:
                        await asyncio.to_thread(_store_alarm_inject, fired)
                        logger.info("[proactive] Active chat, stored %d alarm(s) for injection", len(fired))
                    await _interruptible_sleep(30)
                    continue
                else:
                    # User not chatting → alarm triggers immediate proactive
                    # Don't consume here — _build_alarm_hint inside _generate_sync will consume
                    state = await asyncio.to_thread(_get_proactive_state)
                    if state["session_id"]:
                        logger.info("[proactive] Alarm fired, user not chatting → sending proactive (is_first=%s)",
                                    not state["has_sent_first"])
                        await _generate_and_send(is_first=not state["has_sent_first"], is_alarm=True)
                        # Reset followup timer
                        await asyncio.to_thread(_schedule_followup_wakeup)
                    await _interruptible_sleep(30)
                    continue

            # ── Always: check unlocked diaries (regardless of enabled) ──
            has_unlocked_diary = await asyncio.to_thread(_has_unlocked_diaries)
            if has_unlocked_diary:
                if not await asyncio.to_thread(_has_recent_user_message, 5):
                    # User not chatting → trigger proactive so 助手A sees the diary hint
                    state = await asyncio.to_thread(_get_proactive_state)
                    if state["session_id"]:
                        logger.info("[proactive] Diary unlocked, user not chatting → sending proactive")
                        await _generate_and_send(is_first=not state["has_sent_first"])
                        await asyncio.to_thread(_schedule_followup_wakeup)
                    await _interruptible_sleep(30)
                    continue
                # User is chatting → diary hint will be picked up by chat_service._consume_diary_notifications

            # ── Always: check expired alarm injection (stored > 5 min, user stopped) ──
            expired_inject = await asyncio.to_thread(_check_and_get_expired_inject)
            if expired_inject:
                await asyncio.to_thread(_clear_alarm_inject)
                state = await asyncio.to_thread(_get_proactive_state)
                if state["session_id"]:
                    extra_alarm = _build_alarm_hint_from_inject(expired_inject)
                    logger.info("[proactive] Expired injection → sending proactive with alarm hint")
                    await _generate_and_send(is_first=not state["has_sent_first"], extra_alarm_hint=extra_alarm)
                    await asyncio.to_thread(_schedule_followup_wakeup)
                await _interruptible_sleep(30)
                continue

            # ── Check enabled state ──
            enabled = await get_setting("proactive_enabled", "false") == "true"

            # Track enable transitions to prevent immediate fire
            if enabled and not was_enabled:
                last_enabled_at = _now_beijing()
                skip_to_followup = False
                first_delay_minutes = None
                logger.info("[proactive] Just enabled, recording timestamp")
            was_enabled = enabled

            if not enabled:
                last_enabled_at = None
                skip_to_followup = False
                first_delay_minutes = None
                # Sleep until next alarm or 60s
                r_at, _ = await asyncio.to_thread(_get_earliest_reminder_sync)
                if r_at:
                    wait = max(0, (r_at - _now_beijing()).total_seconds())
                    await _interruptible_sleep(min(wait, 60))
                else:
                    await _interruptible_sleep(60)
                continue

            # ── Skip if user active recently ──
            if await asyncio.to_thread(_has_recent_user_message, 2):
                skip_to_followup = False  # User is chatting, reset to first-message flow
                first_delay_minutes = None
                await _interruptible_sleep(30)
                continue

            # ── Get proactive state ──
            state = await asyncio.to_thread(_get_proactive_state)
            if not state["session_id"]:
                await _interruptible_sleep(60)
                continue

            now = _now_beijing()

            if not state["has_sent_first"] and not skip_to_followup:
                # ── Cooldown guard: skip first-phase if a proactive attempt
                # (sent or NO_MESSAGE) happened within the last 10 minutes.
                # Prevents re-trigger when the service restarts shortly after
                # a proactive cycle — e.g. multiple commit pushes + webhook deploys.
                # has_sent_first excludes no_message, this guard doesn't.
                last_attempt = await asyncio.to_thread(_get_last_proactive_attempt_time)
                if last_attempt:
                    attempt_gap = (now - last_attempt).total_seconds()
                    if attempt_gap < 600:  # 10 min cooldown
                        logger.info(
                            "[proactive] Cooldown: last proactive attempt %.0fs ago, skipping first-phase",
                            attempt_gap,
                        )
                        skip_to_followup = True
                        first_delay_minutes = None
                        await asyncio.to_thread(_schedule_followup_wakeup)
                        await _interruptible_sleep(30)
                        continue

                # ── First message phase ──
                first_enabled = await get_setting("proactive_first_enabled", "true") == "true"
                if not first_enabled:
                    # Only alarms trigger proactive, sleep until next alarm
                    r_at, _ = await asyncio.to_thread(_get_earliest_reminder_sync)
                    if r_at:
                        wait = max(0, (r_at - now).total_seconds())
                        await _interruptible_sleep(min(wait, 300))
                    else:
                        await _interruptible_sleep(300)
                    continue

                first_min = int(await get_setting("proactive_first_min", "5"))
                first_max = int(await get_setting("proactive_first_max", "10"))
                if first_min > first_max:
                    first_min, first_max = first_max, first_min

                # Roll once per wait cycle
                if first_delay_minutes is None:
                    first_delay_minutes = random.randint(first_min, first_max)
                    logger.info("[proactive] Rolled first_delay = %d min", first_delay_minutes)

                needed_seconds = first_delay_minutes * 60

                # Both conditions: user gap AND enable gap must exceed first_delay
                user_ok = state["user_gap_seconds"] >= needed_seconds
                enable_ok = True
                if last_enabled_at:
                    enable_gap = (now - last_enabled_at).total_seconds()
                    enable_ok = enable_gap >= needed_seconds

                if user_ok and enable_ok:
                    logger.info("[proactive] First message triggered (user_gap=%.0fs, delay=%dm)",
                                state["user_gap_seconds"], first_delay_minutes)
                    sent = await _generate_and_send(is_first=True)
                    first_delay_minutes = None
                    if sent:
                        # Schedule first follow-up
                        await asyncio.to_thread(_schedule_followup_wakeup)
                    else:
                        # NO_MESSAGE — skip to follow-up phase to avoid re-triggering first message immediately
                        skip_to_followup = True
                        await asyncio.to_thread(_schedule_followup_wakeup)
                else:
                    # Sleep until ready
                    remaining_user = max(0, needed_seconds - state["user_gap_seconds"])
                    remaining_enable = 0
                    if last_enabled_at:
                        remaining_enable = max(0, needed_seconds - (now - last_enabled_at).total_seconds())
                    remaining = max(remaining_user, remaining_enable)
                    # Also check alarms
                    r_at, _ = await asyncio.to_thread(_get_earliest_reminder_sync)
                    sleep_time = remaining + 5  # 5s buffer
                    if r_at:
                        alarm_wait = max(0, (r_at - now).total_seconds())
                        sleep_time = min(sleep_time, alarm_wait)
                    await _interruptible_sleep(min(sleep_time, 30))
                continue

            # ── Follow-up phase ──
            retry_enabled = await get_setting("proactive_retry_enabled", "true") == "true"
            max_retries = int(await get_setting("proactive_max_retries", "8"))

            if not retry_enabled or state["consecutive"] >= max_retries:
                # No more follow-ups, only alarms
                r_at, _ = await asyncio.to_thread(_get_earliest_reminder_sync)
                if r_at:
                    wait = max(0, (r_at - now).total_seconds())
                    await _interruptible_sleep(min(wait, 300))
                else:
                    await _interruptible_sleep(300)
                continue

            # Check scheduled wakeup for follow-up
            next_wakeup = await asyncio.to_thread(_get_next_wakeup_sync)
            if not next_wakeup:
                next_wakeup = await asyncio.to_thread(_schedule_followup_wakeup)

            if now >= next_wakeup:
                logger.info("[proactive] Follow-up triggered (consecutive=%d)", state["consecutive"])
                await _generate_and_send(is_first=False)
                await asyncio.to_thread(_schedule_followup_wakeup)
            else:
                wait = (next_wakeup - now).total_seconds()
                # Also check alarms
                r_at, _ = await asyncio.to_thread(_get_earliest_reminder_sync)
                if r_at:
                    alarm_wait = max(0, (r_at - now).total_seconds())
                    wait = min(wait, alarm_wait)
                # Cap sleep at 30s so we detect user activity quickly
                wait = min(wait, 30)
                interrupted = await _interruptible_sleep(max(wait, 0))
                if interrupted:
                    logger.info("[proactive] Follow-up sleep interrupted by wakeup event")

        except Exception as e:
            logger.exception("[proactive] Loop error: %s", e)
            await asyncio.sleep(60)
