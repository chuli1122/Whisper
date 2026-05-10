"""
WeChat message handlers — mirrors app/qq/handlers.py for iLink protocol.
WeChat is always in short message mode (buffered).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from .config import WECHAT_ALLOWED_USER_IDS, WECHAT_ASSISTANT_ID
from app.telegram.service import (
    get_buffer_seconds,
    get_session_info,
)
from .service import (
    call_chat_completion,
    record_context_token,
    send_reply,
    store_message_only,
    update_wechat_message_id,
)

logger = logging.getLogger(__name__)


async def _reschedule_random_proactive() -> None:
    """When user sends a message, clear the followup wakeup so the loop resets."""
    try:
        from app.telegram.service import get_setting
        if await get_setting("proactive_enabled", "false") != "true":
            return
        from app.services.proactive_service import _set_next_wakeup_sync, _notify_wakeup
        await asyncio.to_thread(_set_next_wakeup_sync, None)
        _notify_wakeup()
    except Exception:
        pass


# ── Per-user state ───────────────────────────────────────────────────────────

@dataclass
class _ChatBuffer:
    messages: list[str] = field(default_factory=list)
    context_tokens: list[str] = field(default_factory=list)
    timer_task: Optional[asyncio.Task] = None


_buffers: dict[str, _ChatBuffer] = {}
_processed_keys: list[str] = []
_DEDUP_MAX = 500


def _is_allowed(user_id: str) -> bool:
    if not WECHAT_ALLOWED_USER_IDS:
        return True  # empty set = no restriction
    return user_id in WECHAT_ALLOWED_USER_IDS


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
    "wechat": (
        "[环境切换] 当前平台：微信（短消息模式）。"
        "注意：从本条消息起严格按以下规则输出，不再沿用之前的回复风格。"
        "要求：采用日常短消息表达习惯，语气轻松自然；无动作描写，语句以逗号或空格分隔，"
        "可使用[NEXT]拆条，不使用空行分段。整体追求流畅真实的聊天质感，避免生硬书面化。"
    ),
}


def _record_last_active(user_id: str) -> None:
    """Record that user was last active on WeChat. Insert mode switch message if platform changed."""
    from app.database import SessionLocal
    from app.models.models import ChatSession, Settings
    from app.models.models import Message as MessageModel

    db = SessionLocal()
    try:
        # Update settings
        for key, value in [("last_active_source", "wechat"), ("last_active_wechat_user_id", user_id)]:
            row = db.query(Settings).filter(Settings.key == key).first()
            if row:
                row.value = value
            else:
                db.add(Settings(key=key, value=value))

        # Detect platform switch
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
                    MessageModel.meta_info["source"].astext.in_(["qq", "telegram", "wechat"]),
                )
                .order_by(MessageModel.id.desc())
                .first()
            )
            old_source = last_msg.meta_info.get("source") if last_msg else None

            # Override if model recently switched via switch_channel
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
                elif "微信" in content:
                    old_source = "wechat"

        need_switch = old_source is not None and old_source != "wechat"
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
            if recent_tool_switch and "微信" in (recent_tool_switch.content or ""):
                need_switch = False
        if need_switch:
            prompt = _PLATFORM_SWITCH_PROMPTS.get("wechat", "")
            if prompt and latest_session:
                db.add(MessageModel(
                    session_id=latest_session.id,
                    role="system",
                    content=prompt,
                    meta_info={"mode_switch": True, "source": "wechat"},
                ))

        db.commit()
        logger.info("[wechat] last_active: %s → wechat, switch_msg=%s", old_source, need_switch)
    except Exception as exc:
        logger.error("[wechat] Failed to record last_active: %s", exc, exc_info=True)
    finally:
        db.close()


# ── Main entry point (called from poller) ────────────────────────────────────

async def dispatch_message(raw_msg: dict[str, Any], api: Any) -> None:
    """Process a single iLink message."""
    from_user = raw_msg.get("from_user_id", "")
    context_token = raw_msg.get("context_token", "")
    msg_id = raw_msg.get("message_id", 0)
    items = raw_msg.get("item_list", [])

    if not from_user or not items:
        return

    # Allowlist check
    if not _is_allowed(from_user):
        return

    # Filter: only process items we understand (type 1=text, 2=image, 3=audio)
    known_items = [item for item in items if item.get("type") in (1, 2, 3)]
    if not known_items:
        logger.debug("[wechat] Skipping message %s — no known item types: %s",
                     msg_id, [item.get("type") for item in items])
        return
    items = known_items

    # Dedup by message_id (more reliable than context_token)
    dedup_key = str(msg_id)
    if dedup_key in _processed_keys:
        return
    _processed_keys.append(dedup_key)
    if len(_processed_keys) > _DEDUP_MAX:
        del _processed_keys[:len(_processed_keys) - _DEDUP_MAX // 2]

    # Record context_token for reply routing
    record_context_token(from_user, context_token)

    # Record last active platform
    await asyncio.to_thread(_record_last_active, from_user)

    # Reset proactive loop
    asyncio.create_task(_reschedule_random_proactive())

    # Dispatch by item type
    for item in items:
        item_type = item.get("type")
        if item_type == 1:  # text
            text = (item.get("text_item") or {}).get("text", "").strip()
            if text:
                await _handle_text(from_user, context_token, text)
        elif item_type == 2:  # image
            await _handle_image(from_user, context_token, item.get("image_item") or {}, api)
        elif item_type == 3:  # audio
            await _handle_voice(from_user, context_token, item.get("audio_item") or {}, api)


# ── Text (always short mode — buffer) ────────────────────────────────────────

async def _handle_text(user_id: str, context_token: str, text: str) -> None:
    delay = await get_buffer_seconds()
    buf = _buffers.setdefault(user_id, _ChatBuffer())
    buf.messages.append(text)
    buf.context_tokens.append(context_token)
    if buf.timer_task and not buf.timer_task.done():
        buf.timer_task.cancel()
    task = asyncio.create_task(_buffer_fire(user_id, delay))
    buf.timer_task = task


async def _buffer_fire(user_id: str, delay: float) -> None:
    task = asyncio.current_task()
    await asyncio.sleep(delay)
    buf = _buffers.get(user_id)
    if not buf or not buf.messages:
        return
    if buf.timer_task is not task:
        return
    combined = "\n".join(buf.messages)
    ctx_tokens = list(buf.context_tokens) if buf.context_tokens else None
    last_ctx = buf.context_tokens[-1] if buf.context_tokens else None
    buf.messages.clear()
    buf.context_tokens.clear()
    buf.timer_task = None
    await _process_request(user_id, combined, is_short=True,
                           wechat_message_ids=ctx_tokens, context_token=last_ctx)


# ── Voice (download + decrypt + STT + buffer) ────────────────────────────────

async def _handle_voice(user_id: str, context_token: str, audio_item: dict, api) -> None:
    try:
        audio_data = await api.download_media(audio_item)
    except Exception as exc:
        logger.error("[wechat] Failed to download/decrypt voice: %s", exc)
        return

    from app.services.stt_service import transcribe
    text = await asyncio.to_thread(transcribe, audio_data, "voice.silk")
    if not text:
        try:
            await api.send_text(user_id, "语音识别失败，请重新发送或改用文字", context_token)
        except Exception:
            pass
        return

    logger.info("[wechat voice] Transcribed: %s", text[:60])
    text = f"[语音消息] {text}"

    delay = await get_buffer_seconds()
    buf = _buffers.setdefault(user_id, _ChatBuffer())
    buf.messages.append(text)
    buf.context_tokens.append(context_token)
    if buf.timer_task and not buf.timer_task.done():
        buf.timer_task.cancel()
    buf.timer_task = asyncio.create_task(_buffer_fire(user_id, delay))


# ── Image (download + decrypt + store + buffer) ──────────────────────────────

async def _handle_image(user_id: str, context_token: str, image_item: dict, api) -> None:
    try:
        image_bytes = await api.download_media(image_item)
    except Exception as exc:
        logger.error("[wechat] Failed to download/decrypt image: %s", exc, exc_info=True)
        return

    from app.telegram.service import encode_photo_base64
    image_data = await asyncio.to_thread(encode_photo_base64, image_bytes, "image/jpeg")
    content = "[图片]"

    session_id, _ = await get_session_info(WECHAT_ASSISTANT_ID)
    await store_message_only(session_id, content, image_data=image_data, wechat_message_id=[context_token])
    # Image is stored; ChatService will see it when the next text message triggers a completion.
    # Don't put into buffer — that would cause a duplicate [图片] message in DB.


# ── Process request ──────────────────────────────────────────────────────────

async def _process_request(
    user_id: str,
    text: str,
    is_short: bool,
    wechat_message_ids: list[str] | None = None,
    context_token: str | None = None,
) -> None:
    try:
        session_id, assistant_name = await get_session_info(WECHAT_ASSISTANT_ID)
        result_messages = await call_chat_completion(
            session_id, assistant_name, text, short_mode=is_short,
            assistant_id=WECHAT_ASSISTANT_ID,
            wechat_message_ids=wechat_message_ids,
            user_id=user_id,
            context_token=context_token,
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
            elif msg.get("_switched_channel") == "qq":
                try:
                    from app.qq.service import send_reply_with_voice as qq_send
                    from app.telegram.service import get_setting
                    qq_uid = await get_setting("last_active_qq_user_id")
                    if qq_uid:
                        await qq_send(int(qq_uid), content)
                        logger.info("[switch_channel] Routed reply to QQ (uid=%s)", qq_uid)
                        continue
                except Exception as e:
                    logger.error("[switch_channel] Failed to route to QQ: %s", e)
            if i > 0:
                from .service import _typing_delay
                await asyncio.sleep(_typing_delay(content))
            await send_reply(user_id, content, context_token)
            if context_token and msg.get("db_id"):
                await update_wechat_message_id(msg["db_id"], context_token)

    except Exception as exc:
        logger.error("[wechat] Error processing request for user %s: %s", user_id, exc, exc_info=True)
        try:
            from .service import _ilink_api as _api_ref
            if _api_ref and context_token:
                await _api_ref.send_text(user_id, "出错了，请稍后再试", context_token)
        except Exception:
            pass
