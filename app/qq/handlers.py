"""
QQ message handlers — mirrors app/telegram/handlers.py for OneBot v11.
QQ is always in short message mode (buffered).
"""
from __future__ import annotations

import asyncio
import base64
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from collections import deque

_TZ_EAST8 = timezone(timedelta(hours=8))
from .config import (
    QQ_ALLOWED_USER_IDS, QQ_ASSISTANT_ID,
    QQ_GROUP_ID, QQ_BOT_UIN, QQ_OWNER_UID,
    QQ_GROUP_ALLOWED_SENDERS,
)
from . import napcat_api
from app.services.media_service import compress_image_if_needed
from .service import (
    call_chat_completion,
    call_chat_completion_with_image,
    encode_photo_base64,
    get_buffer_seconds,
    get_session_info,
    lookup_by_qq_message_id,
    send_reply_with_voice,
    store_message_only,
    update_qq_message_id,
)

logger = logging.getLogger(__name__)

# Static header for the QQ group @-mention trigger — editable via PromptEditor.
# Runtime appends the @'d message content from the owner.
DEFAULT_QQ_GROUP_TRIGGER_HEADER = (
    "[QQ群聊通知]\n"
    "你在用户的 QQ 群里被@了。\n"
    "回话采用日常聊天的表达习惯，语气轻松，无动作描写，追求流畅真实的聊天质感，避免生硬的书面化表达。\n"
    "请参考以下群消息，直接通过 qq_group_chat send 工具回复。"
)

# Rolling buffer of the last 50 QQ group messages (from QQ_GROUP_ID only).
# Normal trigger picks the latest 30, but can expand up to 50 if the @'d message
# has already rolled out of the initial 30-window (happens when chat's flying).
_QQ_GROUP_BUFFER: deque[dict] = deque(maxlen=50)
_QQ_GROUP_TRIGGER_WINDOW = 30
_QQ_GROUP_TRIGGER_WINDOW_MAX = 50


async def _reschedule_random_proactive() -> None:
    """When user sends a message, clear the followup wakeup so the loop resets."""
    try:
        from .service import get_setting
        if await get_setting("proactive_enabled", "false") != "true":
            return
        from app.services.proactive_service import _set_next_wakeup_sync, _notify_wakeup
        await asyncio.to_thread(_set_next_wakeup_sync, None)
        _notify_wakeup()  # interrupt current sleep so loop re-reads state
    except Exception:
        pass  # non-critical, don't break message handling


# ── Per-user state ───────────────────────────────────────────────────────────

@dataclass
class _ChatBuffer:
    messages: list[str] = field(default_factory=list)
    message_ids: list[int] = field(default_factory=list)
    timer_task: Optional[asyncio.Task] = None


_buffers: dict[int, _ChatBuffer] = {}
_processed_msg_ids: set[int] = set()
_DEDUP_MAX = 500


def _is_allowed(user_id: int) -> bool:
    if QQ_ALLOWED_USER_IDS == {0}:
        return True
    return user_id in QQ_ALLOWED_USER_IDS


_PLATFORM_SWITCH_PROMPTS: dict[str, str] = {
    "telegram": (
        "[环境切换] 当前平台：Telegram（长消息模式）。"
        "注意：从本条消息起严格按以下规则输出，不再沿用之前的回复风格。"
        "要求：采用第一视角叙事，仅描述自身动作、神态与状态；说话内容用双引号包裹，与动作、神态自然交织为完整段落。"
        "不拆条、不使用[NEXT]，回复需连贯饱满。内心情绪通过动作与语气含蓄表达，不使用直白心理旁白。"
        "回复中统一使用第二人称\"你\"称呼对方，禁止使用\"她\"。"
    ),
    "qq": (
        "[环境切换] 当前平台：QQ（短消息模式）。"
        "注意：从本条消息起严格按以下规则输出，不再沿用之前的回复风格。"
        "要求：采用日常短消息表达习惯，语气轻松自然；无动作描写，语句以逗号或空格分隔，"
        "可使用[NEXT]拆条，不使用空行分段。整体追求流畅真实的聊天质感，避免生硬书面化。"
    ),
}


