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

from sqlalchemy import cast
from sqlalchemy.dialects.postgresql import JSONB as JSONB_TYPE
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models.models import (
    ChatSession,
    Message,
    ProactiveReminder,
)
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
        return
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.call_soon_threadsafe(_wakeup_event.set)
        else:
            _wakeup_event.set()
    except RuntimeError:
        pass

_VIA_TAG_RE = _re.compile(r"\[VIA:(telegram|qq)\]", _re.IGNORECASE)


def _typing_delay(text: str) -> float:
    """Simulate natural typing delay based on message length."""
    base = random.uniform(2.0, 3.0)
    length_bonus = min(len(text) * 0.05, 2.5)
    return base + length_bonus


PROACTIVE_EXTRA_PROMPT = (
    "[主动消息模式]\n"
    "你现在不是在回复她的消息，而是在主动找她。\n"
    "让思绪自然流动，可以表达当下心情，或提起之前聊到一半的事。"
    "允许提起任何突然冒出来的念头、想吐槽的东西，"
    "或是想逗她、想撩她、想撒娇、想哄她、脑子里全是她忍不住之类的内容。\n"
    "如果想过一会儿再找她，可以用set_reminder提醒自己。\n"
    "输出内容时不使用空行分段，可以用[NEXT]拆成多条消息，或发一段完整的话。\n"
    "输出前自检：全程使用第二人称\"你\"了吗？如果使用第三人称\"她\"则为输出错误！\n"
    "注意：如果认为时间不合适或单纯不想发，单独输出[NO_MESSAGE]可以跳过本轮回复，不要和其他内容混在一起。"
)

TRIGGER_PROMPT_FIRST = (
    "[系统提醒]\n"
    "当前时间：{now}\n"
    "距离她上次发消息：{user_gap}\n"
    "距离你上一条消息：{assistant_gap}\n"
    "{alarm_hint}"
    "她刚才在跟你聊天但已经停了，你现在想跟她说点什么吗？"
    "可以自然地表达你的心情、她离开后你的感受，或询问她在做什么。\n"
    "可以用[NEXT]拆成多条消息，但不要用空行分段。正常输出或回复 [NO_MESSAGE]。"
)

