from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from openai import OpenAI
import anthropic
from sqlalchemy.orm import Session, sessionmaker

from app.models.models import (
    ApiProvider,
    Assistant,
    ChatSession,
    Memory,
    Message,
    ModelPreset,
    PendingMemory,
    SessionSummary,
    Settings,
    SummaryLayer,
    SummaryLayerHistory,
    UserProfile,
)
from app.services.core_blocks_updater import CoreBlocksUpdater

logger = logging.getLogger(__name__)

TZ_EAST8 = timezone(timedelta(hours=8))


def _call_model_raw(
    db: Session, preset: ModelPreset, system_prompt: str, user_text: str | list,
    *, timeout: float | None = None, source: str | None = None,
    assistant_id: int | None = 2,
    cot_request_id: str | None = None, cot_round_index: int = 0,
    cot_emit_done: bool = True,
    messages: list[dict[str, Any]] | None = None,
    response_blocks_out: list[dict[str, Any]] | None = None,
) -> str:
    """Call a model preset and return raw text response.

    user_text can be a string or a list of content blocks (for multimodal).
    Also writes COT records (thinking + text + usage) with model_info badge.
    Pass source= to label the COT card (e.g. "reflection", "summary", "merge").
    """
    import time as _time
    import uuid as _uuid

    api_provider = db.get(ApiProvider, preset.api_provider_id)
    if not api_provider:
        raise ValueError(f"API provider not found for preset_id={preset.id}")

    base_url = api_provider.base_url
    if base_url.endswith("/chat/completions"):
        base_url = base_url[: -len("/chat/completions")]
        if not base_url.endswith("/v1"):
            base_url = f"{base_url.rstrip('/')}/v1"

    t0 = _time.time()
    thinking_text = ""
    content = ""
    usage_info: dict[str, Any] = {}
    _round_usage: dict[str, Any] | None = None

    _is_oauth = api_provider.auth_type in ("oauth_token", "oauth_claude")
    _is_anthropic_native = api_provider.auth_type in ("anthropic", "oauth_token", "oauth_claude")
    if _is_anthropic_native:
        from app.services.chat.config_helpers import normalize_anthropic_base_url

        if _is_oauth:
            from app.services.chat.oauth_helper import ensure_valid_token, inject_billing_header
            _access_token = ensure_valid_token(db, api_provider=api_provider) or api_provider.api_key
        else:
            ensure_valid_token = None  # not used for api_key auth
            inject_billing_header = None
            _access_token = None

        def _make_anth_client(tok: str | None) -> anthropic.Anthropic:
            _kw: dict[str, Any] = {}
            if _is_oauth:
                _kw["auth_token"] = tok
                _kw["default_headers"] = {
                    "anthropic-beta": "claude-code-20250219,oauth-2025-04-20",
                    "user-agent": "claude-code/2.1.77 (external, cli)",
                    "x-app": "cli",
                }
            else:
                _kw["api_key"] = api_provider.api_key
                _anth_base_url = normalize_anthropic_base_url(base_url)
                if _anth_base_url:
                    _kw["base_url"] = _anth_base_url
            if timeout is not None:
                _kw["timeout"] = timeout
            return anthropic.Anthropic(**_kw)

        anth_client = _make_anth_client(_access_token)
        _summary_tb = preset.thinking_budget or 0
        anth_kwargs: dict[str, Any] = {
            "model": preset.model_name,
            "system": system_prompt,
            "messages": messages if messages is not None else [{"role": "user", "content": user_text}],
        }
        if _summary_tb > 0:
            anth_kwargs["max_tokens"] = preset.max_tokens + _summary_tb
            anth_kwargs["thinking"] = {"type": "enabled", "budget_tokens": _summary_tb}
        else:
            anth_kwargs["max_tokens"] = preset.max_tokens
        if preset.temperature is not None:
            anth_kwargs["temperature"] = preset.temperature
        if preset.top_p is not None:
            anth_kwargs["top_p"] = preset.top_p
        if _is_oauth and inject_billing_header is not None:
            inject_billing_header(anth_kwargs)
        try:
            anth_response = anth_client.messages.create(**anth_kwargs)
        except anthropic.AuthenticationError:
            # Token went stale mid-flight — force refresh and retry once (OAuth only)
            if _is_oauth and ensure_valid_token is not None:
                logger.info("[summary] OAuth 401, forcing token refresh and retrying once")
                _refreshed = ensure_valid_token(db, api_provider=api_provider, force=True)
                if _refreshed and _refreshed != _access_token:
                    anth_client = _make_anth_client(_refreshed)
                    anth_response = anth_client.messages.create(**anth_kwargs)
                else:
                    raise
            else:
                raise
        for block in anth_response.content:
            if block.type == "thinking":
                thinking_text += block.thinking
                if response_blocks_out is not None:
                    response_blocks_out.append({
                        "type": "thinking", "thinking": block.thinking,
                        "signature": getattr(block, "signature", ""),
                    })
            elif block.type == "text":
                content += block.text
                if response_blocks_out is not None:
                    response_blocks_out.append({"type": "text", "text": block.text})
        if anth_response.usage:
            _su = anth_response.usage
            _s_cache_read = getattr(_su, "cache_read_input_tokens", 0) or 0
            _s_cache_create = getattr(_su, "cache_creation_input_tokens", 0) or 0
            usage_info = {
                "prompt_tokens": _su.input_tokens,
                "completion_tokens": _su.output_tokens,
                "cache_hit": _s_cache_read > 0,
                "total_input": _su.input_tokens + _s_cache_read + _s_cache_create,
            }
            _round_usage = {
                "input": _su.input_tokens,
                "cache_create": _s_cache_create,
                "cache_read": _s_cache_read,
                "output": _su.output_tokens,
            }
    else:
        _oai_kwargs: dict[str, Any] = {"api_key": api_provider.api_key, "base_url": base_url}
        if timeout is not None:
            _oai_kwargs["timeout"] = timeout
        oai_client = OpenAI(**_oai_kwargs)
        # Convert Anthropic-format image blocks to OpenAI format if needed
        _oai_user_content = user_text
        if isinstance(user_text, list):
            _oai_user_content = []
            for _block in user_text:
                if isinstance(_block, dict) and _block.get("type") == "image":
                    _src = _block.get("source", {})
                    _oai_user_content.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:{_src.get('media_type', 'image/jpeg')};base64,{_src.get('data', '')}"},
                    })
                else:
                    _oai_user_content.append(_block)
        if messages is not None:
            _oai_messages = [{"role": "system", "content": system_prompt}] + messages
        else:
            _oai_messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": _oai_user_content},
            ]
        params: dict[str, Any] = {
            "model": preset.model_name,
            "messages": _oai_messages,
            "max_tokens": preset.max_tokens,
        }
        if preset.temperature is not None:
            params["temperature"] = preset.temperature
        if preset.top_p is not None:
            params["top_p"] = preset.top_p
        _summary_tb_oai = preset.thinking_budget or 0
        if _summary_tb_oai > 0:
            params["extra_body"] = {
                "reasoning": {"max_tokens": _summary_tb_oai},
                "enable_thinking": True,
                "thinking_budget": _summary_tb_oai,
            }
        else:
            params["extra_body"] = {"enable_thinking": False}
        oai_response = oai_client.chat.completions.create(**params)
        if not oai_response.choices:
            raise ValueError("Response contained no choices.")
        msg = oai_response.choices[0].message
        content = msg.content or ""
        if response_blocks_out is not None:
            response_blocks_out.append({"type": "text", "text": content})
        reasoning_content = getattr(msg, "reasoning_content", None) or getattr(msg, "reasoning", None)
        if reasoning_content:
            thinking_text = str(reasoning_content)
        if oai_response.usage:
            _ou = oai_response.usage
            _o_cached = getattr(_ou, "prompt_tokens_details", None)
            _o_cache_read = getattr(_o_cached, "cached_tokens", 0) or 0 if _o_cached else 0
            usage_info = {
                "prompt_tokens": _ou.prompt_tokens or 0,
                "completion_tokens": _ou.completion_tokens or 0,
                "total_input": _ou.prompt_tokens or 0,
            }
            _round_usage = {
                "input": (_ou.prompt_tokens or 0) - _o_cache_read,
                "cache_create": 0,
                "cache_read": _o_cache_read,
                "output": _ou.completion_tokens or 0,
            }

    usage_info["elapsed_ms"] = int((_time.time() - t0) * 1000)

    # Write COT records (skip if source is explicitly False)
    if source is False:
        return content
    try:
        from app.models.models import CotRecord
        from app.cot_broadcaster import cot_broadcaster

        request_id = cot_request_id or str(_uuid.uuid4())
        _ri = cot_round_index
        model_info = {
            "model_name": preset.model_name,
            "preset_name": preset.name,
            "source": source or "summary",
        }

        def _add(round_idx: int, block_type: str, blk_content: str):
            db.add(CotRecord(
                request_id=request_id, round_index=round_idx,
                block_type=block_type, content=blk_content,
                tool_name=None, assistant_id=assistant_id,
            ))
            cot_broadcaster.publish({
                "type": block_type, "request_id": str(request_id),
                "round_index": round_idx, "block_type": block_type,
                "content": blk_content, "tool_name": None,
                "assistant_id": assistant_id,
            })

        _add(_ri, "model_info", json.dumps(model_info, ensure_ascii=False))
        if _round_usage:
            _add(_ri, "round_usage", json.dumps(_round_usage))
        if thinking_text:
            _add(_ri, "thinking", thinking_text)
        if content:
            _add(_ri, "text", content)
        if cot_emit_done:
            _add(9999, "usage", json.dumps(usage_info))
            cot_broadcaster.publish({
                "type": "done", "request_id": str(request_id), "assistant_id": assistant_id,
            })
        db.flush()
    except Exception:
        logger.warning("[summary] failed to write COT records", exc_info=True)
        try:
            db.rollback()
        except Exception:
            pass

    return content