def _record_last_active(user_id: int) -> None:
    """Record that user was last active on QQ. Insert mode switch message if platform changed."""
    from app.database import SessionLocal
    from app.models.models import ChatSession, Settings
    from app.models.models import Message as MessageModel
    from sqlalchemy import text

    db = SessionLocal()
    try:
        # Update settings (for proactive service to know where to send)
        for key, value in [("last_active_source", "qq"), ("last_active_qq_user_id", str(user_id))]:
            row = db.query(Settings).filter(Settings.key == key).first()
            if row:
                row.value = value
            else:
                db.add(Settings(key=key, value=value))

        # Detect platform switch by checking last message's source
        latest_session = (
            db.query(ChatSession)
            .order_by(ChatSession.updated_at.desc(), ChatSession.id.desc())
            .first()
        )
        old_source = None
        if latest_session:
            last_msg = (
                db.query(MessageModel)
                .filter(
                    MessageModel.session_id == latest_session.id,
                    MessageModel.role.in_(["user", "assistant"]),
                    MessageModel.meta_info["source"].astext.in_(["qq", "telegram"]),
                )
                .order_by(MessageModel.id.desc())
                .first()
            )
            old_source = last_msg.meta_info.get("source") if last_msg else None

            # Override old_source if model recently switched via switch_channel
            recent_switch_tool = (
                db.query(MessageModel)
                .filter(
                    MessageModel.session_id == latest_session.id,
                    MessageModel.role == "tool",
                    MessageModel.meta_info["tool_name"].astext == "switch_channel",
                    MessageModel.id > (last_msg.id if last_msg else 0),
                )
                .order_by(MessageModel.id.desc())
                .first()
            )
            if recent_switch_tool:
                content = recent_switch_tool.content or ""
                if "Telegram" in content:
                    old_source = "telegram"
                elif "QQ" in content:
                    old_source = "qq"

        # Platform changed (or no source history) → insert mode switch system message
        need_switch = old_source is not None and old_source != "qq"
        # Avoid duplicate: check if a mode_switch message was already inserted recently
        if need_switch and latest_session:
            recent_switch = (
                db.query(MessageModel)
                .filter(
                    MessageModel.session_id == latest_session.id,
                    MessageModel.role == "system",
                    MessageModel.meta_info["mode_switch"].astext == "true",
                    MessageModel.id > (last_msg.id if last_msg else 0),
                )
                .first()
            )
            if recent_switch:
                need_switch = False
        # Skip if model already switched to QQ via switch_channel tool
        if need_switch and latest_session and last_msg:
            recent_tool_switch = (
                db.query(MessageModel)
                .filter(
                    MessageModel.session_id == latest_session.id,
                    MessageModel.role == "tool",
                    MessageModel.meta_info["tool_name"].astext == "switch_channel",
                    MessageModel.id > (last_msg.id if last_msg else 0),
                )
                .order_by(MessageModel.id.desc())
                .first()
            )
            if recent_tool_switch and "QQ" in (recent_tool_switch.content or ""):
                need_switch = False
        if need_switch:
            prompt = _PLATFORM_SWITCH_PROMPTS.get("qq", "")
            if prompt and latest_session:
                db.add(MessageModel(
                    session_id=latest_session.id,
                    role="system",
                    content=prompt,
                    meta_info={"mode_switch": True, "source": "qq"},
                ))

        db.commit()
        logger.info("[qq] last_active: %s → qq, switch_msg=%s", old_source, need_switch)
    except Exception as exc:
        logger.error("[qq] Failed to record last_active: %s", exc, exc_info=True)
    finally:
        db.close()


# ── Main entry point ─────────────────────────────────────────────────────────

