from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message

from .config import ALLOWED_CHAT_ID
from aiogram.types import ReplyKeyboardRemove
from .service import (
    call_chat_completion,
    call_chat_completion_with_image,
    call_chat_completion_with_meta,
    encode_photo_base64,
    get_session_info,
    lookup_by_telegram_message_id,
    store_message_only,
    undo_last_round,
    update_telegram_message_id,
)
from app.services.image_description_service import extract_file_content, truncate_to_tokens, get_trigger_threshold

logger = logging.getLogger(__name__)
router = Router()

# ── Per-bot state ────────────────────────────────────────────────────────────

@dataclass
class _ChatBuffer:
    messages: list[str] = field(default_factory=list)
    message_ids: list[int] = field(default_factory=list)
    timer_task: Optional[asyncio.Task] = None


@dataclass
class _BotState:
    buffers: dict[int, _ChatBuffer] = field(default_factory=dict)
    processed_msg_ids: set[int] = field(default_factory=set)


_DEDUP_MAX = 500  # max tracked message_ids per bot to prevent unbounded growth


_bot_states: dict[str, _BotState] = {}


def _get_state(bot_key: str) -> _BotState:
    if bot_key not in _bot_states:
        _bot_states[bot_key] = _BotState()
    return _bot_states[bot_key]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_allowed(chat_id: int) -> bool:
    if ALLOWED_CHAT_ID == 0:
        return True
    return chat_id == ALLOWED_CHAT_ID


_PLATFORM_SWITCH_PROMPTS: dict[str, str] = {
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
}


def _record_last_active_telegram() -> None:
    """Record that user was last active on Telegram. Insert mode switch message if platform changed."""
    from app.database import SessionLocal
    from app.models.models import ChatSession, Settings
    from app.models.models import Message as MessageModel

    db = SessionLocal()
    try:
        row = db.query(Settings).filter(Settings.key == "last_active_source").first()
        if row:
            row.value = "telegram"
        else:
            db.add(Settings(key="last_active_source", value="telegram"))

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
        need_switch = old_source is not None and old_source != "telegram"
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
        # Skip if model already switched to this platform via switch_channel tool
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
            if recent_tool_switch and "Telegram" in (recent_tool_switch.content or ""):
                need_switch = False
        if need_switch:
            prompt = _PLATFORM_SWITCH_PROMPTS.get("telegram", "")
            if prompt and latest_session:
                db.add(MessageModel(
                    session_id=latest_session.id,
                    role="system",
                    content=prompt,
                    meta_info={"mode_switch": True, "source": "telegram"},
                ))

        db.commit()
        logger.info("[telegram] last_active: %s → telegram, switch_msg=%s", old_source, need_switch)
    except Exception as exc:
        logger.error("[telegram] Failed to record last_active: %s", exc, exc_info=True)
    finally:
        db.close()


