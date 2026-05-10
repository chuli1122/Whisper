from __future__ import annotations

import asyncio
import base64
import json as _json
import logging
from datetime import datetime, timedelta, timezone
from io import BytesIO
from typing import Any, Callable

from app.cot_broadcaster import publish_messages_updated
from app.database import SessionLocal
from app.models.models import Assistant, ChatSession, Message, Settings

_TZ_EAST8 = timezone(timedelta(hours=8))

logger = logging.getLogger(__name__)

# Sources that should not be re-sent by channel handlers (they have their own delivery)
_exclude_sources = {"proactive", "cafe", "reflection"}


# ── Settings helpers ──────────────────────────────────────────────────────────

def _get_setting_sync(key: str, default: str = "") -> str:
    db = SessionLocal()
    try:
        row = db.query(Settings).filter(Settings.key == key).first()
        return row.value if row else default
    finally:
        db.close()


async def get_setting(key: str, default: str = "") -> str:
    return await asyncio.to_thread(_get_setting_sync, key, default)


# ── Session / Assistant lookup ────────────────────────────────────────────────

def _get_session_info_sync(assistant_id: int) -> tuple[int, str]:
    """
    Returns (session_id, assistant_name) for the given assistant.
    Finds the most recently updated session belonging to that assistant.
    """
    db = SessionLocal()
    try:
        # Look up assistant name
        assistant = db.get(Assistant, assistant_id)
        name = assistant.name if assistant else "unknown"

        # Find most recent session for this assistant
        session = (
            db.query(ChatSession)
            .filter(ChatSession.assistant_id == assistant_id)
            .order_by(ChatSession.updated_at.desc())
            .first()
        )
        if session:
            return session.id, name

        # No session exists — fall back to any recent session
        session = (
            db.query(ChatSession)
            .order_by(ChatSession.updated_at.desc())
            .first()
        )
        if session:
            logger.warning(
                "No session for assistant_id=%d, falling back to session %d",
                assistant_id, session.id,
            )
            return session.id, name

        logger.warning("No chat session found; creating one for assistant_id=%d", assistant_id)
        new_session = ChatSession(
            assistant_id=assistant_id,
            title="Telegram",
            type="chat",
        )
        db.add(new_session)
        db.commit()
        db.refresh(new_session)
        return new_session.id, name
    finally:
        db.close()


async def get_session_info(assistant_id: int) -> tuple[int, str]:
    return await asyncio.to_thread(_get_session_info_sync, assistant_id)


# ── Chat completion ───────────────────────────────────────────────────────────