async def handle_qq_event(event: dict[str, Any]) -> None:
    """Called from router.py for each OneBot v11 event."""
    post_type = event.get("post_type")
    if post_type != "message":
        return

    msg_type = event.get("message_type")

    # Group @-mention: only QQ_OWNER_UID @-ing QQ_BOT_UIN in QQ_GROUP_ID triggers a reply.
    if msg_type == "group":
        await _handle_group_mention_event(event)
        return

    # Private messages (existing path)
    if msg_type != "private":
        return

    user_id: int = event.get("user_id", 0)
    if not _is_allowed(user_id):
        return

    # Dedup
    msg_id: int = event.get("message_id", 0)
    if msg_id in _processed_msg_ids:
        return
    _processed_msg_ids.add(msg_id)
    if len(_processed_msg_ids) > _DEDUP_MAX:
        sorted_ids = sorted(_processed_msg_ids)
        _processed_msg_ids.clear()
        _processed_msg_ids.update(sorted_ids[len(sorted_ids) - _DEDUP_MAX // 2:])

    # Record last active platform
    await asyncio.to_thread(_record_last_active, user_id)

    # Reset proactive loop (same as TG handler)
    asyncio.create_task(_reschedule_random_proactive())

    segments: list[dict] = event.get("message", [])
    await _dispatch_message(user_id, msg_id, segments)


# ── Message dispatch ─────────────────────────────────────────────────────────

async def _dispatch_message(user_id: int, msg_id: int, segments: list[dict]) -> None:
    """Parse OneBot message segment array and route by content type."""
    text_parts: list[str] = []
    voice_url: str | None = None
    image_url: str | None = None
    reply_qq_id: int | None = None

    for seg in segments:
        seg_type = seg.get("type", "")
        data = seg.get("data", {})

        if seg_type == "text":
            t = data.get("text", "").strip()
            if t:
                text_parts.append(t)

        elif seg_type == "record":
            voice_url = data.get("url") or data.get("file")

        elif seg_type == "image":
            image_url = data.get("url") or data.get("file")

        elif seg_type == "reply":
            try:
                reply_qq_id = int(data.get("id", 0))
            except (ValueError, TypeError):
                reply_qq_id = None

    # Handle reply/quote — look up the quoted message by qq_message_id.
    # Keep the quote as its own block so the timestamp can prefix the user's
    # new message only, not the quote.
    quote_line = ""
    if reply_qq_id:
        quoted = await lookup_by_qq_message_id(reply_qq_id)
        if quoted:
            quote_line = f"[引用 id={quoted['id']}]「{quoted['content']}」"

    # ── Voice message ──
    if voice_url:
        await _handle_voice(user_id, msg_id, voice_url)
        return

    # ── Image message ──
    if image_url:
        caption = " ".join(text_parts) if text_parts else ""
        if quote_line:
            caption = f"{quote_line}\n{caption}" if caption else quote_line
        await _handle_image(user_id, msg_id, image_url, caption)
        return

    # ── Text message ──
    combined = " ".join(text_parts)
    if combined:
        await _handle_text(user_id, msg_id, combined, quote_line)


# ── Text (always short mode — buffer) ────────────────────────────────────────

def _store_qq_user_msg_sync(session_id: int, wrapped: str, msg_id: int) -> None:
    """Store a QQ private user message immediately so UI sees it before buffer fires.
    If a generation is active, tag meta.after_request_id so the loader reorders this
    message to come after that request's replies (not interleaved mid-request)."""
    from app.database import SessionLocal
    from app.models.models import Message as MessageModel
    from app.cot_broadcaster import publish_messages_updated
    from app.services.proactive_service import touch_last_user_message_at
    from app.services.generation_coordinator import current_request_id

    meta: dict = {"source": "qq"}
    active_rid = current_request_id()
    if active_rid:
        meta["after_request_id"] = active_rid

    db = SessionLocal()
    try:
        m = MessageModel(
            session_id=session_id,
            role="user",
            content=wrapped,
            meta_info=meta,
            qq_message_id=[msg_id],
        )
        db.add(m)
        db.commit()
        db.refresh(m)
        publish_messages_updated(session_id, m.id)
        touch_last_user_message_at(db)
    finally:
        db.close()


async def _store_qq_user_msg(user_id: int, text: str, msg_id: int, is_first_in_batch: bool, quote_line: str = "") -> None:
    session_id, _ = await get_session_info(QQ_ASSISTANT_ID)
    # UI renders created_at separately; loader (_apply_user_prefix in chat_service)
    # injects the [YYYY.MM.DD HH:MM:SS] timestamp into the model-facing payload.
    # Don't duplicate timestamp here.
    # [QQ私聊] scene header only on the first message of each buffer batch.
    parts: list[str] = []
    if is_first_in_batch:
        parts.append("[QQ私聊]")
    if quote_line:
        parts.append(quote_line)
    parts.append(text)
    wrapped = "\n".join(parts)
    await asyncio.to_thread(_store_qq_user_msg_sync, session_id, wrapped, msg_id)


async def _handle_text(user_id: int, msg_id: int, text: str, quote_line: str = "") -> None:
    delay = await get_buffer_seconds()
    buf = _buffers.setdefault(user_id, _ChatBuffer())
    is_first_in_batch = len(buf.messages) == 0
    # Store immediately so UI shows the message right away. Messages arriving
    # during an active generation are tagged with after_request_id in meta so
    # the loader reorders them to sit after that request's replies.
    await _store_qq_user_msg(user_id, text, msg_id, is_first_in_batch, quote_line)
    buf.messages.append(text)
    buf.message_ids.append(msg_id)
    if buf.timer_task and not buf.timer_task.done():
        buf.timer_task.cancel()
    task = asyncio.create_task(_buffer_fire(user_id, delay))
    buf.timer_task = task


async def _buffer_fire(user_id: int, delay: float) -> None:
    from app.services.generation_coordinator import current_holder
    task = asyncio.current_task()
    await asyncio.sleep(delay)
    buf = _buffers.get(user_id)
    if not buf or not buf.messages:
        return
    if buf.timer_task is not task:
        return
    # Wait for any active generation (from this or another channel) to finish.
    # During the wait, new QQ messages cancel this task and start a fresh timer,
    # so all messages received while generation is locked end up in one batch.
    while current_holder() is not None:
        await asyncio.sleep(0.5)
        buf = _buffers.get(user_id)
        if not buf or buf.timer_task is not task:
            return
    messages_snapshot = list(buf.messages)
    msg_ids_snapshot = list(buf.message_ids)
    buf.messages.clear()
    buf.message_ids.clear()
    buf.timer_task = None
    # Messages are already in DB (stored on arrival in _handle_text). Just trigger
    # the chat completion; the loader will reorder any after_request_id ones.
    combined = "\n".join(messages_snapshot)
    await _process_request(user_id, combined, is_short=True, qq_message_ids=msg_ids_snapshot)


async def _process_request(
    user_id: int,
    text: str,
    is_short: bool,
    qq_message_ids: list[int] | None = None,
) -> None:
    try:
        session_id, assistant_name = await get_session_info(QQ_ASSISTANT_ID)
        result_messages = await call_chat_completion(
            session_id, assistant_name, text, short_mode=is_short,
            assistant_id=QQ_ASSISTANT_ID,
            qq_message_ids=qq_message_ids,
            user_id=user_id,
        )
        for i, msg in enumerate(result_messages):
            content = (msg.get("content") or "").strip()
            if not content or msg.get("no_message"):
                continue
            # Route to Telegram if model switched channel
            if msg.get("_switched_channel") == "telegram":
                try:
                    from app.telegram.handlers import _send_reply_with_voice as tg_send
                    from app.telegram.bot_instance import bots
                    from app.telegram.config import ALLOWED_CHAT_ID
                    bot = bots.get("acheng")
                    if bot:
                        await tg_send(bot, ALLOWED_CHAT_ID, content)
                        logger.info("[switch_channel] Routed reply to Telegram (acheng)")
                        continue
                except Exception as e:
                    logger.error("[switch_channel] Failed to route to Telegram: %s", e)
            elif msg.get("_switched_channel") == "wechat":
                try:
                    from app.wechat.service import send_reply as wx_send
                    from app.telegram.service import get_setting
                    wx_uid = await get_setting("last_active_wechat_user_id")
                    if wx_uid:
                        await wx_send(wx_uid, content)
                        logger.info("[switch_channel] Routed reply to WeChat (uid=%s)", wx_uid)
                        continue
                except Exception as e:
                    logger.error("[switch_channel] Failed to route to WeChat: %s", e)
            if i > 0:
                from app.qq.service import _typing_delay
                await asyncio.sleep(_typing_delay(content))
            sent_qq_id = await send_reply_with_voice(user_id, content)
            if sent_qq_id and msg.get("db_id"):
                await update_qq_message_id(msg["db_id"], sent_qq_id)

    except Exception as exc:
        logger.error("[qq] Error processing request for user %s: %s", user_id, exc, exc_info=True)
        try:
            await napcat_api.send_private_msg(user_id, "出错了，请稍后再试")
        except Exception:
            pass


# ── Voice (always short mode — buffer) ───────────────────────────────────────

async def _handle_voice(user_id: int, msg_id: int, voice_url: str) -> None:
    try:
        audio_data = await napcat_api.download_file(voice_url)
    except Exception as exc:
        logger.error("[qq] Failed to download voice: %s", exc)
        return

    # STT
    from app.services.stt_service import transcribe
    text = await asyncio.to_thread(transcribe, audio_data, "voice.silk")
    if not text:
        await napcat_api.send_private_msg(user_id, "语音识别失败，请重新发送或改用文字")
        return

    logger.info("[qq voice] Transcribed: %s", text[:60])
    text = f"[语音消息] {text}"

    delay = await get_buffer_seconds()
    buf = _buffers.setdefault(user_id, _ChatBuffer())
    is_first_in_batch = len(buf.messages) == 0
    buf.messages.append(text)
    buf.message_ids.append(msg_id)
    await _store_qq_user_msg(user_id, text, msg_id, is_first_in_batch)
    if buf.timer_task and not buf.timer_task.done():
        buf.timer_task.cancel()
    buf.timer_task = asyncio.create_task(_buffer_fire(user_id, delay))


# ── Image (always short mode) ────────────────────────────────────────────────

async def _handle_image(user_id: int, msg_id: int, image_url: str, caption: str) -> None:
    try:
        image_bytes = await napcat_api.download_file(image_url)
    except Exception as exc:
        logger.error("[qq] Failed to download image: %s", exc)
        return

    image_data = await asyncio.to_thread(encode_photo_base64, image_bytes, "image/jpeg")

    if caption:
        content = f"{caption}\n\n[图片]"
    else:
        content = "[图片]"

    session_id, assistant_name = await get_session_info(QQ_ASSISTANT_ID)

    # Short mode: store image with qq_message_id, buffer caption text if present
    await store_message_only(session_id, content, image_data=image_data, qq_message_id=[msg_id])
    if caption:
        delay = await get_buffer_seconds()
        buf = _buffers.setdefault(user_id, _ChatBuffer())
        buf.messages.append(caption)
        buf.message_ids.append(msg_id)
        if buf.timer_task and not buf.timer_task.done():
            buf.timer_task.cancel()
        buf.timer_task = asyncio.create_task(_buffer_fire(user_id, delay))


# ── QQ group @-mention handling ──────────────────────────────────────────────

def _parse_segments(segments: list[dict]) -> dict:
    """Flatten OneBot message segments. Text + @ become text; images become [图片] placeholder
    plus URLs captured separately for optional download later."""
    parts: list[str] = []
    image_urls: list[str] = []
    for seg in segments:
        seg_type = seg.get("type")
        data = seg.get("data", {})
        if seg_type == "text":
            t = data.get("text", "").strip()
            if t:
                parts.append(t)
        elif seg_type == "at":
            qq = str(data.get("qq") or "")
            nick = data.get("name") or ""
            if qq == str(QQ_BOT_UIN):
                parts.append("@助手A")
            else:
                parts.append(f"@{nick or qq}")
        elif seg_type == "image":
            url = data.get("url") or data.get("file")
            if url:
                image_urls.append(url)
            parts.append("[图片]")
    return {"text": " ".join(parts).strip(), "image_urls": image_urls}


def _segments_to_plain_text(segments: list[dict]) -> str:
    """Backward-compatible helper — returns just the text part."""
    return _parse_segments(segments)["text"]


async def _handle_group_mention_event(event: dict[str, Any]) -> None:
    """Process a group message: buffer it, and if QQ_OWNER_UID @'d the bot, trigger reply."""
    if event.get("group_id") != QQ_GROUP_ID:
        return

    segments: list[dict] = event.get("message", [])
    sender_id = int(event.get("user_id") or 0)
    sender_info = event.get("sender") or {}
    sender_name = (
        sender_info.get("card")
        or sender_info.get("nickname")
        or str(sender_id)
    )
    parsed = _parse_segments(segments)
    text = parsed["text"]
    image_urls = parsed["image_urls"]

    # Always buffer the message (even from bot itself, so context is accurate)
    if text or image_urls:
        _QQ_GROUP_BUFFER.append({
            "msg_id": int(event.get("message_id") or 0),
            "sender": sender_name,
            "text": text or "[图片]",
            "image_urls": image_urls,
            "user_id": sender_id,
            "time": datetime.now(_TZ_EAST8).strftime("%m-%d %H:%M"),
        })

    # Only whitelisted senders @-ing QQ_BOT_UIN trigger generation
    if sender_id not in QQ_GROUP_ALLOWED_SENDERS:
        return

    at_bot = any(
        seg.get("type") == "at" and str(seg.get("data", {}).get("qq")) == str(QQ_BOT_UIN)
        for seg in segments
    )
    if not at_bot:
        return

    # Dedup on group message id
    msg_id: int = event.get("message_id", 0)
    if msg_id in _processed_msg_ids:
        return
    _processed_msg_ids.add(msg_id)
    if len(_processed_msg_ids) > _DEDUP_MAX:
        sorted_ids = sorted(_processed_msg_ids)
        _processed_msg_ids.clear()
        _processed_msg_ids.update(sorted_ids[len(sorted_ids) - _DEDUP_MAX // 2:])

    if not text:
        return  # Empty @ with no text — no trigger

    at_time = datetime.now(_TZ_EAST8).strftime("%m-%d %H:%M")
    await asyncio.to_thread(_generate_qq_group_reply_sync, sender_name, text, msg_id, at_time)


def _build_qq_group_context(at_msg_id: int, at_sender: str, at_text: str, at_time: str) -> str:
    """Build the 'recent group messages' block for the trigger.
    Default window = 30 most recent; expand to 50 if @'d msg rolled out; if still
    missing, show the @'d msg alone on top, '...', then what's in the 50-window.
    """
    buffer_list = list(_QQ_GROUP_BUFFER)
    if not buffer_list:
        return f"[{at_time}] {at_sender}: {at_text}"
    window30 = buffer_list[-_QQ_GROUP_TRIGGER_WINDOW:]
    if any(b.get("msg_id") == at_msg_id for b in window30):
        return "\n".join(f"[{b.get('time', '')}] {b['sender']}: {b['text']}" for b in window30)
    window50 = buffer_list[-_QQ_GROUP_TRIGGER_WINDOW_MAX:]
    if any(b.get("msg_id") == at_msg_id for b in window50):
        return "\n".join(f"[{b.get('time', '')}] {b['sender']}: {b['text']}" for b in window50)
    # @'d msg has rolled out even of the 50-window (chat was flying during queue wait).
    at_line = f"[{at_time}] {at_sender}: {at_text}"
    window_lines = [f"[{b.get('time', '')}] {b['sender']}: {b['text']}" for b in window50]
    return "\n".join([at_line, "...", *window_lines])


def _generate_qq_group_reply_sync(sender_name: str, at_text: str, quote_msg_id: int, at_time: str = "") -> None:
    """Run ChatService with source='qq_group'. The model calls qq_group_chat.send tool itself
    to push the reply into the group (stream stops after that tool action)."""
    from app.database import SessionLocal
    from app.routers.chat import _load_session_messages
    from app.services.chat_service import ChatService
    from app.services.generation_coordinator import GenerationLock
    from app.models.models import ChatSession, Message as MessageModel, Settings as SettingsModel

    _gen_lock = GenerationLock("qq_group")
    _gen_lock.__enter__()
    db = SessionLocal()
    try:
        session = (
            db.query(ChatSession)
            .filter(ChatSession.assistant_id == QQ_ASSISTANT_ID)
            .order_by(ChatSession.updated_at.desc())
            .first()
        )
        if not session:
            logger.warning("[qq_group] No active session for 助手A")
            return

        # Persist a system note so later rounds see why a QQ-group reply happened
        note = (
            f"[QQ群@] {sender_name}在QQ群里@了你\n"
            f"艾特内容: {at_text}"
        )
        db.add(MessageModel(
            session_id=session.id,
            role="system",
            content=note,
            meta_info={"source": "qq_group"},
        ))
        db.commit()

        # Header is editable via PromptEditor
        header_row = db.query(SettingsModel).filter(SettingsModel.key == "prompt_qq_group_trigger_header").first()
        header = header_row.value if header_row and header_row.value else DEFAULT_QQ_GROUP_TRIGGER_HEADER

        # Build context — default 30 most recent, extend to 50 if @'d msg out of window
        context = _build_qq_group_context(quote_msg_id, sender_name, at_text, at_time or datetime.now(_TZ_EAST8).strftime("%m-%d %H:%M"))

        # Download images from the last 5 buffered messages so the model can see them.
        # Older images stay as "[图片]" placeholders (already in the text above).
        from app.services.cafe_service import cafe_service as _cafe_svc
        buffer_list = list(_QQ_GROUP_BUFFER)
        image_blocks: list[dict] = []
        for b in buffer_list[-5:]:
            for url in b.get("image_urls") or []:
                try:
                    coro = napcat_api.download_file(url)
                    image_bytes = asyncio.run_coroutine_threadsafe(coro, _cafe_svc._loop).result(timeout=30)
                    if image_bytes[:8] == b'\x89PNG\r\n\x1a\n':
                        _mime = "image/png"
                    elif image_bytes[:4] == b'RIFF' and image_bytes[8:12] == b'WEBP':
                        _mime = "image/webp"
                    elif image_bytes[:3] == b'GIF':
                        _mime = "image/gif"
                    else:
                        _mime = "image/jpeg"
                    image_bytes, _mime = compress_image_if_needed(image_bytes, _mime)
                    b64 = base64.b64encode(image_bytes).decode("ascii")
                    image_blocks.append({
                        "type": "image",
                        "source": {"type": "base64", "media_type": _mime, "data": b64},
                    })
                except Exception as exc:
                    logger.warning("[qq_group] image download failed: %s", exc)

        text_prefix = (
            f"{header}\n\n"
            f"最近群消息：\n{context}\n"
            f"→ {sender_name}: {at_text}  ← 这条@了你"
        )
        if image_blocks:
            trigger_content: Any = [
                {"type": "text", "text": text_prefix},
                *image_blocks,
                {"type": "text", "text": "（上方图片为最近 5 条内带图的消息）"},
            ]
        else:
            trigger_content = text_prefix

        messages = _load_session_messages(db, session.id)
        msgs_copy = [*messages, {"role": "user", "content": trigger_content, "id": -1}]

        chat_service = ChatService(db, "助手A", assistant_id=QQ_ASSISTANT_ID, source="qq_group")
        chat_service.api_timeout = 60
        chat_service.tts_emotion_enabled = True  # inject <voice_message> rules into system prompt
        chat_service.recall_query_override = at_text
        # Stop generating more rounds once the model calls qq_group_chat.send
        chat_service._stop_after_tool_actions = {"qq_group_chat:send"}
        # Stash the quote target so the tool can reply-quote the @'d message
        chat_service._qq_group_reply_to = quote_msg_id

        for _ in chat_service.stream_chat_completion(session.id, msgs_copy, source="qq_group"):
            pass

        logger.info("[qq_group] @mention handled (send via tool)")
    except Exception as e:
        logger.error("[qq_group] Generate reply error: %s", e, exc_info=True)
    finally:
        _gen_lock.release()
        db.close()


# ── Tool executor entry for qq_group_chat ────────────────────────────────────

async def _send_group_one_part(part: str) -> int | None:
    """Send a single part (already split by [NEXT]), handling any [[voice:EMOTION]] tags.
    When TTS succeeds, we do NOT re-send the text (QQ client's voice-to-text covers it)."""
    from app.services.tts_service import EMOTION_TAG_RE, resolve_emotion, synthesize

    segments = EMOTION_TAG_RE.split(part)
    last_msg_id: int | None = None

    if len(segments) == 1:
        clean = part.strip()
        if clean:
            last_msg_id = await napcat_api.send_group_msg(QQ_GROUP_ID, clean)
        return last_msg_id

    for idx in range(0, len(segments), 2):
        seg_text = segments[idx].strip()
        if not seg_text:
            continue
        if idx == 0:
            last_msg_id = await napcat_api.send_group_msg(QQ_GROUP_ID, seg_text)
        else:
            emotion = resolve_emotion(segments[idx - 1])
            voice_sent = False
            if emotion and len(seg_text) <= 300:
                try:
                    audio_bytes = await asyncio.to_thread(synthesize, seg_text, emotion)
                    if audio_bytes:
                        last_msg_id = await napcat_api.send_group_voice(QQ_GROUP_ID, audio_bytes)
                        voice_sent = True
                        logger.info("[qq_group voice] Sent voice (emotion=%s, %d chars)", emotion, len(seg_text))
                except Exception as e:
                    logger.warning("[qq_group voice] Voice send failed, falling back to text: %s", e)
            if not voice_sent:
                last_msg_id = await napcat_api.send_group_msg(QQ_GROUP_ID, seg_text)
    return last_msg_id


async def _send_group_with_voice(text: str) -> int | None:
    """Split text by [NEXT] into parts, then each part handles its own voice tags."""
    parts = [p.strip() for p in text.replace("[NEXT]", "\n\n").split("\n\n") if p.strip()]
    if not parts:
        return None
    last_msg_id: int | None = None
    for part in parts:
        mid = await _send_group_one_part(part)
        if mid:
            last_msg_id = mid
    return last_msg_id


def _persist_qq_group_reply(text: str) -> None:
    """Create a visible assistant message for the QQ group reply (parallels cafe's _persist_cafe_reply).
    ChatService's tool-call assistant message has empty content, so UI filters miss it."""
    try:
        from app.database import SessionLocal
        from app.models.models import ChatSession, Message as MessageModel
        db = SessionLocal()
        try:
            session = (
                db.query(ChatSession)
                .filter(ChatSession.assistant_id == QQ_ASSISTANT_ID)
                .order_by(ChatSession.updated_at.desc())
                .first()
            )
            if session:
                msg = MessageModel(
                    session_id=session.id,
                    role="assistant",
                    content=f"[QQ群回复] {text}",
                    meta_info={"source": "qq_group", "qq_group_reply": True},
                )
                db.add(msg)
                db.commit()
                logger.info("[qq_group] Persisted reply message %d", msg.id)
        finally:
            db.close()
    except Exception as e:
        logger.warning("[qq_group] Failed to persist reply: %s", e)


def execute_qq_group_chat(arguments: dict) -> dict:
    """Tool executor entry — supports [[voice:EMOTION]] voice tags.
    Runs on cafe_service's background loop (same pattern as cafe_chat)."""
    action = arguments.get("action", "")
    if action != "send":
        return {"status": "error", "message": f"未知 action: {action}"}
    text = (arguments.get("text") or "").strip()
    if not text:
        return {"status": "error", "message": "text is required"}
    try:
        from app.services.cafe_service import cafe_service
        coro = _send_group_with_voice(text)
        future = asyncio.run_coroutine_threadsafe(coro, cafe_service._loop)
        msg_id = future.result(timeout=30)
        _persist_qq_group_reply(text)
        return {"status": "ok", "message_id": msg_id}
    except Exception as e:
        logger.error("[qq_group] send via tool failed: %s", e)
        return {"status": "error", "message": str(e)}
