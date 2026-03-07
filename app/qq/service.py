"""
QQ Bot service layer — bridges QQ message handling with ChatService.
Mirrors app/telegram/service.py but adapted for OneBot/NapCat.
"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

from app.database import SessionLocal
from app.models.models import Message, Settings
from app.telegram.service import (
    encode_photo_base64,
    get_buffer_seconds,
    get_chat_mode,
    get_session_info,
)

from . import napcat_api

logger = logging.getLogger(__name__)


# ── Chat completion (QQ version) ─────────────────────────────────────────────

def _chat_completion_sync(
    session_id: int,
    assistant_name: str,
    message: str,
    short_mode: bool,
    assistant_id: int | None = None,
    qq_message_ids: list[int] | None = None,
) -> list[dict[str, Any]]:
    from app.routers.chat import _load_session_messages
    from app.services.chat_service import ChatService

    db = SessionLocal()
    try:
        # Store user messages — one row per QQ message (like TG)
        parts = message.split("\n") if qq_message_ids and len(qq_message_ids) > 1 else [message]
        ids = qq_message_ids or []

        max_id_before = 0
        for i, part in enumerate(parts):
            part_text = part.strip()
            if not part_text:
                continue
            qq_id = [ids[i]] if i < len(ids) else None
            user_msg = Message(
                session_id=session_id,
                role="user",
                content=part_text,
                meta_info={"source": "qq"},
                qq_message_id=qq_id,
            )
            db.add(user_msg)
            db.commit()
            db.refresh(user_msg)
            max_id_before = user_msg.id

        chat_service = ChatService(db, assistant_name, assistant_id=assistant_id, source="qq")
        # Enable TTS emotion if master switch is on
        _voice_on = db.query(Settings).filter(Settings.key == "proactive_voice_enabled").first()
        if _voice_on and _voice_on.value == "true":
            _tts_key = db.query(Settings).filter(Settings.key == "tts_api_key").first()
            if _tts_key and _tts_key.value:
                chat_service.tts_emotion_enabled = True

        messages = _load_session_messages(db, session_id)
        for _ in chat_service.stream_chat_completion(
            session_id, messages, short_mode=short_mode, source="qq",
        ):
            pass

        new_msgs = (
            db.query(Message)
            .filter(
                Message.session_id == session_id,
                Message.id > max_id_before,
                Message.role == "assistant",
                Message.content != "",
            )
            .order_by(Message.id)
            .all()
        )
        return [{"role": "assistant", "content": m.content, "db_id": m.id} for m in new_msgs]
    finally:
        db.close()


async def call_chat_completion(
    session_id: int,
    assistant_name: str,
    message: str,
    short_mode: bool,
    assistant_id: int | None = None,
    qq_message_ids: list[int] | None = None,
) -> list[dict[str, Any]]:
    return await asyncio.to_thread(
        _chat_completion_sync, session_id, assistant_name, message, short_mode,
        assistant_id, qq_message_ids,
    )


# ── Chat completion with image ───────────────────────────────────────────────

def _chat_completion_with_image_sync(
    session_id: int,
    assistant_name: str,
    message: str,
    image_data: str | None = None,
    short_mode: bool = False,
    assistant_id: int | None = None,
) -> list[dict[str, Any]]:
    from app.routers.chat import _load_session_messages
    from app.services.chat_service import ChatService

    db = SessionLocal()
    try:
        user_msg = Message(
            session_id=session_id,
            role="user",
            content=message,
            meta_info={"source": "qq"},
            image_data=image_data,
        )
        db.add(user_msg)
        db.commit()
        db.refresh(user_msg)
        max_id_before = user_msg.id

        chat_service = ChatService(db, assistant_name, assistant_id=assistant_id, source="qq")
        messages = _load_session_messages(db, session_id)
        for _ in chat_service.stream_chat_completion(
            session_id, messages, short_mode=short_mode, source="qq",
        ):
            pass

        new_msgs = (
            db.query(Message)
            .filter(
                Message.session_id == session_id,
                Message.id > max_id_before,
                Message.role == "assistant",
                Message.content != "",
            )
            .order_by(Message.id)
            .all()
        )
        return [{"role": "assistant", "content": m.content, "db_id": m.id} for m in new_msgs]
    finally:
        db.close()


async def call_chat_completion_with_image(
    session_id: int,
    assistant_name: str,
    message: str,
    image_data: str | None = None,
    short_mode: bool = False,
    assistant_id: int | None = None,
) -> list[dict[str, Any]]:
    return await asyncio.to_thread(
        _chat_completion_with_image_sync,
        session_id, assistant_name, message, image_data, short_mode, assistant_id,
    )


# ── Store message only (no chat completion) ──────────────────────────────────

def _store_message_only_sync(
    session_id: int,
    content: str,
    image_data: str | None = None,
    qq_message_id: list[int] | None = None,
) -> int:
    db = SessionLocal()
    try:
        msg = Message(
            session_id=session_id,
            role="user",
            content=content,
            meta_info={"source": "qq"},
            image_data=image_data,
            qq_message_id=qq_message_id,
        )
        db.add(msg)
        db.commit()
        db.refresh(msg)
        return msg.id
    finally:
        db.close()


async def store_message_only(
    session_id: int,
    content: str,
    image_data: str | None = None,
    qq_message_id: list[int] | None = None,
) -> int:
    return await asyncio.to_thread(_store_message_only_sync, session_id, content, image_data, qq_message_id)


# ── QQ message ID lookup / update ────────────────────────────────────────────

def _lookup_by_qq_id_sync(qq_msg_id: int) -> dict[str, Any] | None:
    from sqlalchemy import cast
    from sqlalchemy.dialects.postgresql import JSONB as JSONB_TYPE

    db = SessionLocal()
    try:
        msg = (
            db.query(Message)
            .filter(Message.qq_message_id.op("@>")(cast([qq_msg_id], JSONB_TYPE)))
            .first()
        )
        if msg:
            return {"id": msg.id, "content": msg.content, "role": msg.role}
        return None
    finally:
        db.close()


async def lookup_by_qq_message_id(qq_msg_id: int) -> dict[str, Any] | None:
    return await asyncio.to_thread(_lookup_by_qq_id_sync, qq_msg_id)


def _update_qq_msg_id_sync(message_db_id: int, qq_msg_id: int) -> None:
    db = SessionLocal()
    try:
        msg = db.get(Message, message_db_id)
        if msg:
            msg.qq_message_id = [qq_msg_id]
            db.commit()
    finally:
        db.close()


async def update_qq_message_id(message_db_id: int, qq_msg_id: int) -> None:
    return await asyncio.to_thread(_update_qq_msg_id_sync, message_db_id, qq_msg_id)


# ── Send reply with optional TTS voice ───────────────────────────────────────

def _typing_delay(text: str) -> float:
    """Simulate natural typing delay based on message length."""
    base = random.uniform(2.5, 4.0)
    length_bonus = min(len(text) * 0.06, 3.0)
    return base + length_bonus


async def _send_one_part(user_id: int, text: str, *, explicit_block: bool = False) -> int | None:
    """Send a single part, handling [[voice:EMOTION]] tags. Returns last sent qq message_id."""
    from app.services.tts_service import EMOTION_TAG_RE, VALID_EMOTIONS, synthesize

    segments = EMOTION_TAG_RE.split(text)
    last_msg_id: int | None = None

    # No voice tag → plain text
    if len(segments) == 1:
        clean = text.strip()
        if clean:
            last_msg_id = await napcat_api.send_private_msg(user_id, clean)
        return last_msg_id

    sent_something = False
    for idx in range(0, len(segments), 2):
        seg_text = segments[idx].strip()
        if not seg_text:
            continue

        if sent_something:
            await asyncio.sleep(_typing_delay(seg_text))

        if idx == 0:
            # Text before the first voice tag
            last_msg_id = await napcat_api.send_private_msg(user_id, seg_text)
            sent_something = True
        else:
            emotion = segments[idx - 1].lower()
            if emotion not in VALID_EMOTIONS:
                emotion = None

            if explicit_block:
                voice_line = seg_text
                rest_text = ""
            else:
                lines = seg_text.split("\n", 1)
                voice_line = lines[0].strip()
                rest_text = lines[1].strip() if len(lines) > 1 else ""

            voice_sent = False
            if voice_line and emotion and len(voice_line) <= 300:
                try:
                    audio_bytes = await asyncio.to_thread(synthesize, voice_line, emotion)
                    if audio_bytes:
                        await napcat_api.send_private_voice(user_id, audio_bytes)
                        voice_sent = True
                        logger.info("[qq voice] Sent voice (emotion=%s, %d chars)", emotion, len(voice_line))
                except Exception as e:
                    logger.warning("[qq voice] Voice send failed: %s", e)

            if voice_line and not voice_sent:
                last_msg_id = await napcat_api.send_private_msg(user_id, voice_line)
                sent_something = True
            elif voice_sent:
                sent_something = True

            if rest_text:
                if sent_something:
                    await asyncio.sleep(_typing_delay(rest_text))
                last_msg_id = await napcat_api.send_private_msg(user_id, rest_text)
                sent_something = True

    return last_msg_id


async def send_reply_with_voice(user_id: int, text: str) -> int | None:
    """Send reply, splitting by [NEXT]. Each part independently handles voice tags.
    Returns the last sent qq message_id (for mapping back to DB)."""
    if not text.strip():
        return None

    # Every newline and [NEXT] splits into separate messages
    import re
    text = text.replace("[NEXT]", "\n")
    parts = [p.strip() for p in text.split("\n") if p.strip()]
    has_next = len(parts) > 1
    last_msg_id: int | None = None

    for i, part in enumerate(parts):
        if i > 0:
            await asyncio.sleep(_typing_delay(part))
        mid = await _send_one_part(user_id, part, explicit_block=has_next)
        if mid is not None:
            last_msg_id = mid

    return last_msg_id