def translate_text(db: Session, text: str, assistant_id: int | None = None) -> str:
    """Translate text to Chinese using the summary fallback model.

    assistant_id: which assistant's preset to use. If not given, falls back
    to the first assistant (legacy behavior).
    """
    assistant = None
    if assistant_id is not None:
        assistant = db.get(Assistant, assistant_id)
    if not assistant:
        assistant = db.query(Assistant).first()
    if not assistant:
        raise ValueError("No assistant configured")

    preset = None
    if assistant.summary_fallback_preset_id:
        preset = db.get(ModelPreset, assistant.summary_fallback_preset_id)
    if not preset and assistant.summary_model_preset_id:
        preset = db.get(ModelPreset, assistant.summary_model_preset_id)
    if not preset:
        preset = db.get(ModelPreset, assistant.model_preset_id)
    if not preset:
        raise ValueError("No model preset available")

    system_prompt = "你是翻译助手。将以下英文内容翻译成中文，保持原意，只输出翻译结果。"
    return _call_model_raw(db, preset, system_prompt, text, source=False)


class SummaryService:
    def __init__(self, session_factory: sessionmaker) -> None:
        self.session_factory = session_factory

    def generate_summary(
        self, session_id: int, messages: list[Message], assistant_id: int
    ) -> None:
        db: Session = self.session_factory()
        try:
            if not messages:
                return

            assistant = db.get(Assistant, assistant_id)
            if not assistant:
                logger.warning(
                    "Summary skipped: assistant not found (assistant_id=%s).",
                    assistant_id,
                )
                return

            primary_preset = self._resolve_primary_preset(db, assistant)
            if not primary_preset:
                logger.warning(
                    "Summary skipped: no available preset (assistant_id=%s).",
                    assistant_id,
                )
                return
            fallback_preset = self._resolve_fallback_preset(db, assistant)
            session_row = db.get(ChatSession, session_id)
            is_chat_session = session_row is None or session_row.type == "chat"

            user_profile = db.query(UserProfile).first()
            user_name = user_profile.nickname if user_profile and user_profile.nickname else "User"
            assistant_name = assistant.name or "Assistant"

            # Split: trimmed messages (being compressed) + retained (still in context)
            trimmed_msgs = sorted(messages, key=lambda m: (m.created_at, m.id))
            max_trimmed_id = max((m.id for m in messages if m.id is not None), default=0)
            retained_msgs = (
                db.query(Message)
                .filter(
                    Message.session_id == session_id,
                    Message.role.in_(["user", "assistant", "tool"]),
                    Message.id > max_trimmed_id,
                )
                .order_by(Message.created_at.asc(), Message.id.asc())
                .limit(60)
                .all()
            )
            logger.info(
                "Summary context split: %d trimmed, %d retained (session_id=%s, max_trimmed_id=%s)",
                len(trimmed_msgs), len(retained_msgs), session_id, max_trimmed_id,
            )

            trimmed_text, trimmed_images = self._format_messages(trimmed_msgs or messages, user_name, assistant_name)
            if not trimmed_text.strip():
                logger.warning("Summary skipped: no usable message content (session_id=%s).", session_id)
                return

            conversation_text_str = (
                trimmed_text
                + "\n\n===== 对话记录结束 =====\n"
                "请根据system prompt中的任务要求，对以上对话输出JSON。不要回复对话内容。"
            )
            if trimmed_images:
                resized = self._resize_images_for_summary(trimmed_images)
                conversation_text: str | list = [{"type": "text", "text": conversation_text_str}]
                for _mt, _b64 in resized:
                    conversation_text.append({"type": "image", "source": {"type": "base64", "media_type": _mt, "data": _b64}})
            else:
                conversation_text = conversation_text_str

            # Build system prompt: full persona + summary/extraction tasks
            base_persona = (assistant.system_prompt or "").strip()

            task_instructions = f"""
系统提示：
你正在回顾刚才的对话，为自己的记忆系统整理内容。只返回JSON，不要多余文字。
对以下对话写摘要和提取记忆。

任务一：摘要
以第一人称视角写摘要，按以下结构：

【话题】关键词1、关键词2、关键词3（短关键词列表，3-6个）
【人物】涉及的人物名字（没有就不写这行）
【情绪】她的情绪变化（可写 A→B，如"焦虑→平静"）
【摘要】
摘要正文

重点记录：聊了什么、做了什么决定、情绪变化、新暴露的信息。
时间用具体描述如"2.5晚上20点左右"，不要用"刚才""昨天"这类相对时间。
亲密场景：只记场景设定、她表达的偏好、情绪变化，不记具体行为描写。
工具调用（存储记忆、搜索、网页等）只需一笔带过，例如'我存了一条关于xxx的记忆''我搜了xxx'。不要记录工具的参数、返回内容或调用过程。
单条摘要严格控制在500字以内。只记结论、决定和关键转折，省略过程性对话。

任务二：记忆提取
从对话中提取值得长期记住的信息。
- 对话中已通过 save_memory 存过的不要重复提取
- 每条记忆不超过100字，用第一人称记录
- 时间戳由后端自动添加，content里不要写日期时间，除非记录的是过去发生的事
- klass分类（严格按定义归类）：
  identity：关于她是谁（名字、身份、自我认知、人生经历）
  relationship：日常相处模式、互动习惯、称呼方式、相处中的默契
  bond：重大情感里程碑、关系转折点、深层情感表达（不是日常亲昵）
  conflict：争吵、矛盾、误解、冷战、道歉
  fact：客观事件、具体发生的事、外部信息
  preference：喜好、习惯、偏好、厌恶
  health：身体状况、作息、饮食、精神状态
  task：待办、约定、承诺、计划
  other：以上都不符合
- 日常闲聊、没有新信息的内容不需要提取
- 没有值得提取的就返回空数组
- tags：给每条记忆加1-3个短关键词标签，方便检索
- disclosure：写一句"什么情况下应该想起这条记忆"，用于情境触发召回。例如："当她提到工作压力时""当讨论到未来计划时""当她情绪低落时"

输出格式：
{{"summary": "...", "memories": [{{"content": "...", "klass": "...", "tags": ["标签1", "标签2"], "disclosure": "..."}}, ...]}}
memories 为空时写 "memories": []
文本中引用内容一律用直角引号「」，不要用英文双引号，避免破坏JSON格式。
""".strip()

            recent_examples = (
                db.query(SessionSummary)
                .filter(
                    SessionSummary.session_id == session_id,
                    SessionSummary.assistant_id == assistant_id,
                    SessionSummary.deleted_at.is_(None),
                    SessionSummary.summary_content.isnot(None),
                )
                .order_by(SessionSummary.id.desc())
                .limit(3)
                .all()
            )
            examples_xml = ""
            if recent_examples:
                blocks = [
                    f"[历史摘要 {i + 1} | msg {s.msg_id_start}-{s.msg_id_end}]\n{s.summary_content.strip()}"
                    for i, s in enumerate(reversed(recent_examples))
                ]
                examples_xml = (
                    "\n\n参考以下历史摘要，保持第一人称视角。\n\n"
                    + "\n\n".join(blocks)
                )

            system_prompt = (
                base_persona + "\n\n" + task_instructions + examples_xml
                if base_persona
                else task_instructions + examples_xml
            )

            parsed_payload: dict[str, Any] | None = None
            try:
                parsed_payload = self._call_summary_model(
                    db,
                    primary_preset,
                    system_prompt,
                    conversation_text,
                )
            except Exception:
                logger.exception(
                    "Primary summary model failed (session_id=%s, preset_id=%s).",
                    session_id,
                    primary_preset.id,
                )
                if fallback_preset and fallback_preset.id != primary_preset.id:
                    try:
                        parsed_payload = self._call_summary_model(
                            db,
                            fallback_preset,
                            system_prompt,
                            conversation_text,
                        )
                    except Exception:
                        logger.exception(
                            "Fallback summary model failed (session_id=%s, preset_id=%s).",
                            session_id,
                            fallback_preset.id,
                        )
                elif fallback_preset and fallback_preset.id == primary_preset.id:
                    logger.warning(
                        "Fallback preset equals primary preset (session_id=%s, preset_id=%s).",
                        session_id,
                        primary_preset.id,
                    )
            if not parsed_payload:
                raise RuntimeError("Both primary and fallback summary models failed")

            summary_text = str(parsed_payload.get("summary", "")).strip()
            if not summary_text:
                logger.warning("Summary skipped: empty summary content (session_id=%s).", session_id)
                return

            msg_ids = [message.id for message in messages if message.id is not None]
            msg_id_start = msg_ids[0] if msg_ids else None
            msg_id_end = msg_ids[-1] if msg_ids else None
            time_start = self._to_utc(messages[0].created_at) if messages else None
            time_end = self._to_utc(messages[-1].created_at) if messages else None
            summary = SessionSummary(
                session_id=session_id,
                assistant_id=assistant_id,
                summary_content=summary_text,
                perspective=assistant_name,
                msg_id_start=msg_id_start,
                msg_id_end=msg_id_end,
                time_start=time_start,
                time_end=time_end,
            )
            db.add(summary)
            db.flush()

            if msg_ids:
                updated = db.query(Message).filter(Message.id.in_(msg_ids)).update(
                    {Message.summary_group_id: summary.id},
                    synchronize_session=False,
                )
                logger.info("Marked %d/%d messages with summary_group_id=%s", updated, len(msg_ids), summary.id)

            # Also mark any messages in the range that were missed (e.g. tool-call
            # chains extracted into tool_cache before trimming).  Without this,
            # a restart backfill would mark them later, shifting the conversation
            # prefix and breaking the Anthropic prompt cache.
            range_updated = db.query(Message).filter(
                Message.session_id == session_id,
                Message.id.between(msg_id_start, msg_id_end),
                Message.summary_group_id.is_(None),
            ).update(
                {Message.summary_group_id: summary.id},
                synchronize_session=False,
            )
            if range_updated:
                logger.info("Range-marked %d extra messages in [%s-%s] with summary_group_id=%s",
                            range_updated, msg_id_start, msg_id_end, summary.id)

            db.commit()
            logger.info("Summary generated OK (session_id=%s, summary_id=%s).",
                        session_id, summary.id)

            # Process extracted memories → pending_memories table
            raw_memories = parsed_payload.get("memories", [])
            if isinstance(raw_memories, list) and raw_memories:
                self._process_extracted_memories(db, raw_memories, summary.id, time_end)

            self._dispatch_core_block_signal(summary.id, assistant.id)
        except Exception:
            logger.exception("Failed to generate summary (session_id=%s).", session_id)
        finally:
            db.close()

    def _process_extracted_memories(
        self, db: Session, raw_memories: list[dict[str, Any]], summary_id: int,
        time_end: datetime | None = None,
    ) -> None:
        """Dedup extracted memories against existing ones, create as pending Memory entries."""
        from app.services.embedding_service import EmbeddingService
        from app.constants import KLASS_DEFAULTS
        from sqlalchemy import text

        embedding_service = EmbeddingService()
        valid_klasses = set(KLASS_DEFAULTS.keys())
        saved_count = 0

        for mem in raw_memories:
            if not isinstance(mem, dict):
                continue
            content = str(mem.get("content", "")).strip()
            if not content or len(content) < 4:
                continue
            # Add timestamp prefix (matching save_memory format)
            if time_end:
                ts_str = time_end.astimezone(TZ_EAST8).strftime("%Y.%m.%d %H:%M")
                content = f"[{ts_str}] {content}"
            klass = mem.get("klass", "other")
            if klass not in valid_klasses:
                klass = "other"
            raw_tags = mem.get("tags", [])
            tags = {"topic": [str(t) for t in raw_tags[:6]] if isinstance(raw_tags, list) else []}
            disclosure = str(mem.get("disclosure", "")).strip() or None
            klass_config = KLASS_DEFAULTS.get(klass, KLASS_DEFAULTS["other"])
            # Get embedding for dedup
            embedding = embedding_service.get_embedding(content)
            if embedding is None:
                continue

            # Check similarity against existing non-pending memories
            dup_sql = text("""
                SELECT id, content, 1 - (embedding <=> :query_embedding) AS similarity
                FROM memories
                WHERE embedding IS NOT NULL AND deleted_at IS NULL AND is_pending = FALSE
                ORDER BY embedding <=> :query_embedding
                LIMIT 1
            """)
            dup_result = db.execute(dup_sql, {"query_embedding": str(embedding)}).first()

            if dup_result and dup_result.similarity > 0.88:
                logger.debug(
                    "[memory_extract] Skipped duplicate: '%s' ~ '%s' (%.2f)",
                    content[:30], dup_result.content[:30], dup_result.similarity,
                )
                continue

            # Also check against existing pending memories to avoid double-pending
            dup_pending_sql = text("""
                SELECT id FROM memories
                WHERE embedding IS NOT NULL AND deleted_at IS NULL AND is_pending = TRUE
                  AND 1 - (embedding <=> :query_embedding) > 0.88
                LIMIT 1
            """)
            dup_pending = db.execute(dup_pending_sql, {"query_embedding": str(embedding)}).first()
            if dup_pending:
                logger.debug("[memory_extract] Skipped: already pending (memory_id=%s)", dup_pending.id)
                continue

            # Determine related memory
            related_id = None
            similarity = None
            if dup_result and dup_result.similarity > 0.5:
                related_id = dup_result.id
                similarity = round(dup_result.similarity, 3)

            # Create real Memory entry with is_pending=True
            memory = Memory(
                content=content,
                klass=klass,
                tags=tags,
                embedding=embedding,
                source="auto_extract",
                importance=klass_config["importance"],
                halflife_days=klass_config["halflife_days"],
                is_pending=True,
                disclosure=disclosure,
            )
            db.add(memory)
            db.flush()  # get memory.id

            # Create PendingMemory as review metadata
            pending = PendingMemory(
                memory_id=memory.id,
                content=content,
                klass=klass,
                importance=3,
                tags=tags,
                embedding=embedding,
                related_memory_id=related_id,
                similarity=similarity,
                summary_id=summary_id,
                status="pending",
            )
            db.add(pending)
            saved_count += 1

        if saved_count:
            db.commit()
            logger.info("[memory_extract] %d pending memories created (summary_id=%s)", saved_count, summary_id)

    def _dispatch_core_block_signal(self, summary_id: int, assistant_id: int) -> None:
        def _worker() -> None:
            try:
                updater = CoreBlocksUpdater(self.session_factory)
                updater.collect_signals_from_summary(summary_id, assistant_id)
            except Exception:
                logger.exception(
                    "Core block signal task failed (summary_id=%s, assistant_id=%s).",
                    summary_id,
                    assistant_id,
                )

        threading.Thread(target=_worker, daemon=True).start()


    def _resolve_primary_preset(
        self, db: Session, assistant: Assistant
    ) -> ModelPreset | None:
        if assistant.summary_model_preset_id:
            preset = db.get(ModelPreset, assistant.summary_model_preset_id)
            if preset:
                return preset
            logger.warning(
                "Configured summary preset not found (preset_id=%s).",
                assistant.summary_model_preset_id,
            )
        summary_named = db.query(ModelPreset).filter(ModelPreset.name == "summary").first()
        if summary_named:
            return summary_named
        return db.get(ModelPreset, assistant.model_preset_id)

    def _resolve_fallback_preset(
        self, db: Session, assistant: Assistant
    ) -> ModelPreset | None:
        if not assistant.summary_fallback_preset_id:
            return None
        preset = db.get(ModelPreset, assistant.summary_fallback_preset_id)
        if preset:
            return preset
        logger.warning(
            "Configured fallback summary preset not found (preset_id=%s).",
            assistant.summary_fallback_preset_id,
        )
        return None

    def _call_summary_model(
        self,
        db: Session,
        preset: ModelPreset,
        system_prompt: str,
        conversation_text: str,
    ) -> dict[str, Any]:
        import uuid as _uuid
        from app.cot_broadcaster import cot_broadcaster

        _cot_rid = str(_uuid.uuid4())
        _resp_blocks: list[dict[str, Any]] = []
        content = _call_model_raw(
            db, preset, system_prompt, conversation_text, source="摘要",
            cot_request_id=_cot_rid, cot_round_index=0, cot_emit_done=False,
            response_blocks_out=_resp_blocks,
        )
        payload = self._parse_summary_json(content)

        summary_text = str(payload.get("summary", "")).strip()
        for _ci in range(3):
            if len(summary_text) <= 1000:
                break
            logger.info(
                "[summary] Too long (%d chars), requesting rewrite round %d",
                len(summary_text), _ci + 1,
            )
            rewrite_messages = [
                {"role": "user", "content": conversation_text},
                {"role": "assistant", "content": _resp_blocks},
                {"role": "user", "content": (
                    f"你的摘要有{len(summary_text)}字，超过1000字上限。"
                    f"请精简到700字以内，保留关键事件和情绪变化。只输出JSON。"
                )},
            ]
            try:
                _retry_blocks: list[dict[str, Any]] = []
                content = _call_model_raw(
                    db, preset, system_prompt, None, source="摘要精简",
                    cot_request_id=_cot_rid, cot_round_index=_ci + 1, cot_emit_done=False,
                    messages=rewrite_messages, response_blocks_out=_retry_blocks,
                )
                new_payload = self._parse_summary_json(content)
                new_summary = str(new_payload.get("summary", "")).strip()
                if new_summary and len(new_summary) < len(summary_text):
                    logger.info("[summary] Rewrite %d → %d chars", len(summary_text), len(new_summary))
                    payload = new_payload
                    summary_text = new_summary
                    _resp_blocks = _retry_blocks
                else:
                    break
            except Exception:
                logger.warning("[summary] Rewrite round %d failed", _ci + 1, exc_info=True)
                break

        cot_broadcaster.publish({
            "type": "done", "request_id": _cot_rid, "assistant_id": 2,
        })
        return payload

    @staticmethod
    def _parse_summary_json(content: str) -> dict[str, Any]:
        logger.info("[summary] Raw model output (first 500 chars): %s", content[:500])
        cleaned = content.strip()
        if cleaned.startswith("```json"):
            cleaned = cleaned[len("```json"):]
        elif cleaned.startswith("```"):
            cleaned = cleaned[len("```"):]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-len("```")]
        cleaned = cleaned.strip()
        try:
            payload = json.loads(cleaned)
        except json.JSONDecodeError as e:
            logger.warning("[summary] JSON parse failed at pos %s: ...%s...",
                           e.pos, cleaned[max(0, (e.pos or 0) - 30):(e.pos or 0) + 30])
            try:
                decoder = json.JSONDecoder()
                payload, _ = decoder.raw_decode(cleaned)
            except json.JSONDecodeError:
                repaired = re.sub(r'"\s*\n\s*"', '",\n"', cleaned)
                repaired = re.sub(r'(\})\s*\n\s*"', '},\n"', repaired)
                repaired = re.sub(r'(\])\s*\n\s*"', '],\n"', repaired)
                repaired = re.sub(r',\s*([}\]])', r'\1', repaired)
                try:
                    payload = json.loads(repaired)
                    logger.info("[summary] JSON repair succeeded")
                except json.JSONDecodeError:
                    decoder = json.JSONDecoder()
                    payload, _ = decoder.raw_decode(repaired)
        if not isinstance(payload, dict):
            raise ValueError("Summary response is not a JSON object.")
        return payload

    def _format_messages(
        self, messages: list[Message], user_name: str, assistant_name: str
    ) -> tuple[str, list[tuple[str, str]]]:
        """Format messages for summary. Returns (text, images).

        images: list of (media_type, base64_data) tuples for multimodal API calls.
        """
        lines: list[str] = []
        images: list[tuple[str, str]] = []
        for message in messages:
            role = (message.role or "").lower()
            meta = message.meta_info or {}

            if role == "user":
                speaker = user_name
                content = (message.content or "").strip()
                # Collect image data for multimodal summary
                if message.image_data:
                    _img_data = message.image_data
                    if _img_data.startswith("media:"):
                        _fn = _img_data[6:]
                        from app.services.media_service import get_file_path
                        _path = get_file_path(_fn)
                        if _path:
                            import base64
                            _img_bytes = _path.read_bytes()
                            _b64 = base64.b64encode(_img_bytes).decode("ascii")
                            _mime = "image/png" if _img_bytes[:8] == b'\x89PNG\r\n\x1a\n' else "image/gif" if _img_bytes[:6] in (b'GIF87a', b'GIF89a') else "image/webp" if _img_bytes[:4] == b'RIFF' and _img_bytes[8:12] == b'WEBP' else "image/jpeg"
                            images.append((_mime, _b64))
                            content = f"{content}\n[图片]" if content else "[图片]"
                        else:
                            content = f"{content}\n[图片已过期]" if content else "[图片已过期]"
                    elif _img_data.startswith("data:"):
                        try:
                            _meta, _b64 = _img_data.split(",", 1)
                            _mt = _meta.split(":")[1].split(";")[0]
                            images.append((_mt, _b64))
                            content = f"{content}\n[图片]" if content else "[图片]"
                        except Exception:
                            pass
            elif role == "assistant":
                if "tool_call" in meta:
                    tc = meta["tool_call"]
                    tool_name = tc.get("tool_name", "unknown")
                    args = tc.get("arguments", {})
                    content = f"[调用工具] {tool_name}({json.dumps(args, ensure_ascii=False)})"
                    speaker = assistant_name
                else:
                    speaker = assistant_name
                    content = (message.content or "").strip()
            elif role == "tool":
                tool_name = meta.get("tool_name", "unknown")
                raw = (message.content or "").strip()
                if raw.startswith("{") and len(raw) > 500:
                    from app.services.format_converters import _build_tool_index
                    content = f"[工具结果] {_build_tool_index(tool_name, raw, len(raw))}"
                else:
                    content = f"[工具结果] {tool_name}: {raw}"
                speaker = ""
            else:
                speaker = role or "unknown"
                content = (message.content or "").strip()

            if not content:
                continue
            created_at = self._to_utc(message.created_at)
            if speaker:
                if created_at:
                    ts = created_at.astimezone(TZ_EAST8).strftime("%Y.%m.%d %H:%M")
                    lines.append(f"[{ts}] {speaker}: {content}")
                else:
                    lines.append(f"{speaker}: {content}")
            else:
                lines.append(content)
        return "\n".join(lines), images

    @staticmethod
    def _resize_images_for_summary(
        images: list[tuple[str, str]], max_dim: int = 1600,
    ) -> list[tuple[str, str]]:
        """Resize images so each dimension <= max_dim for multi-image API limits."""
        import base64
        from io import BytesIO
        try:
            from PIL import Image
        except ImportError:
            return images

        result = []
        for mime, b64 in images:
            try:
                raw = base64.b64decode(b64)
                img = Image.open(BytesIO(raw))
                w, h = img.size
                if w <= max_dim and h <= max_dim and mime != "image/gif":
                    result.append((mime, b64))
                    continue
                scale = min(max_dim / w, max_dim / h)
                new_w, new_h = int(w * scale), int(h * scale)
                img = img.resize((new_w, new_h), Image.LANCZOS)
                buf = BytesIO()
                fmt = "PNG" if mime == "image/png" else "JPEG"
                out_mime = "image/png" if fmt == "PNG" else "image/jpeg"
                if mime == "image/gif":
                    img = img.convert("RGB")
                img.save(buf, format=fmt, quality=80)
                result.append((out_mime, base64.b64encode(buf.getvalue()).decode("ascii")))
                logger.info("[summary] Resized image %dx%d → %dx%d for summary", w, h, new_w, new_h)
            except Exception:
                result.append((mime, b64))
        return result

    def _to_utc(self, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=TZ_EAST8)
        return value.astimezone(timezone.utc)

    # ── Layer merge helpers ──────────────────────────────────────────────

    def ensure_layer_needs_merge(
        self,
        db: Session,
        assistant_id: int,
        layer_type: str,
    ) -> None:
        """Ensure the layer row exists and is marked for merge (no content append)."""
        row = (
            db.query(SummaryLayer)
            .filter(
                SummaryLayer.assistant_id == assistant_id,
                SummaryLayer.layer_type == layer_type,
            )
            .first()
        )
        now = datetime.now(TZ_EAST8)
        if row:
            row.needs_merge = True
            row.updated_at = now
        else:
            db.add(SummaryLayer(
                assistant_id=assistant_id,
                layer_type=layer_type,
                content="",
                needs_merge=True,
                created_at=now,
                updated_at=now,
            ))

    def merge_layer(self, assistant_id: int, layer_type: str) -> None:
        """Call the summary model to merge/compress a layer's content. Runs in background."""
        db: Session = self.session_factory()
        try:
            row = (
                db.query(SummaryLayer)
                .filter(
                    SummaryLayer.assistant_id == assistant_id,
                    SummaryLayer.layer_type == layer_type,
                )
                .first()
            )
            if not row:
                return

            # Query pending (unconsumed) summaries for this layer
            pending = (
                db.query(SessionSummary)
                .filter(
                    SessionSummary.assistant_id == assistant_id,
                    SessionSummary.merged_into == layer_type,
                    SessionSummary.merged_at_version.is_(None),
                    SessionSummary.deleted_at.is_(None),
                )
                .order_by(SessionSummary.created_at.asc())
                .all()
            )

            if not pending and not row.needs_merge:
                return
            # If no pending summaries and no existing content, just clear the flag
            if not pending and not (row.content and row.content.strip()):
                row.needs_merge = False
                db.commit()
                return

            # Build merge input: existing clean content + pending summaries
            # For longterm: try to use daily's compressed version instead of raw summaries
            parts: list[str] = []
            if row.content and row.content.strip():
                parts.append(row.content.strip())

            if layer_type == "longterm" and pending:
                # Look up daily history to find compressed versions
                pending_id_set = {s.id for s in pending}
                daily_histories = (
                    db.query(SummaryLayerHistory)
                    .filter(
                        SummaryLayerHistory.layer_type == "daily",
                        SummaryLayerHistory.assistant_id == assistant_id,
                        SummaryLayerHistory.merged_summary_ids.isnot(None),
                    )
                    .order_by(SummaryLayerHistory.version.desc())
                    .all()
                )
                claimed: set[int] = set()
                for dh in daily_histories:
                    try:
                        merged_ids = set(json.loads(dh.merged_summary_ids))
                    except Exception:
                        continue
                    overlap = pending_id_set & merged_ids
                    if overlap and dh.content and dh.content.strip():
                        parts.append(dh.content.strip())
                        claimed |= overlap
                # Add remaining raw summaries not covered by daily history
                for s in pending:
                    if s.id not in claimed and s.summary_content and s.summary_content.strip():
                        parts.append(s.summary_content.strip())
            else:
                for s in pending:
                    if s.summary_content and s.summary_content.strip():
                        parts.append(s.summary_content.strip())

            merge_input = "\n\n".join(parts)

            if not merge_input.strip():
                row.needs_merge = False
                db.commit()
                return

            # Fast path: if layer has no existing content, just copy pending text directly (no LLM rewrite)
            existing_content = (row.content or "").strip()
            if not existing_content and pending:
                raw_parts = []
                for s in pending:
                    if s.summary_content and s.summary_content.strip():
                        raw_parts.append(s.summary_content.strip())
                if raw_parts:
                    row.content = "\n\n".join(raw_parts)
                    new_version = (row.version or 0) + 1
                    row.version = new_version
                    row.needs_merge = False
                    new_ids = [s.id for s in pending]
                    for s in pending:
                        s.merged_at_version = new_version
                    db.commit()
                    logger.info("[merge_layer] %s fast-path: copied %d summaries directly (assistant_id=%s)", layer_type, len(raw_parts), assistant_id)
                    return

            assistant = db.get(Assistant, assistant_id)
            if not assistant:
                return
            preset = self._resolve_primary_preset(db, assistant)
            if not preset:
                return

            user_profile = db.query(UserProfile).first()
            user_name = user_profile.nickname if user_profile and user_profile.nickname else "User"
            assistant_name = assistant.name or "Assistant"

            budget_key = f"summary_budget_{layer_type}"
            budget_row = db.query(Settings).filter(Settings.key == budget_key).first()
            budget_tokens = int(budget_row.value) if budget_row else (2000 if layer_type != "recent" else 2000)
            max_chars = budget_tokens // 2

            if layer_type == "daily":
                prompt = (
                    f"你是{assistant_name}，{user_name}的AI伴侣。你在整理自己最近的记忆。\n\n"
                    f"请将以下内容合并为一段连贯的近期回顾：\n"
                    f"- 按时间先后顺序整理\n"
                    f"- 保留关键事件、情绪变化、重要对话内容\n"
                    f"- 去除重复信息\n"
                    f"- 时间用具体描述如\"下午3点\"，不要用\"刚才\"\"今天\"等相对时间\n"
                    f"- 亲密场景只保留偏好和情绪，不保留具体描写\n"
                    f"- 控制在{max_chars}字以内\n"
                    f"- \"我\"= {assistant_name}\n\n"
                    f"只输出合并后的文本，不要JSON，不要多余解释。"
                )
            else:
                prompt = (
                    f"你是{assistant_name}，{user_name}的AI伴侣。你在整理自己的长期记忆。\n\n"
                    f"请将以下内容整合为一段长期记忆：\n"
                    f"- 按时间先后顺序，较早的内容适当压缩\n"
                    f"- 重点保留：关系变化、她表达过的偏好和在意的事、重大事件、承诺和约定\n"
                    f"- 日常闲聊如果不影响理解关系可以省略\n"
                    f"- 亲密场景只保留偏好和情绪，不保留具体描写\n"
                    f"- 时间用具体描述如\"2月25日晚上\"，不要用\"昨天\"\"前几天\"等相对时间\n"
                    f"- 控制在{max_chars}字以内\n"
                    f"- \"我\"= {assistant_name}\n\n"
                    f"只输出合并后的文本，不要JSON，不要多余解释。"
                )

            merged = None
            try:
                merged = _call_model_raw(db, preset, prompt, merge_input, timeout=300.0, source="合并")
                merged = (merged or "").strip()
            except Exception:
                logger.warning(
                    "[merge_layer] Primary preset failed for %s assistant_id=%s, trying fallback",
                    layer_type, assistant_id,
                )
            if not merged:
                fallback = self._resolve_fallback_preset(db, assistant)
                if fallback and fallback.id != preset.id:
                    try:
                        merged = _call_model_raw(db, fallback, prompt, merge_input, timeout=300.0, source="合并")
                        merged = (merged or "").strip()
                        if merged:
                            logger.info(
                                "[merge_layer] %s merged via fallback for assistant_id=%s",
                                layer_type, assistant_id,
                            )
                    except Exception:
                        logger.exception(
                            "[merge_layer] Fallback also failed for %s assistant_id=%s",
                            layer_type, assistant_id,
                        )
            if merged:
                new_ids = [s.id for s in pending]
                # Save current clean content to history before overwriting
                # (skip if pre-merge content is empty — no point saving an empty snapshot)
                # NOTE: merged_summary_ids is NOT stored here — this is the PRE-merge
                # snapshot and doesn't contain the new summaries. The correct association
                # is created by daily_merge_to_longterm when transferring to longterm.
                old_content = (row.content or "").strip()
                if old_content:
                    db.add(SummaryLayerHistory(
                        summary_layer_id=row.id,
                        layer_type=row.layer_type,
                        assistant_id=row.assistant_id,
                        content=row.content or "",
                        version=row.version,
                    ))
                row.version += 1
                row.content = merged
                row.needs_merge = False
                row.token_count = len(merged)
                row.updated_at = datetime.now(TZ_EAST8)
                # Mark pending summaries as consumed
                for s in pending:
                    s.merged_at_version = row.version
                db.commit()
                logger.info(
                    "[merge_layer] %s merged for assistant_id=%s (%d chars, v%d)",
                    layer_type, assistant_id, len(merged), row.version,
                )
            else:
                logger.warning("[merge_layer] Empty merge result for %s assistant_id=%s", layer_type, assistant_id)
        except Exception:
            logger.exception("[merge_layer] Failed for %s assistant_id=%s", layer_type, assistant_id)
        finally:
            db.close()

    def merge_layers_async(self, assistant_id: int, layer_types: tuple[str, ...] | None = None) -> None:
        """Merge specified layers in background thread."""
        if layer_types is None:
            layer_types = ("daily", "longterm")

        def _worker() -> None:
            for lt in layer_types:
                self.merge_layer(assistant_id, lt)

        threading.Thread(target=_worker, daemon=True).start()

    def daily_merge_to_longterm(self, assistant_id: int) -> None:
        """Move daily compressed content into longterm (called by midnight cron).

        Strategy: daily's clean merged content feeds into longterm.
        Summaries transfer from daily → longterm with merged_at_version set
        (already consumed by daily, will be consumed again by longterm merge).
        """
        db: Session = self.session_factory()
        try:
            daily = (
                db.query(SummaryLayer)
                .filter(
                    SummaryLayer.assistant_id == assistant_id,
                    SummaryLayer.layer_type == "daily",
                )
                .first()
            )
            if not daily:
                return

            # If daily still needs merge, merge it first
            if daily.needs_merge:
                self.merge_layer(assistant_id, "daily")
                db.refresh(daily)

            has_daily_content = daily.content and daily.content.strip()

            # Find all summaries currently assigned to daily
            daily_summaries = (
                db.query(SessionSummary)
                .filter(
                    SessionSummary.assistant_id == assistant_id,
                    SessionSummary.merged_into == "daily",
                )
                .all()
            )

            if not has_daily_content and not daily_summaries:
                return

            # Ensure longterm row exists
            longterm = (
                db.query(SummaryLayer)
                .filter(
                    SummaryLayer.assistant_id == assistant_id,
                    SummaryLayer.layer_type == "longterm",
                )
                .first()
            )
            now = datetime.now(TZ_EAST8)
            if not longterm:
                longterm = SummaryLayer(
                    assistant_id=assistant_id,
                    layer_type="longterm",
                    content="",
                    needs_merge=True,
                    created_at=now,
                    updated_at=now,
                )
                db.add(longterm)
                db.flush()

            # Append daily's clean content to longterm content (for next merge)
            if has_daily_content:
                existing = (longterm.content or "").strip()
                if existing:
                    longterm.content = existing + "\n\n" + daily.content.strip()
                else:
                    longterm.content = daily.content.strip()

            longterm.needs_merge = True
            longterm.updated_at = now

            # Transfer summaries: merged_into → "longterm" (mark consumed later after merge)
            daily_summary_ids = [s.id for s in daily_summaries]
            for s in daily_summaries:
                s.merged_into = "longterm"
                s.merged_at_version = None  # will be set after longterm merge

            # Save daily content to daily history before clearing
            if has_daily_content:
                db.add(SummaryLayerHistory(
                    summary_layer_id=daily.id,
                    layer_type="daily",
                    assistant_id=daily.assistant_id,
                    content=daily.content or "",
                    version=daily.version,
                    merged_summary_ids=json.dumps(daily_summary_ids) if daily_summary_ids else None,
                ))

            # Clear daily — increment version to stay monotonic (don't reset to 1)
            daily.content = ""
            daily.needs_merge = False
            daily.version += 1
            daily.updated_at = now
            db.commit()

            # Now merge longterm (existing longterm content + daily content appended above)
            # merge_layer will pick up the transferred summaries (merged_at_version=None)
            # and set their merged_at_version to the new longterm version
            self.merge_layer(assistant_id, "longterm")
            logger.info("[daily_merge_to_longterm] Completed for assistant_id=%s", assistant_id)
        except Exception:
            logger.exception("[daily_merge_to_longterm] Failed for assistant_id=%s", assistant_id)
        finally:
            db.close()


