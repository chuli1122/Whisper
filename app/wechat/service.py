"""
WeChat Bot service layer — bridges WeChat message handling with ChatService.
Mirrors app/qq/service.py but adapted for iLink protocol.
"""
from __future__ import annotations

import asyncio
import json as _json
import logging
import random
import re
from typing import Any, Callable

from app.cot_broadcaster import publish_messages_updated
from app.database import SessionLocal
from app.models.models import Message, Settings
from app.telegram.service import (
    encode_photo_base64,
    get_buffer_seconds,
    get_chat_mode,
    get_session_info,
)

logger = logging.getLogger(__name__)

# Module-level reference to the ILinkAPI instance (set by poller.start_polling)
_ilink_api = None


def set_api(api) -> None:
    """Called by poller to inject the ILinkAPI instance."""
    global _ilink_api
    _ilink_api = api


# ── Chat completion (WeChat version) ─────────────────────────────────────────

def _chat_completion_sync(
    session_id: int,
    assistant_name: str,
    message: str,
    short_mode: bool,
    assistant_id: int | None = None,
    wechat_message_ids: list[str] | None = None,
    on_intermediate: Callable[[str, int], None] | None = None,
) -> list[dict[str, Any]]:
    from app.routers.chat import _load_session_messages
    from app.services.chat_service import ChatService
    from app.services.generation_coordinator import GenerationLock

    db = SessionLocal()
    try:
        # Store user messages — one row per WeChat message (like QQ/TG)
        parts = message.split("\n") if wechat_message_ids and len(wechat_message_ids) > 1 else [message]
        ids = wechat_message_ids or []

        # Only add [微信私聊] header on source switch (matches TG's pattern — WeChat
        # doesn't rapid-fire packs like QQ does, so header per-batch would be noise)
        last_user = (
            db.query(Message)
            .filter(Message.session_id == session_id, Message.role == "user")
            .order_by(Message.id.desc())
            .first()
        )
        last_source = (last_user.meta_info or {}).get("source") if last_user else None
        needs_wechat_header = last_source != "wechat"

        max_id_before = 0
        first_stored = True
        for i, part in enumerate(parts):
            part_text = part.strip()
            if not part_text:
                continue
            wx_id = [ids[i]] if i < len(ids) else None
            if first_stored and needs_wechat_header:
                wrapped = f"[微信私聊]\n{part_text}"
            else:
                wrapped = part_text
            first_stored = False
            user_msg = Message(
                session_id=session_id,
                role="user",
                content=wrapped,
                meta_info={"source": "wechat"},
                wechat_message_id=wx_id,
            )
            db.add(user_msg)
            db.commit()
            db.refresh(user_msg)
            publish_messages_updated(session_id, user_msg.id)
            from app.services.proactive_service import touch_last_user_message_at
            touch_last_user_message_at(db)
            max_id_before = user_msg.id

        with GenerationLock("wechat") as _gen_lock:
            chat_service = ChatService(db, assistant_name, assistant_id=assistant_id, source="wechat")
            # Enable TTS emotion if master switch is on
            _voice_on = db.query(Settings).filter(Settings.key == "proactive_voice_enabled").first()
            if _voice_on and _voice_on.value == "true":
                _tts_key = db.query(Settings).filter(Settings.key == "tts_api_key").first()
                if _tts_key and _tts_key.value:
                    chat_service.tts_emotion_enabled = True

            messages = _load_session_messages(db, session_id)
            sent_db_ids: set[int] = set()
            for event_str in chat_service.stream_chat_completion(
                session_id, messages, short_mode=short_mode, source="wechat",
            ):
                if on_intermediate and event_str.startswith("data: "):
                    try:
                        data = _json.loads(event_str[6:].strip())
                        if isinstance(data, dict) and "intermediate" in data:
                            im = data["intermediate"]
                            if not getattr(chat_service, "_switched_channel", None):
                                on_intermediate(im["content"], im["db_id"])
                                sent_db_ids.add(im["db_id"])
                    except (ValueError, KeyError, TypeError):
                        pass

            _gen_lock.release()

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
            result = [
                {"role": "assistant", "content": m.content, "db_id": m.id}
                for m in new_msgs
                if not (m.meta_info or {}).get("no_message") and m.id not in sent_db_ids
            ]
            switched = getattr(chat_service, "_switched_channel", None)
            if switched:
                for r in result:
                    r["_switched_channel"] = switched
        return result
    finally:
        db.close()


async def call_chat_completion(
    session_id: int,
    assistant_name: str,
    message: str,
    short_mode: bool,
    assistant_id: int | None = None,
    wechat_message_ids: list[str] | None = None,
    user_id: str | None = None,
    context_token: str | None = None,
) -> list[dict[str, Any]]:
    on_intermediate = None
    if user_id and context_token:
        loop = asyncio.get_running_loop()
        _uid = user_id
        _ctx = context_token

        def _on_intermediate(content: str, db_id: int) -> None:
            fut = asyncio.run_coroutine_threadsafe(
                send_reply(_uid, content, _ctx), loop,
            )
            try:
                fut.result(timeout=60)
            except Exception:
                logger.warning("[wechat] Failed to send intermediate message db_id=%d", db_id)
        on_intermediate = _on_intermediate
    return await asyncio.to_thread(
        _chat_completion_sync, session_id, assistant_name, message, short_mode,
        assistant_id, wechat_message_ids, on_intermediate,
    )


# ── Store message only (no chat completion) ──────────────────────────────────