async def _typing_loop(bot: Bot, chat_id: int, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            await bot.send_chat_action(chat_id=chat_id, action="typing")
        except Exception as exc:
            logger.debug("typing_loop error: %s", exc)
        try:
            await asyncio.wait_for(
                asyncio.shield(stop_event.wait()), timeout=4.0
            )
        except asyncio.TimeoutError:
            pass


async def _send_one_part(bot: Bot, chat_id: int, text: str, *, explicit_block: bool = False) -> int | None:
    """Send a single message part, handling any number of [[voice:]] tags.

    explicit_block=True  → model used [NEXT], voice entire segment after tag
    explicit_block=False → model forgot [NEXT], only voice first line after tag
    """
    from app.services.tts_service import EMOTION_TAG_RE, VALID_EMOTIONS, synthesize

    last_id = None
    segments = EMOTION_TAG_RE.split(text)

    # No voice tag at all → plain text
    if len(segments) == 1:
        clean = text.strip()
        if not clean:
            return None
        sent = await bot.send_message(chat_id=chat_id, text=clean)
        return sent.message_id

    sent_something = False
    for idx in range(0, len(segments), 2):
        seg_text = segments[idx].strip()
        if not seg_text:
            continue

        if sent_something:
            await asyncio.sleep(_typing_delay(seg_text))

        if idx == 0:
            # Text before the first voice tag → plain text
            sent = await bot.send_message(chat_id=chat_id, text=seg_text)
            last_id = sent.message_id
            sent_something = True
        else:
            # Text after a voice tag
            from app.services.tts_service import resolve_emotion
            emotion = resolve_emotion(segments[idx - 1])

            if explicit_block:
                # Model used [NEXT] — voice the entire segment
                voice_line = seg_text
                rest_text = ""
            else:
                # Model forgot [NEXT] — only voice first line, rest as plain text
                lines = seg_text.split("\n", 1)
                voice_line = lines[0].strip()
                rest_text = lines[1].strip() if len(lines) > 1 else ""

            if voice_line and emotion and len(voice_line) <= 300:
                voice_sent = False
                try:
                    audio_bytes = await asyncio.to_thread(synthesize, voice_line, emotion)
                    if audio_bytes:
                        from aiogram.types import BufferedInputFile
                        voice_file = BufferedInputFile(audio_bytes, filename="voice.mp3")
                        sent = await bot.send_voice(chat_id=chat_id, voice=voice_file, caption=voice_line)
                        last_id = sent.message_id
                        sent_something = True
                        voice_sent = True
                        logger.info("[voice] Sent voice with caption (emotion=%s, %d chars)", emotion, len(voice_line))
                except Exception as e:
                    logger.warning("[voice] Voice send failed: %s", e)

            if voice_line and not voice_sent:
                sent = await bot.send_message(chat_id=chat_id, text=voice_line)
                last_id = sent.message_id
                sent_something = True

            if rest_text:
                if sent_something:
                    await asyncio.sleep(_typing_delay(rest_text))
                sent = await bot.send_message(chat_id=chat_id, text=rest_text)
                last_id = sent.message_id
                sent_something = True

    return last_id


def _typing_delay(text: str) -> float:
    """Simulate natural typing delay based on message length."""
    import random
    base = random.uniform(2.0, 3.0)
    length_bonus = min(len(text) * 0.05, 2.5)
    return base + length_bonus


async def _send_reply_with_voice(bot: Bot, chat_id: int, text: str) -> list[int]:
    """Send reply, splitting by [NEXT]. Each part independently checks for voice."""
    tg_ids: list[int] = []
    if not text.strip():
        return tg_ids

    # Safety net: strip any leaked memory reference IDs, metadata, or THINK blocks
    import re as _re
    text = _re.sub(r'(?:\[THINK\]|<scratchpad>).*?(?:\[/THINK\]|</THINK>|</thinking>|</scratchpad>)', '', text, flags=_re.DOTALL)
    for _orphan in ('<scratchpad>', '</scratchpad>', '[THINK]', '[/THINK]', '</THINK>', '</thinking>'):
        text = text.replace(_orphan, '')
    text = _re.sub(r'\[#\s*\d+\s*\]\s*', '', text)
    text = _re.sub(r'\[\[used:[\d,\s]+\]\]', '', text)
    text = _re.sub(r'\(来源:\s*\w+\)\s*$', '', text, flags=_re.MULTILINE)
    parts = [p.strip() for p in text.split("[NEXT]") if p.strip()]
    has_next = len(parts) > 1  # model explicitly used [NEXT]

    for i, part in enumerate(parts):
        if i > 0:
            await asyncio.sleep(_typing_delay(part))
        tg_id = await _send_one_part(bot, chat_id, part, explicit_block=has_next)
        if tg_id:
            tg_ids.append(tg_id)

    return tg_ids


_session_locks: dict[int, asyncio.Lock] = {}


async def _process_request(
    chat_id: int,
    combined_text: str,
    bot: Bot,
    bot_key: str,
    assistant_id: int,

    telegram_message_id: list[int] | None = None,
) -> None:
    session_id, assistant_name = await get_session_info(assistant_id)

    # Per-session lock: queue new messages while previous request is still running
    if session_id not in _session_locks:
        _session_locks[session_id] = asyncio.Lock()
    lock = _session_locks[session_id]

    if lock.locked():
        logger.info("[telegram] Session %d busy, queuing message", session_id)

    async with lock:
        # If cafe @mention generation is in progress, wait for it so its
        # tool-call messages land in DB first and end up in the private-chat context.
        try:
            from app.services.cafe_service import cafe_service as _cafe_service
            _wait_start = asyncio.get_event_loop().time()
            while getattr(_cafe_service, "_generating_source", None):
                if asyncio.get_event_loop().time() - _wait_start > 120:
                    logger.warning("[tg] Waited 120s for cafe to release, giving up")
                    break
                await asyncio.sleep(0.2)
        except Exception:
            logger.warning("[tg] cafe service lookup failed (continuing)", exc_info=True)

        stop_event = asyncio.Event()
        typing_task = asyncio.create_task(_typing_loop(bot, chat_id, stop_event))

        try:
            # Collect thinking chunks so we can attach an expandable CoT
            # block to clean single long-mode replies.
            thinking_buf = [""]
            def _on_thinking(chunk: str) -> None:
                thinking_buf[0] += chunk

            result_messages = await call_chat_completion(
                session_id, assistant_name, combined_text,
                short_mode=False,
                telegram_message_id=telegram_message_id,
                assistant_id=assistant_id,
                chat_id=chat_id,
                bot=bot,
                on_thinking_chunk=_on_thinking,
            )

            stop_event.set()
            typing_task.cancel()

            sent_count = 0
            for msg in result_messages:
                content = (msg.get("content") or "").strip()
                if not content or msg.get("no_message"):
                    continue
                # Route to QQ if model switched channel
                if msg.get("_switched_channel") == "qq":
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
                if sent_count > 0:
                    await asyncio.sleep(_typing_delay(content))

                # Attach expandable CoT block only for single clean long-mode replies
                # (no [NEXT] splits / no [voice:] tags / no channel switch)
                use_cot_block = (
                    len(result_messages) == 1
                    and thinking_buf[0].strip()
                    and "[NEXT]" not in content
                    and "[voice:" not in content
                )
                cot_sent_id: int | None = None
                if use_cot_block:
                    import html as _html, re as _re
                    cleaned = _re.sub(r'(?:\[THINK\]|<scratchpad>).*?(?:\[/THINK\]|</THINK>|</thinking>|</scratchpad>)', '', content, flags=_re.DOTALL)
                    for _orphan in ('<scratchpad>', '</scratchpad>', '[THINK]', '[/THINK]', '</THINK>', '</thinking>'):
                        cleaned = cleaned.replace(_orphan, '')
                    cleaned = _re.sub(r'\[#\s*\d+\s*\]\s*', '', cleaned)
                    cleaned = _re.sub(r'\[\[used:[\d,\s]+\]\]', '', cleaned)
                    cleaned = _re.sub(r'\(来源:\s*\w+\)\s*$', '', cleaned, flags=_re.MULTILINE)
                    cleaned = cleaned.strip()
                    if cleaned:
                        safe_thinking = _html.escape(thinking_buf[0].strip())
                        safe_content = _html.escape(cleaned)
                        combined = (
                            f"<blockquote expandable><b>助手A正在想…💭</b>\n\n{safe_thinking}</blockquote>\n\n"
                            f"{safe_content}"
                        )
                        if len(combined) <= 4000:
                            # CoT + 正文装得下一条
                            try:
                                sent = await bot.send_message(
                                    chat_id=chat_id,
                                    text=combined,
                                    parse_mode="HTML",
                                )
                                cot_sent_id = sent.message_id
                            except Exception as e:
                                logger.warning("[tg] CoT+body send failed, falling back to plain: %s", e)
                        else:
                            # 超 4096 硬限,把 CoT 独立一条先发,正文走下面 _send_reply_with_voice
                            cot_only = f"<blockquote expandable><b>助手A正在想…💭</b>\n\n{safe_thinking}</blockquote>"
                            if len(cot_only) <= 4000:
                                try:
                                    await bot.send_message(
                                        chat_id=chat_id,
                                        text=cot_only,
                                        parse_mode="HTML",
                                    )
                                except Exception as e:
                                    logger.warning("[tg] CoT-only send failed: %s", e)
                            else:
                                logger.warning("[tg] CoT itself > 4000 chars, dropped")
                            # cot_sent_id stays None → body goes through _send_reply_with_voice below

                if cot_sent_id is not None:
                    if msg.get("db_id"):
                        await update_telegram_message_id(msg["db_id"], cot_sent_id)
                else:
                    tg_ids = await _send_reply_with_voice(bot, chat_id, content)
                    if tg_ids and msg.get("db_id"):
                        await update_telegram_message_id(msg["db_id"], tg_ids[-1])
                sent_count += 1

        except Exception as exc:
            stop_event.set()
            typing_task.cancel()
            logger.error("Error processing request for chat %s (bot=%s): %s", chat_id, bot_key, exc, exc_info=True)
            try:
                await bot.send_message(chat_id=chat_id, text="❌ 出错了，请稍后再试")
            except Exception:
                pass


async def _process_photo_request(
    chat_id: int,
    content: str,
    image_data: str,
    bot: Bot,
    bot_key: str,
    assistant_id: int,

    telegram_message_id: list[int] | None = None,
) -> None:
    """Process a photo message: store with image_data and trigger chat completion."""
    stop_event = asyncio.Event()
    typing_task = asyncio.create_task(_typing_loop(bot, chat_id, stop_event))
    try:
        session_id, assistant_name = await get_session_info(assistant_id)
        result_messages = await call_chat_completion_with_image(
            session_id, assistant_name, content,
            image_data=image_data,
            short_mode=False,
            telegram_message_id=telegram_message_id,
            assistant_id=assistant_id,
            chat_id=chat_id,
            bot=bot,
        )
        stop_event.set()
        typing_task.cancel()
        sent_count = 0
        for msg in result_messages:
            reply_content = (msg.get("content") or "").strip()
            if not reply_content or msg.get("no_message"):
                continue
            if msg.get("_switched_channel") == "qq":
                try:
                    from app.qq.service import send_reply_with_voice as qq_send
                    from app.telegram.service import get_setting
                    qq_uid = await get_setting("last_active_qq_user_id")
                    if qq_uid:
                        await qq_send(int(qq_uid), reply_content)
                        logger.info("[switch_channel] Routed reply to QQ (uid=%s)", qq_uid)
                        continue
                except Exception as e:
                    logger.error("[switch_channel] Failed to route to QQ: %s", e)
            elif msg.get("_switched_channel") == "wechat":
                try:
                    from app.wechat.service import send_reply as wx_send
                    from app.telegram.service import get_setting
                    wx_uid = await get_setting("last_active_wechat_user_id")
                    if wx_uid:
                        await wx_send(wx_uid, reply_content)
                        logger.info("[switch_channel] Routed reply to WeChat (uid=%s)", wx_uid)
                        continue
                except Exception as e:
                    logger.error("[switch_channel] Failed to route to WeChat: %s", e)
            if sent_count > 0:
                await asyncio.sleep(_typing_delay(reply_content))
            tg_ids = await _send_reply_with_voice(bot, chat_id, reply_content)
            if tg_ids and msg.get("db_id"):
                await update_telegram_message_id(msg["db_id"], tg_ids[-1])
            sent_count += 1
    except Exception as exc:
        stop_event.set()
        typing_task.cancel()
        logger.error("Error processing photo for chat %s: %s", chat_id, exc, exc_info=True)
        try:
            await bot.send_message(chat_id=chat_id, text="❌ 出错了，请稍后再试")
        except Exception:
            pass


async def _process_file_request(
    chat_id: int,
    content: str,
    meta_info: dict,
    bot: Bot,
    bot_key: str,
    assistant_id: int,

    telegram_message_id: list[int] | None = None,
) -> None:
    """Process a file message: store with meta_info and trigger chat completion."""
    stop_event = asyncio.Event()
    typing_task = asyncio.create_task(_typing_loop(bot, chat_id, stop_event))
    try:
        session_id, assistant_name = await get_session_info(assistant_id)
        result_messages = await call_chat_completion_with_meta(
            session_id, assistant_name, content,
            meta_info=meta_info,
            short_mode=False,
            telegram_message_id=telegram_message_id,
            assistant_id=assistant_id,
            chat_id=chat_id,
            bot=bot,
        )
        stop_event.set()
        typing_task.cancel()
        sent_count = 0
        for msg in result_messages:
            reply_content = (msg.get("content") or "").strip()
            if not reply_content or msg.get("no_message"):
                continue
            if msg.get("_switched_channel") == "qq":
                try:
                    from app.qq.service import send_reply_with_voice as qq_send
                    from app.telegram.service import get_setting
                    qq_uid = await get_setting("last_active_qq_user_id")
                    if qq_uid:
                        await qq_send(int(qq_uid), reply_content)
                        logger.info("[switch_channel] Routed reply to QQ (uid=%s)", qq_uid)
                        continue
                except Exception as e:
                    logger.error("[switch_channel] Failed to route to QQ: %s", e)
            elif msg.get("_switched_channel") == "wechat":
                try:
                    from app.wechat.service import send_reply as wx_send
                    from app.telegram.service import get_setting
                    wx_uid = await get_setting("last_active_wechat_user_id")
                    if wx_uid:
                        await wx_send(wx_uid, reply_content)
                        logger.info("[switch_channel] Routed reply to WeChat (uid=%s)", wx_uid)
                        continue
                except Exception as e:
                    logger.error("[switch_channel] Failed to route to WeChat: %s", e)
            if sent_count > 0:
                await asyncio.sleep(_typing_delay(reply_content))
            tg_ids = await _send_reply_with_voice(bot, chat_id, reply_content)
            if tg_ids and msg.get("db_id"):
                await update_telegram_message_id(msg["db_id"], tg_ids[-1])
            sent_count += 1
    except Exception as exc:
        stop_event.set()
        typing_task.cancel()
        logger.error("Error processing file for chat %s: %s", chat_id, exc, exc_info=True)
        try:
            await bot.send_message(chat_id=chat_id, text="❌ 出错了，请稍后再试")
        except Exception:
            pass


async def _buffer_fire(chat_id: int, bot: Bot, delay: float, bot_key: str, assistant_id: int) -> None:
    """Buffer fire for short mode (kept for potential future use)."""
    await asyncio.sleep(delay)
    state = _get_state(bot_key)
    buf = state.buffers.pop(chat_id, None)
    if buf and buf.messages:
        combined = "\n".join(buf.messages)
        tg_ids = buf.message_ids if buf.message_ids else None
        await _process_request(
            chat_id, combined, bot, bot_key, assistant_id,
            telegram_message_id=tg_ids,
        )


# ── Command handlers ─────────────────────────────────────────────────────────

@router.message(Command("start"))
async def cmd_start(message: Message, bot_key: str, **_kw) -> None:
    if not _is_allowed(message.chat.id):
        return
    await message.answer(
        "你好 ❤",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(Command("undo"))
async def cmd_undo(message: Message, assistant_id: int, **_kw) -> None:
    if not _is_allowed(message.chat.id):
        return
    deleted = await undo_last_round(assistant_id)
    if deleted:
        await message.answer(f"已撤回 {deleted} 条消息")
    else:
        await message.answer("没有可撤回的消息")


# ── Main message handler ─────────────────────────────────────────────────────

@router.message()
async def handle_message(message: Message, bot: Bot, bot_key: str, assistant_id: int, **_kw) -> None:
    if not _is_allowed(message.chat.id):
        return

    # Record last active platform
    asyncio.get_event_loop().run_in_executor(None, _record_last_active_telegram)

    if message.location is not None:
        return

    # Reset random proactive timer on every user message
    asyncio.create_task(_reschedule_random_proactive())

    chat_id = message.chat.id

    # ── Dedup: skip if this message_id was already processed ──
    state = _get_state(bot_key)
    msg_id = message.message_id
    if msg_id in state.processed_msg_ids:
        logger.debug("Skipping duplicate message_id=%d (bot=%s)", msg_id, bot_key)
        return
    state.processed_msg_ids.add(msg_id)
    if len(state.processed_msg_ids) > _DEDUP_MAX:
        sorted_ids = sorted(state.processed_msg_ids)
        state.processed_msg_ids = set(sorted_ids[len(sorted_ids) - _DEDUP_MAX // 2 :])

    # ── Photo handling ──
    if message.photo:
        caption = (message.caption or "").strip()
        try:
            photo = message.photo[-1]  # largest size
            file_info = await bot.get_file(photo.file_id)
            file_bytes = await bot.download_file(file_info.file_path)
            image_data = await asyncio.to_thread(encode_photo_base64, file_bytes, "image/jpeg")
        except Exception as exc:
            logger.error("Failed to download photo: %s", exc)
            return

        session_id, _ = await get_session_info(assistant_id)

        if caption:
            content = f"{caption}\n\n[图片]"
        else:
            content = "[图片]"

        # Telegram = long mode: caption triggers reply, no caption stores only
        if caption:
            await _process_photo_request(
                chat_id, content, image_data,
                bot, bot_key, assistant_id,                telegram_message_id=[message.message_id],
            )
        else:
            await store_message_only(
                session_id, content, image_data=image_data,
                telegram_message_id=[message.message_id],
            )
        return

    # ── Document handling ──
    if message.document:
        caption = (message.caption or "").strip()
        doc = message.document
        file_name = doc.file_name or "unknown"

        try:
            file_info = await bot.get_file(doc.file_id)
            file_bytes_io = await bot.download_file(file_info.file_path)
            if hasattr(file_bytes_io, "read"):
                file_data = file_bytes_io.read()
            else:
                file_data = file_bytes_io
        except Exception as exc:
            logger.error("Failed to download document: %s", exc)
            return

        # Extract text content
        file_text = extract_file_content(file_name, file_data)
        if not file_text:
            file_text = ""
            content_marker = f"[文件：{file_name}，内容提取失败]"
        else:
            # Truncate if too long
            from app.database import SessionLocal
            _db = SessionLocal()
            try:
                threshold = get_trigger_threshold(_db)
            finally:
                _db.close()
            max_file_tokens = threshold // 2
            file_text = truncate_to_tokens(file_text, max_file_tokens)
            content_marker = f"[文件：{file_name}]\n{file_text}"

        if caption:
            content = f"{caption}\n\n{content_marker}"
        else:
            content = content_marker

        meta_info = {"needs_file_summary": True, "file_name": file_name}
        mode = await get_chat_mode()
        session_id, _ = await get_session_info(assistant_id)

        if mode == "short":
            # Short mode: store with meta, add text to buffer
            await store_message_only(
                session_id, content, meta_info=meta_info,
                telegram_message_id=[message.message_id],
            )
            buf_text = caption if caption else f"[发送了文件：{file_name}]"
            delay = await get_buffer_seconds()
            buf = state.buffers.setdefault(chat_id, _ChatBuffer())
            buf.messages.append(buf_text)
            buf.message_ids.append(message.message_id)
            if buf.timer_task and not buf.timer_task.done():
                buf.timer_task.cancel()
            buf.timer_task = asyncio.create_task(
                _buffer_fire(chat_id, bot, delay, bot_key, assistant_id)
            )
        elif caption:
            # Long mode with caption → trigger reply
            await _process_file_request(
                chat_id, content, meta_info,
                bot, bot_key, assistant_id,                telegram_message_id=[message.message_id],
            )
        else:
            # Long mode without caption → store only, wait for next text
            await store_message_only(
                session_id, content, meta_info=meta_info,
                telegram_message_id=[message.message_id],
            )
        return

    # ── Voice message handling (STT) ──
    if message.voice:
        try:
            file_info = await bot.get_file(message.voice.file_id)
            file_bytes_io = await bot.download_file(file_info.file_path)
            if hasattr(file_bytes_io, "read"):
                audio_data = file_bytes_io.read()
            else:
                audio_data = file_bytes_io
        except Exception as exc:
            logger.error("Failed to download voice: %s", exc)
            return

        # Transcribe
        from app.services.stt_service import transcribe
        text = await asyncio.to_thread(transcribe, audio_data, "voice.ogg")
        if not text:
            await bot.send_message(chat_id=chat_id, text="语音识别失败，请重新发送或改用文字")
            return

        logger.info("[voice] Transcribed: %s", text[:60])
        text = f"[语音消息] {text}"

        # Telegram = long mode: process immediately
        await _process_request(
            chat_id, text, bot, bot_key, assistant_id,
                       telegram_message_id=[message.message_id],
        )
        return

    # ── Text handling (original logic) ──
    text = (message.text or message.caption or "").strip()
    if not text:
        return

    # Handle reply/quote — look up the quoted message by telegram_message_id
    if message.reply_to_message:
        quoted_tg_id = message.reply_to_message.message_id
        quoted = await lookup_by_telegram_message_id(quoted_tg_id)
        if quoted:
            quote_prefix = f"[引用消息 id={quoted['id']}] {quoted['content']}"
            text = f"{quote_prefix}\n{text}"

    # Telegram = long mode: process immediately
    await _process_request(
        chat_id, text, bot, bot_key, assistant_id,
               telegram_message_id=[message.message_id],
    )


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