# ── Module-level midnight cron ───────────────────────────────────────────────


async def daily_merge_cron() -> None:
    """Run every 7 days at midnight (Beijing time): merge daily → longterm for all assistants."""
    from app.database import SessionLocal
    from app.models.models import Settings

    MERGE_INTERVAL_DAYS = 7

    while True:
        try:
            now_bj = datetime.now(TZ_EAST8)

            # Read last merge date from settings
            db = SessionLocal()
            try:
                last_merge_row = db.query(Settings).filter(Settings.key == "last_weekly_merge").first()
                if last_merge_row:
                    last_merge_date = datetime.fromisoformat(last_merge_row.value).date()
                else:
                    # First run: set today as last merge, next merge in 7 days
                    last_merge_date = now_bj.date()
                    db.add(Settings(key="last_weekly_merge", value=now_bj.date().isoformat()))
                    db.commit()
            finally:
                db.close()

            next_merge_date = last_merge_date + timedelta(days=MERGE_INTERVAL_DAYS)
            next_merge_dt = datetime.combine(next_merge_date, datetime.min.time()).replace(tzinfo=TZ_EAST8)

            if now_bj >= next_merge_dt:
                wait_seconds = 0
            else:
                wait_seconds = (next_merge_dt - now_bj).total_seconds()

            logger.info("[daily_merge_cron] Next run in %.0f seconds", wait_seconds)
            await asyncio.sleep(wait_seconds)

            # Re-check after sleep: manual merge may have reset the countdown
            db = SessionLocal()
            try:
                recheck_row = db.query(Settings).filter(Settings.key == "last_weekly_merge").first()
                if recheck_row:
                    recheck_date = datetime.fromisoformat(recheck_row.value).date()
                    recheck_next = recheck_date + timedelta(days=MERGE_INTERVAL_DAYS)
                    if datetime.now(TZ_EAST8).date() < recheck_next:
                        logger.info("[daily_merge_cron] Skipped — manual merge already happened")
                        continue
            finally:
                db.close()

            logger.info("[daily_merge_cron] Starting weekly merge")
            db = SessionLocal()
            try:
                assistants = (
                    db.query(Assistant)
                    .filter(Assistant.deleted_at.is_(None))
                    .all()
                )
                assistant_ids = [a.id for a in assistants]
            finally:
                db.close()

            service = SummaryService(SessionLocal)
            for aid in assistant_ids:
                try:
                    # daily_merge_to_longterm 内部会调外部 API (拿 summary), 同步 blocking IO
                    # 必须 to_thread 隔离, 否则整个 event loop 被卡住, 所有 HTTP 请求 / TG webhook 全部排队
                    await asyncio.to_thread(service.daily_merge_to_longterm, aid)
                except Exception:
                    logger.exception("[daily_merge_cron] Failed for assistant_id=%s", aid)

            # Record merge date
            db = SessionLocal()
            try:
                row = db.query(Settings).filter(Settings.key == "last_weekly_merge").first()
                if row:
                    row.value = datetime.now(TZ_EAST8).date().isoformat()
                else:
                    db.add(Settings(key="last_weekly_merge", value=datetime.now(TZ_EAST8).date().isoformat()))
                db.commit()
            finally:
                db.close()

            logger.info("[daily_merge_cron] Weekly merge completed for %d assistants", len(assistant_ids))
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("[daily_merge_cron] Unexpected error")
            await asyncio.sleep(60)
