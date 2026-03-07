"""
QQ message handlers — mirrors app/telegram/handlers.py for OneBot v11.
QQ is always in short message mode (buffered).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from .config import QQ_ALLOWED_USER_IDS, QQ_ASSISTANT_ID
from . import napcat_api
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
        "[环境切换] 当前平台：Telegram（长消息模式）。\n"
        "注意：从这条消息开始按以下要求输出，不要模仿上文的风格。\n"
        "要求：完整段落输出，不拆条，不使用[NEXT]，每次回复至少3段，"
        "说话用双引号包裹（如\"我想你了。\"），动作描写和语言自然穿插交织在同一段内，段落要有体量。"
        "回复正文中一律使用第二人称\"你\"称呼对方，不许用\"她\"。"
    ),
    "qq": (
        "[环境切换] 当前平台：QQ（短消息模式）。\n"
        "注意：从这条消息开始按以下要求输出，不要模仿上文的风格。\n"
        "要求：像真人发微信一样自然回复，用[NEXT]拆条，不使用空行分段，不使用动作描写。"
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

        # Platform changed (or no source history) → insert mode switch system message
        need_switch = old_source is None or old_source != "qq"
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

    # Only private messages
    if event.get("message_type") != "private":
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

    # Handle reply/quote — look up the quoted message by qq_message_id
    if reply_qq_id:
        quoted = await lookup_by_qq_message_id(reply_qq_id)
        if quoted:
            quote_prefix = f"[引用消息 id={quoted['id']}] {quoted['content']}"
            text_parts.insert(0, quote_prefix)

    # ── Voice message ──
    if voice_url:
        await _handle_voice(user_id, msg_id, voice_url)
        return

    # ── Image message ──
    if image_url:
        caption = " ".join(text_parts) if text_parts else ""
        await _handle_image(user_id, msg_id, image_url, caption)
        return

    # ── Text message ──
    combined = " ".join(text_parts)
    if combined:
        await _handle_text(user_id, msg_id, combined)


# ── Text (always short mode — buffer) ────────────────────────────────────────

async def _handle_text(user_id: int, msg_id: int, text: str) -> None:
    delay = await get_buffer_seconds()
    buf = _buffers.setdefault(user_id, _ChatBuffer())
    buf.messages.append(text)
    buf.message_ids.append(msg_id)
    if buf.timer_task and not buf.timer_task.done():
        buf.timer_task.cancel()
    buf.timer_task = asyncio.create_task(_buffer_fire(user_id, delay))


async def _buffer_fire(user_id: int, delay: float) -> None:
    await asyncio.sleep(delay)
    buf = _buffers.get(user_id)
    if not buf or not buf.messages:
        return
    combined = "\n".join(buf.messages)
    qq_ids = list(buf.message_ids) if buf.message_ids else None
    buf.messages.clear()
    buf.message_ids.clear()
    buf.timer_task = None
    await _process_request(user_id, combined, is_short=True, qq_message_ids=qq_ids)


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
        )
        for i, msg in enumerate(result_messages):
            content = (msg.get("content") or "").strip()
            if not content:
                continue
            if i > 0:
                from app.qq.service import _typing_delay
                await asyncio.sleep(_typing_delay(content))
            sent_qq_id = await send_reply_with_voice(user_id, content)
            # Write back qq_message_id to the assistant message in DB
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
    buf.messages.append(text)
    buf.message_ids.append(msg_id)
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

    image_data = encode_photo_base64(image_bytes, "image/jpeg")

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