def _chat_completion_sync(
    session_id: int,
    assistant_name: str,
    message: str,
    short_mode: bool,
    telegram_message_id: list[int] | None = None,
    assistant_id: int | None = None,
    on_intermediate: Callable[[str, int], None] | None = None,
    on_thinking_chunk: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    """
    Synchronous wrapper: pre-stores user message (with telegram_message_id),
    loads history, calls ChatService, returns assistant messages with db_id.
    """
    from app.routers.chat import _load_session_messages
    from app.services.chat_service import ChatService
    from app.services.generation_coordinator import GenerationLock

    db = SessionLocal()
    try:
        # 1. Pre-store user messages — one row per telegram message
        #    When buffer merges multiple messages, split them back out
        parts = message.split("\n") if telegram_message_id and len(telegram_message_id) > 1 else [message]
        tg_ids = telegram_message_id or []

        # TG 私聊只在 source 切换到 TG 时加 [TG私聊] 头；timestamp 由前端日志页渲染 created_at，不重复注入
        last_user = (
            db.query(Message)
            .filter(Message.session_id == session_id, Message.role == "user")
            .order_by(Message.id.desc())
            .first()
        )
        last_source = (last_user.meta_info or {}).get("source") if last_user else None
        needs_tg_header = last_source != "telegram"

        max_id_before = 0
        first_stored = True
        for i, part in enumerate(parts):
            part_text = part.strip()
            if not part_text:
                continue
            tg_id_for_part = [tg_ids[i]] if i < len(tg_ids) else None
            if first_stored and needs_tg_header:
                wrapped = f"[TG私聊]\n{part_text}"
            else:
                wrapped = part_text
            first_stored = False
            user_msg = Message(
                session_id=session_id,
                role="user",
                content=wrapped,
                meta_info={"source": "telegram"},
                telegram_message_id=tg_id_for_part,
            )
            db.add(user_msg)
            db.commit()
            db.refresh(user_msg)
            publish_messages_updated(session_id, user_msg.id)
            from app.services.proactive_service import touch_last_user_message_at
            touch_last_user_message_at(db)
            max_id_before = user_msg.id

        # Serialize actual model-response generation against other channels
        # (cafe / qq / qq_group). Pre-store above is safe in parallel because
        # messages get a monotonically increasing id either way.
        with GenerationLock("telegram") as _gen_lock:
            # 2. Load history (includes the message we just stored, all have 'id')
            chat_service = ChatService(db, assistant_name, assistant_id=assistant_id, source="telegram")
            # Enable voice if master switch is on and TTS is configured
            from app.models.models import Settings as SettingsModel
            _voice_on = db.query(SettingsModel).filter(SettingsModel.key == "proactive_voice_enabled").first()
            if _voice_on and _voice_on.value == "true":
                _tts_key = db.query(SettingsModel).filter(SettingsModel.key == "tts_api_key").first()
                if _tts_key and _tts_key.value:
                    chat_service.tts_emotion_enabled = True
            messages = _load_session_messages(db, session_id)

            # 3. Call stream_chat_completion — parse SSE events
            sent_db_ids: set[int] = set()
            for event_str in chat_service.stream_chat_completion(
                session_id, messages, short_mode=short_mode, source="telegram",
            ):
                if not event_str.startswith("data: "):
                    continue
                try:
                    data = _json.loads(event_str[6:].strip())
                except (ValueError, TypeError):
                    continue
                if not isinstance(data, dict):
                    continue
                if "intermediate" in data and on_intermediate:
                    im = data["intermediate"]
                    if not getattr(chat_service, "_switched_channel", None):
                        on_intermediate(im["content"], im["db_id"])
                        sent_db_ids.add(im["db_id"])
                elif "thinking" in data and on_thinking_chunk:
                    try:
                        on_thinking_chunk(data["thinking"])
                    except Exception:
                        logger.warning("on_thinking_chunk failed", exc_info=True)

            # Stream done; release global lock before the post-processing so the
            # next queued source can start generating while this handler finishes sending.
            _gen_lock.release()

            # 4. Find NEW assistant messages created during this call
            new_assistant_msgs = (
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

            # 5. Build result with db_ids (skip NO_MESSAGE, already-sent intermediates,
            #    and messages from other sources like proactive/cafe to avoid double delivery)
            result = [
                {"role": "assistant", "content": m.content, "db_id": m.id}
                for m in new_assistant_msgs
                if not (m.meta_info or {}).get("no_message")
                and m.id not in sent_db_ids
                and (m.meta_info or {}).get("source") not in _exclude_sources
            ]
            # Attach switched channel info if model called switch_channel
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
    telegram_message_id: list[int] | None = None,
    assistant_id: int | None = None,
    chat_id: int | None = None,
    bot: Any = None,
    on_thinking_chunk: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    on_intermediate = None
    if chat_id and bot:
        loop = asyncio.get_running_loop()
        def _on_intermediate(content: str, db_id: int) -> None:
            # Route through _send_reply_with_voice to split by [NEXT] and handle
            # voice tags — bot.send_message direct call would leave [NEXT] literal
            # visible in TG and ignore voice tags.
            from app.telegram.handlers import _send_reply_with_voice
            fut = asyncio.run_coroutine_threadsafe(
                _send_reply_with_voice(bot, chat_id, content), loop,
            )
            try:
                fut.result(timeout=60)
            except Exception:
                logger.warning("[tg] Failed to send intermediate message db_id=%d", db_id)
        on_intermediate = _on_intermediate
    return await asyncio.to_thread(
        _chat_completion_sync,
        session_id,
        assistant_name,
        message,
        short_mode,
        telegram_message_id,
        assistant_id,
        on_intermediate,
        on_thinking_chunk,
    )


# ── Telegram message ID helpers ──────────────────────────────────────────────

def _update_telegram_msg_id_sync(message_db_id: int, telegram_msg_id: int) -> None:
    db = SessionLocal()
    try:
        msg = db.get(Message, message_db_id)
        if msg:
            msg.telegram_message_id = [telegram_msg_id]
            db.commit()
    finally:
        db.close()


async def update_telegram_message_id(message_db_id: int, telegram_msg_id: int) -> None:
    return await asyncio.to_thread(_update_telegram_msg_id_sync, message_db_id, telegram_msg_id)


def _lookup_by_telegram_id_sync(telegram_msg_id: int) -> dict[str, Any] | None:
    from sqlalchemy import cast
    from sqlalchemy.dialects.postgresql import JSONB as JSONB_TYPE

    db = SessionLocal()
    try:
        msg = (
            db.query(Message)
            .filter(Message.telegram_message_id.op("@>")(cast([telegram_msg_id], JSONB_TYPE)))
            .first()
        )
        if msg:
            return {"id": msg.id, "content": msg.content, "role": msg.role}
        return None
    finally:
        db.close()


async def lookup_by_telegram_message_id(telegram_msg_id: int) -> dict[str, Any] | None:
    return await asyncio.to_thread(_lookup_by_telegram_id_sync, telegram_msg_id)


# ── Buffer delay ──────────────────────────────────────────────────────────────

async def get_buffer_seconds() -> float:
    raw = await get_setting("telegram_buffer_seconds", "15")
    try:
        return max(1.0, float(raw))
    except ValueError:
        return 15.0


async def get_chat_mode() -> str:
    raw = await get_setting("chat_mode", "long")
    return raw if raw in ("short", "long") else "long"


# ── Undo (delete last round) ────────────────────────────────────────────────

def _undo_last_round_sync(assistant_id: int) -> int:
    """Delete the most recent user message and all assistant/system messages after it.
    Returns the number of deleted messages."""
    db = SessionLocal()
    try:
        session_id, _ = _get_session_info_sync(assistant_id)

        # Find the latest user message
        last_user = (
            db.query(Message)
            .filter(Message.session_id == session_id, Message.role == "user")
            .order_by(Message.id.desc())
            .first()
        )
        if not last_user:
            return 0

        # Delete that user message + all messages after it (assistant replies, system, etc.)
        deleted = (
            db.query(Message)
            .filter(Message.session_id == session_id, Message.id >= last_user.id)
            .delete(synchronize_session=False)
        )
        db.commit()
        return deleted
    finally:
        db.close()


async def undo_last_round(assistant_id: int) -> int:
    return await asyncio.to_thread(_undo_last_round_sync, assistant_id)


# ── Store message without triggering chat ────────────────────────────────────

def _store_message_only_sync(
    session_id: int,
    content: str,
    meta_info: dict | None = None,
    image_data: str | None = None,
    telegram_message_id: list[int] | None = None,
) -> int:
    """Store a user message in the DB without triggering chat completion.
    Returns the message DB id."""
    db = SessionLocal()
    try:
        msg = Message(
            session_id=session_id,
            role="user",
            content=content,
            meta_info=meta_info or {},
            image_data=image_data,
            telegram_message_id=telegram_message_id,
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
    meta_info: dict | None = None,
    image_data: str | None = None,
    telegram_message_id: list[int] | None = None,
) -> int:
    return await asyncio.to_thread(
        _store_message_only_sync, session_id, content, meta_info, image_data, telegram_message_id,
    )


# ── Photo message with image_data ───────────────────────────────────────────

def _chat_completion_with_image_sync(
    session_id: int,
    assistant_name: str,
    message: str,
    image_data: str | None = None,
    short_mode: bool = False,
    telegram_message_id: list[int] | None = None,
    assistant_id: int | None = None,
    on_intermediate: Callable[[str, int], None] | None = None,
) -> list[dict[str, Any]]:
    """Like _chat_completion_sync but stores image_data alongside the user message."""
    from app.routers.chat import _load_session_messages
    from app.services.chat_service import ChatService
    from app.services.generation_coordinator import GenerationLock

    db = SessionLocal()
    try:
        # TG 私聊图片消息：source 切换到 TG 才加 [TG私聊] 头（timestamp 由前端渲染）
        last_user = (
            db.query(Message)
            .filter(Message.session_id == session_id, Message.role == "user")
            .order_by(Message.id.desc())
            .first()
        )
        last_source = (last_user.meta_info or {}).get("source") if last_user else None
        if last_source != "telegram":
            wrapped = f"[TG私聊]\n{message}"
        else:
            wrapped = message
        user_msg = Message(
            session_id=session_id,
            role="user",
            content=wrapped,
            meta_info={"source": "telegram"},
            image_data=image_data,
            telegram_message_id=telegram_message_id,
        )
        db.add(user_msg)
        db.commit()
        db.refresh(user_msg)
        publish_messages_updated(session_id, user_msg.id)
        from app.services.proactive_service import touch_last_user_message_at
        touch_last_user_message_at(db)
        max_id_before = user_msg.id

        with GenerationLock("telegram") as _gen_lock:
            chat_service = ChatService(db, assistant_name, assistant_id=assistant_id, source="telegram")
            messages = _load_session_messages(db, session_id)

            sent_db_ids: set[int] = set()
            for event_str in chat_service.stream_chat_completion(
                session_id, messages, short_mode=short_mode, source="telegram",
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

            new_assistant_msgs = (
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
                for m in new_assistant_msgs
                if m.id not in sent_db_ids
                and (m.meta_info or {}).get("source") not in _exclude_sources
            ]
            switched = getattr(chat_service, "_switched_channel", None)
            if switched:
                for r in result:
                    r["_switched_channel"] = switched
        return result
    finally:
        db.close()


async def call_chat_completion_with_image(
    session_id: int,
    assistant_name: str,
    message: str,
    image_data: str | None = None,
    short_mode: bool = False,
    telegram_message_id: list[int] | None = None,
    assistant_id: int | None = None,
    chat_id: int | None = None,
    bot: Any = None,
) -> list[dict[str, Any]]:
    on_intermediate = None
    if chat_id and bot:
        loop = asyncio.get_running_loop()
        def _on_intermediate(content: str, db_id: int) -> None:
            from app.telegram.handlers import _send_reply_with_voice
            fut = asyncio.run_coroutine_threadsafe(
                _send_reply_with_voice(bot, chat_id, content), loop,
            )
            try:
                fut.result(timeout=60)
            except Exception:
                logger.warning("[tg] Failed to send intermediate message db_id=%d", db_id)
        on_intermediate = _on_intermediate
    return await asyncio.to_thread(
        _chat_completion_with_image_sync,
        session_id, assistant_name, message, image_data, short_mode, telegram_message_id, assistant_id,
        on_intermediate,
    )


# ── File message with meta_info ─────────────────────────────────────────────

def _chat_completion_with_meta_sync(
    session_id: int,
    assistant_name: str,
    message: str,
    meta_info: dict | None = None,
    short_mode: bool = False,
    telegram_message_id: list[int] | None = None,
    assistant_id: int | None = None,
    on_intermediate: Callable[[str, int], None] | None = None,
) -> list[dict[str, Any]]:
    """Like _chat_completion_sync but stores custom meta_info."""
    from app.routers.chat import _load_session_messages
    from app.services.chat_service import ChatService

    db = SessionLocal()
    try:
        user_msg = Message(
            session_id=session_id,
            role="user",
            content=message,
            meta_info={**(meta_info or {}), "source": "telegram"},
            telegram_message_id=telegram_message_id,
        )
        db.add(user_msg)
        db.commit()
        db.refresh(user_msg)
        publish_messages_updated(session_id, user_msg.id)
        from app.services.proactive_service import touch_last_user_message_at
        touch_last_user_message_at(db)
        max_id_before = user_msg.id

        chat_service = ChatService(db, assistant_name, assistant_id=assistant_id, source="telegram")
        messages = _load_session_messages(db, session_id)

        sent_db_ids: set[int] = set()
        for event_str in chat_service.stream_chat_completion(
            session_id, messages, short_mode=short_mode, source="telegram",
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

        new_assistant_msgs = (
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
            for m in new_assistant_msgs
            if m.id not in sent_db_ids
            and (m.meta_info or {}).get("source") not in _exclude_sources
        ]
        switched = getattr(chat_service, "_switched_channel", None)
        if switched:
            for r in result:
                r["_switched_channel"] = switched
        return result
    finally:
        db.close()


async def call_chat_completion_with_meta(
    session_id: int,
    assistant_name: str,
    message: str,
    meta_info: dict | None = None,
    short_mode: bool = False,
    telegram_message_id: list[int] | None = None,
    assistant_id: int | None = None,
    chat_id: int | None = None,
    bot: Any = None,
) -> list[dict[str, Any]]:
    on_intermediate = None
    if chat_id and bot:
        loop = asyncio.get_running_loop()
        def _on_intermediate(content: str, db_id: int) -> None:
            from app.telegram.handlers import _send_reply_with_voice
            fut = asyncio.run_coroutine_threadsafe(
                _send_reply_with_voice(bot, chat_id, content), loop,
            )
            try:
                fut.result(timeout=60)
            except Exception:
                logger.warning("[tg] Failed to send intermediate message db_id=%d", db_id)
        on_intermediate = _on_intermediate
    return await asyncio.to_thread(
        _chat_completion_with_meta_sync,
        session_id, assistant_name, message, meta_info, short_mode, telegram_message_id, assistant_id,
        on_intermediate,
    )


# ── Photo/Document helpers ──────────────────────────────────────────────────

def encode_photo_base64(
    file_bytes: BytesIO | bytes,
    mime: str = "image/jpeg",
    *,
    max_long_edge: int = 1024,
    quality: int = 75,
) -> str:
    """Save photo to media directory as-is (no compression). Returns ``media:<filename>``.

    Anthropic internally resizes to 1568px, so compressing client-side wastes quality
    without saving tokens.
    """
    from app.services.media_service import save_image

    if isinstance(file_bytes, BytesIO):
        file_bytes.seek(0)
        raw = file_bytes.read()
    else:
        raw = file_bytes

    if raw[:8] == b'\x89PNG\r\n\x1a\n':
        ext = "png"
    elif raw[:2] == b'\xff\xd8':
        ext = "jpg"
    elif raw[:4] == b'RIFF' and raw[8:12] == b'WEBP':
        ext = "webp"
    elif raw[:3] == b'GIF':
        ext = "gif"
    else:
        ext = "png" if mime == "image/png" else "jpg"
    filename = save_image(raw, ext)
    return f"media:{filename}"