def _store_message_only_sync(
    session_id: int,
    content: str,
    image_data: str | None = None,
    wechat_message_id: list[str] | None = None,
) -> int:
    db = SessionLocal()
    try:
        msg = Message(
            session_id=session_id,
            role="user",
            content=content,
            meta_info={"source": "wechat"},
            image_data=image_data,
            wechat_message_id=wechat_message_id,
        )
        db.add(msg)
        db.commit()
        db.refresh(msg)
        publish_messages_updated(session_id, msg.id)
        return msg.id
    finally:
        db.close()


async def store_message_only(
    session_id: int,
    content: str,
    image_data: str | None = None,
    wechat_message_id: list[str] | None = None,
) -> int:
    return await asyncio.to_thread(_store_message_only_sync, session_id, content, image_data, wechat_message_id)


# ── WeChat message ID update ────────────────────────────────────────────────

def _update_wx_msg_id_sync(message_db_id: int, context_token: str) -> None:
    db = SessionLocal()
    try:
        msg = db.get(Message, message_db_id)
        if msg:
            msg.wechat_message_id = [context_token]
            db.commit()
    finally:
        db.close()


async def update_wechat_message_id(message_db_id: int, context_token: str) -> None:
    return await asyncio.to_thread(_update_wx_msg_id_sync, message_db_id, context_token)


# ── Send reply ───────────────────────────────────────────────────────────────

def _typing_delay(text: str) -> float:
    """Simulate natural typing delay based on message length."""
    base = random.uniform(2.0, 3.0)
    length_bonus = min(len(text) * 0.05, 2.5)
    return base + length_bonus


async def send_reply(user_id: str, text: str, context_token: str | None = None) -> None:
    """Send reply to WeChat user, splitting by [NEXT].

    If context_token is not provided, uses the last known context_token
    from the _last_context_tokens dict (set by handlers on message receive).
    """
    if not text.strip():
        return
    if not _ilink_api:
        logger.error("[wechat] ILinkAPI not initialized, cannot send reply")
        return

    # Resolve context_token
    if not context_token:
        context_token = _last_context_tokens.get(user_id, "")
    if not context_token:
        logger.error("[wechat] No context_token for user %s, cannot send", user_id)
        return

    # Clean up leaked metadata
    text = re.sub(r'\[THINK\].*?(?:\[/THINK\]|</THINK>|</thinking>|$)', '', text, flags=re.DOTALL)
    text = re.sub(r'\[#\s*\d+\s*\]\s*', '', text)
    text = re.sub(r'\[\[used:[\d,\s]+\]\]', '', text)
    text = re.sub(r'\(来源:\s*\w+\)\s*$', '', text, flags=re.MULTILINE)

    # Split by [NEXT] and empty lines into separate messages
    text = text.replace("[NEXT]", "\n\n")
    parts = [p.strip() for p in re.split(r'\n\s*\n', text) if p.strip()]

    for i, part in enumerate(parts):
        if i > 0:
            await asyncio.sleep(_typing_delay(part))
        await _send_one_part(user_id, part, context_token)


async def _send_one_part(user_id: str, text: str, context_token: str) -> None:
    """Send a single part, handling [[voice:EMOTION]] tags.

    Voice segments are synthesized via TTS and sent as file attachments.
    Text segments are sent as normal text messages.
    """
    from app.services.tts_service import EMOTION_TAG_RE, VALID_EMOTIONS, synthesize

    segments = EMOTION_TAG_RE.split(text)

    # No voice tag → plain text
    if len(segments) == 1:
        clean = text.strip()
        if clean:
            try:
                await _ilink_api.send_text(user_id, clean, context_token)
            except Exception as e:
                logger.error("[wechat] Failed to send text to %s: %s", user_id, e)
        return

    for idx in range(0, len(segments), 2):
        seg_text = segments[idx].strip()
        if not seg_text:
            continue

        if idx == 0:
            # Text before the first voice tag
            try:
                await _ilink_api.send_text(user_id, seg_text, context_token)
            except Exception as e:
                logger.error("[wechat] Failed to send text to %s: %s", user_id, e)
        else:
            from app.services.tts_service import resolve_emotion
            emotion = resolve_emotion(segments[idx - 1])
            lines = seg_text.split("\n", 1)
            voice_line = lines[0].strip()
            rest_text = lines[1].strip() if len(lines) > 1 else ""

            voice_sent = False
            if voice_line and emotion and len(voice_line) <= 300:
                try:
                    audio_bytes = await asyncio.to_thread(synthesize, voice_line, emotion)
                    if audio_bytes:
                        await _ilink_api.send_file(user_id, audio_bytes, "voice.mp3", context_token)
                        voice_sent = True
                        logger.info("[wechat] Sent voice file (emotion=%s, %d chars)", emotion, len(voice_line))
                except Exception as e:
                    logger.warning("[wechat] Voice send failed: %s", e)

            if voice_line and not voice_sent:
                try:
                    await _ilink_api.send_text(user_id, voice_line, context_token)
                except Exception as e:
                    logger.error("[wechat] Failed to send text to %s: %s", user_id, e)

            if rest_text:
                await asyncio.sleep(_typing_delay(rest_text))
                try:
                    await _ilink_api.send_text(user_id, rest_text, context_token)
                except Exception as e:
                    logger.error("[wechat] Failed to send text to %s: %s", user_id, e)


# Track last context_token per user (updated by handlers on each incoming message)
_last_context_tokens: dict[str, str] = {}


def record_context_token(user_id: str, context_token: str) -> None:
    """Called by handlers to record the latest context_token for a user."""
    _last_context_tokens[user_id] = context_token