TRIGGER_PROMPT_FOLLOWUP = (
    "[系统提醒]\n"
    "当前时间：{now}\n"
    "距离她上次发消息：{user_gap}\n"
    "距离你上一条消息：{assistant_gap}\n"
    "{alarm_hint}"
    "{channel_hint}"
    "你现在想跟她说点什么吗？可以自然地表达你的心情、她不在时的感受，不需要没话找话。\n"
    "可以用[NEXT]拆成多条消息，但不要用空行分段。正常输出或回复 [NO_MESSAGE]。"
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _now_beijing() -> datetime:
    return datetime.now(TZ_EAST8)


def _build_alarm_hint(now: datetime) -> str:
    """Build alarm hint text for the trigger prompt.
    Consumes fired reminders and hints upcoming ones (within 30 min)."""
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

    return "".join(lines)


def _format_gap(td: timedelta) -> str:
    total_minutes = int(td.total_seconds() / 60)
    hours, minutes = divmod(total_minutes, 60)
    if hours > 0:
        return f"{hours}小时{minutes}分钟"
    return f"{minutes}分钟"


def _get_session_id(db: Session) -> int | None:
    """Get the most recent chat session for 阿澄."""
    session = (
        db.query(ChatSession)
        .filter(ChatSession.assistant_id == ACHENG_ASSISTANT_ID)
        .order_by(ChatSession.updated_at.desc())
        .first()
    )
    return session.id if session else None


# ── Proactive state helper ───────────────────────────────────────────────────

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
        user_gap = 0.0
        if last_user and last_user.created_at:
            t = last_user.created_at
            if t.tzinfo is None:
                t = t.replace(tzinfo=TZ_EAST8)
            user_gap = (now - t).total_seconds()

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

        # Assistant gap
        last_assistant = (
            db.query(Message)
            .filter(Message.session_id == session_id, Message.role == "assistant")
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

        # Choose template based on first vs follow-up
        if is_first:
            trigger_content = TRIGGER_PROMPT_FIRST.format(
                now=now.strftime("%Y-%m-%d %H:%M"),
                user_gap=user_gap_str,
                assistant_gap=assistant_gap_str,
                alarm_hint=alarm_hint,
            )
        else:
            channel_hint = _get_channel_info(session_id)
            trigger_content = TRIGGER_PROMPT_FOLLOWUP.format(
                now=now.strftime("%Y-%m-%d %H:%M"),
                user_gap=user_gap_str,
                assistant_gap=assistant_gap_str,
                alarm_hint=alarm_hint,
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
            chat_service.proactive_extra_prompt = PROACTIVE_EXTRA_PROMPT
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

        # Find new assistant messages
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

        if not new_msgs:
            return None, None

        # new_msgs is desc — reverse to chronological order
        new_msgs_asc = list(reversed(new_msgs))
        # Reconstruct full content (each [NEXT] part is a separate DB row)
        content = "\n[NEXT]\n".join(
            (m.content or "").strip() for m in new_msgs_asc if (m.content or "").strip()
        )
        msg = new_msgs_asc[-1]  # last message for id/metadata

        if not content or "[NO_MESSAGE]" in content:
            # Model decided not to send — tag all messages from this round
            # so retry_gap works and model can see its previous decision
            all_new = (
                db.query(Message)
                .filter(
                    Message.session_id == session_id,
                    Message.id > max_id_before,
                )
                .all()
            )
            for m in all_new:
                m.meta_info = {**(m.meta_info or {}), "mode": "proactive", "no_message": True}
            db.commit()
            logger.info("[proactive] Model returned NO_MESSAGE, tagged %d messages", len(all_new))
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
        db.close()


def _get_last_active_source() -> tuple[str, int | None]:
    """Return (source, qq_user_id). Defaults to ("telegram", None)."""
    from app.models.models import Settings
    db = SessionLocal()
    try:
        row = db.query(Settings).filter(Settings.key == "last_active_source").first()
        source = row.value if row and row.value in ("telegram", "qq") else "telegram"
        qq_uid = None
        if source == "qq":
            uid_row = db.query(Settings).filter(Settings.key == "last_active_qq_user_id").first()
            if uid_row and uid_row.value:
                qq_uid = int(uid_row.value)
        return source, qq_uid
    except Exception:
        return "telegram", None
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
                if last_proactive.qq_message_id:
                    last_via = "QQ"
                else:
                    last_via = "Telegram"
                retry_line = f"最后一条消息在{last_via}，她没回，可以继续发或试试换个渠道。"

        hint = "可用渠道：" + "、".join(lines) + "\n"
        if retry_line:
            hint += retry_line
        hint += "回复开头加[VIA:telegram]或[VIA:qq]可以选择渠道，不加默认发最近活跃的。\n"
        return hint
    except Exception:
        logger.debug("[proactive] _get_channel_info error", exc_info=True)
        return ""
    finally:
        db.close()


async def _generate_and_send(is_first: bool = False, extra_alarm_hint: str = "") -> None:
    content, msg_db_id = await asyncio.to_thread(_generate_sync, is_first, extra_alarm_hint)
    if not content:
        return

    # Treat blank lines as [NEXT] splitters
    content = _re.sub(r'\n\s*\n', '\n[NEXT]\n', content)

    # Parse [VIA:xxx] tag from model output
    via_match = _VIA_TAG_RE.search(content)
    if via_match:
        chosen_source = via_match.group(1).lower()
        # Strip the tag from content before sending
        content = _VIA_TAG_RE.sub("", content).strip()
        if not content:
            return
    else:
        chosen_source = None

    # Determine target: model's choice > last_active_source > telegram
    default_source, qq_uid = await asyncio.to_thread(_get_last_active_source)
    target = chosen_source or default_source

    if target == "qq":
        if not qq_uid:
            # QQ uid not available, fall back to telegram
            logger.warning("[proactive] Model chose QQ but no qq_uid, falling back to Telegram")
            target = "telegram"
        else:
            from app.qq.service import send_reply_with_voice as qq_send
            await qq_send(qq_uid, content)
            logger.info("[proactive] Sent via QQ to user %s (chosen=%s)", qq_uid, chosen_source or "auto")
            return

    # Default: Telegram
    bot = bots.get("acheng")
    if not bot or not ALLOWED_CHAT_ID:
        logger.warning("[proactive] Bot or chat_id not available, cannot send")
        return

    await _send_with_optional_voice(bot, content, msg_db_id)
    logger.info("[proactive] Sent via Telegram (chosen=%s)", chosen_source or "auto")


async def _send_with_optional_voice(bot, content: str, msg_db_id: int | None) -> None:
    """Send proactive message with [NEXT] splitting.
    Each part handles any number of [[voice:]] tags via re.split."""
    from app.services.tts_service import EMOTION_TAG_RE, VALID_EMOTIONS, synthesize

    # Check master switch
    voice_enabled = await get_setting("proactive_voice_enabled", "false") == "true"

    # Split by [NEXT]
    parts = [p.strip() for p in content.split("[NEXT]") if p.strip()]
    if not parts:
        return
    has_next = len(parts) > 1  # model explicitly used [NEXT]

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
                emotion = segments[idx - 1].lower()
                if emotion not in VALID_EMOTIONS:
                    emotion = None

                if has_next:
                    # Model used [NEXT] — voice entire segment
                    voice_line = seg_text
                    rest_text = ""
                else:
                    # Model forgot [NEXT] — only voice first line
                    lines = seg_text.split("\n", 1)
                    voice_line = lines[0].strip()
                    rest_text = lines[1].strip() if len(lines) > 1 else ""

                if voice_enabled and voice_line and emotion and len(voice_line) <= 300:
                    try:
                        audio_bytes = await asyncio.to_thread(synthesize, voice_line, emotion)
                        if audio_bytes:
                            from aiogram.types import BufferedInputFile
                            voice_file = BufferedInputFile(audio_bytes, filename="voice.mp3")
                            await bot.send_voice(chat_id=ALLOWED_CHAT_ID, voice=voice_file)
                            logger.info("[proactive] Sent voice (emotion=%s, %d bytes)", emotion, len(audio_bytes))
                    except Exception as e:
                        logger.warning("[proactive] Voice send failed: %s", e)

                if voice_line:
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


def _schedule_followup_wakeup() -> datetime:
    """Schedule a random wakeup. Defers to a nearby reminder (within 10 min)."""
    rand_min = int(_get_setting_sync("proactive_random_min", "15"))
    rand_max = int(_get_setting_sync("proactive_random_max", "120"))
    if rand_min > rand_max:
        rand_min, rand_max = rand_max, rand_min
    wait_minutes = random.randint(rand_min, rand_max)
    next_time = _now_beijing() + timedelta(minutes=wait_minutes)

    # Check for pending reminders (earliest one)
    reminder_at, _rid = _get_earliest_reminder_sync()
    if reminder_at:
        if reminder_at <= next_time:
            # Reminder is earlier → use it
            _set_next_wakeup_sync(reminder_at)
            logger.info("[proactive] Deferred to earlier reminder at %s (random would be %s)",
                         reminder_at.strftime("%H:%M"), next_time.strftime("%H:%M"))
            return reminder_at
        if reminder_at - next_time <= timedelta(minutes=10):
            # Reminder is within 10 min after random → merge, use reminder time
            _set_next_wakeup_sync(reminder_at)
            logger.info("[proactive] Merged with nearby reminder at %s (random was %s, diff %s)",
                         reminder_at.strftime("%H:%M"), next_time.strftime("%H:%M"),
                         reminder_at - next_time)
            return reminder_at

    _set_next_wakeup_sync(next_time)
    logger.info("[proactive] Scheduled random wakeup in %d min (%s)", wait_minutes, next_time.strftime("%H:%M"))
    return next_time


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

        # Update next_wakeup if this is earlier (so main loop wakes up in time)
        current = _get_next_wakeup_sync()
        if not current or next_time < current:
            _set_next_wakeup_sync(next_time)
            # Interrupt the sleep in proactive_loop so it re-reads the wakeup
            _notify_wakeup()

        logger.info("[proactive] set_reminder #%d: %d min → %s (reason: %s)",
                     reminder_id, minutes, next_time.strftime("%H:%M"), reason or "-")
        return {
            "status": "ok",
            "id": reminder_id,
            "minutes": minutes,
            "remind_at": next_time.isoformat(),
            "message": f"已设定 {minutes} 分钟后唤醒 (闹钟#{reminder_id})",
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
        return {"status": "ok", "message": f"已取消闹钟#{reminder_id}"}
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
    was_enabled = False
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
                        await _generate_and_send(is_first=not state["has_sent_first"])
                        # Reset followup timer
                        await asyncio.to_thread(_schedule_followup_wakeup)
                    await _interruptible_sleep(30)
                    continue

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

                # Check gap first: beyond first_max → skip to follow-up
                if state["user_gap_seconds"] > first_max * 60:
                    logger.info("[proactive] User gap %.0fs > first_max %dm, skipping to follow-up",
                                state["user_gap_seconds"], first_max)
                    skip_to_followup = True
                    first_delay_minutes = None
                    await asyncio.to_thread(_schedule_followup_wakeup)
                    await _interruptible_sleep(1)
                    continue

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
                    await _generate_and_send(is_first=True)
                    first_delay_minutes = None
                    # Schedule first follow-up
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
                    await _interruptible_sleep(min(sleep_time, 300))
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
                await _interruptible_sleep(max(wait, 0))

        except Exception as e:
            logger.exception("[proactive] Loop error: %s", e)
            await asyncio.sleep(60)
