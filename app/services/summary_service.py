from __future__ import annotations

import asyncio
import json
import logging
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
    db: Session, preset: ModelPreset, system_prompt: str, user_text: str,
    *, timeout: float | None = None,
) -> str:
    """Call a model preset and return raw text response."""
    api_provider = db.get(ApiProvider, preset.api_provider_id)
    if not api_provider:
        raise ValueError(f"API provider not found for preset_id={preset.id}")

    base_url = api_provider.base_url
    if base_url.endswith("/chat/completions"):
        base_url = base_url[: -len("/chat/completions")]
        if not base_url.endswith("/v1"):
            base_url = f"{base_url.rstrip('/')}/v1"
    if api_provider.auth_type == "oauth_token":
        _anth_kwargs: dict[str, Any] = {
            "auth_token": api_provider.api_key,
            "default_headers": {
                "anthropic-beta": "claude-code-20250219,oauth-2025-04-20,fine-grained-tool-streaming-2025-05-14",
                "user-agent": "claude-cli/2.1.2 (external, cli)",
                "x-app": "cli",
            },
        }
        if timeout is not None:
            _anth_kwargs["timeout"] = timeout
        anth_client = anthropic.Anthropic(**_anth_kwargs)
        _summary_tb = preset.thinking_budget or 0
        anth_kwargs: dict[str, Any] = {
            "model": preset.model_name,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_text}],
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
        anth_response = anth_client.messages.create(**anth_kwargs)
        content = ""
        for block in anth_response.content:
            if block.type == "text":
                content += block.text
    else:
        _oai_kwargs: dict[str, Any] = {"api_key": api_provider.api_key, "base_url": base_url}
        if timeout is not None:
            _oai_kwargs["timeout"] = timeout
        oai_client = OpenAI(**_oai_kwargs)
        params: dict[str, Any] = {
            "model": preset.model_name,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text},
            ],
            "max_tokens": preset.max_tokens,
        }
        if preset.temperature is not None:
            params["temperature"] = preset.temperature
        if preset.top_p is not None:
            params["top_p"] = preset.top_p
        _summary_tb_oai = preset.thinking_budget or 0
        if _summary_tb_oai > 0:
            params["extra_body"] = {"reasoning": {"max_tokens": _summary_tb_oai}}
        oai_response = oai_client.chat.completions.create(**params)
        if not oai_response.choices:
            raise ValueError("Response contained no choices.")
        msg = oai_response.choices[0].message
        content = msg.content or ""
    return content


def translate_text(db: Session, text: str) -> str:
    """Translate text to Chinese using the summary fallback model."""
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
    return _call_model_raw(db, preset, system_prompt, text)


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

            trimmed_text = self._format_messages(trimmed_msgs or messages, user_name, assistant_name)
            if not trimmed_text.strip():
                logger.warning("Summary skipped: no usable message content (session_id=%s).", session_id)
                return

            conversation_text = trimmed_text

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
【情绪】情绪变化（可写 A→B，如"焦虑→平静"）
【摘要】
摘要正文

重点记录：聊了什么、做了什么决定、情绪变化、新暴露的信息。
时间用具体描述如"2.5晚上20点左右"，不要用"刚才""昨天"这类相对时间。
亲密场景：只记场景设定、她表达的偏好、情绪变化，不记具体行为描写。
如果对话中有工具调用（如存储记忆、搜索记忆等），在摘要正文中自然地概括，例如'我存储了一条关于xxx的记忆'。
单条摘要严格控制在500字以内。只记结论、决定和关键转折，省略过程性对话。

任务二：记忆提取
从对话中提取值得长期记住的信息。
- 对话中已通过 save_memory 存过的不要重复提取
- 每条记忆不超过100字，用第一人称记录
- 时间戳由后端自动添加，content里不要写日期时间，除非记录的是过去发生的事
- klass：identity / relationship / bond / conflict / fact / preference / health / task / other
- 日常闲聊、没有新信息的内容不需要提取
- 没有值得提取的就返回空数组
- tags：给每条记忆加1-3个短关键词标签，方便检索

任务三：情绪标签
根据对话末尾{user_name}的状态判断当前情绪，从以下选一个：
sad/angry/anxious/tired/emo/happy/flirty/proud/calm

输出格式：
{{"summary": "...", "memories": [{{"content": "...", "klass": "...", "tags": ["标签1", "标签2"]}}, ...], "mood_tag": "..."}}
memories 为空时写 "memories": []
""".strip()

            if is_chat_session:
                system_prompt = base_persona + "\n\n" + task_instructions if base_persona else task_instructions
            else:
                # Group session: no mood_tag
                group_task = task_instructions.replace(
                    f'根据对话末尾{user_name}的状态判断当前情绪，从以下选一个：\nsad/angry/anxious/tired/emo/happy/flirty/proud/calm',
                    '',
                ).replace(', "mood_tag": "..."', '')
                system_prompt = base_persona + "\n\n" + group_task if base_persona else group_task

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
                return

            summary_text = str(parsed_payload.get("summary", "")).strip()
            if not summary_text:
                logger.warning("Summary skipped: empty summary content (session_id=%s).", session_id)
                return

            valid_moods = {
                "sad",
                "angry",
                "anxious",
                "tired",
                "emo",
                "happy",
                "flirty",
                "proud",
                "calm",
            }
            mood_tag = None
            if is_chat_session:
                mood_tag_raw = str(parsed_payload.get("mood_tag", "")).strip().lower()
                mood_tag = mood_tag_raw if mood_tag_raw in valid_moods else None

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
                mood_tag=mood_tag,
            )
            db.add(summary)
            db.flush()

            # Clear manual mood flag when auto-summary detects mood
            if mood_tag:
                manual_row = db.query(Settings).filter(Settings.key == "mood_manual").first()
                if manual_row:
                    manual_row.value = "false"
                else:
                    db.add(Settings(key="mood_manual", value="false"))

            if msg_ids:
                updated = db.query(Message).filter(Message.id.in_(msg_ids)).update(
                    {Message.summary_group_id: summary.id},
                    synchronize_session=False,
                )
                logger.info("Marked %d/%d messages with summary_group_id=%s", updated, len(msg_ids), summary.id)

            db.commit()
            logger.info("Summary generated OK (session_id=%s, summary_id=%s, mood=%s).",
                        session_id, summary.id, mood_tag)

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
        content = _call_model_raw(db, preset, system_prompt, conversation_text)
        cleaned_content = content.strip()
        if cleaned_content.startswith("```json"):
            cleaned_content = cleaned_content[len("```json") :]
        elif cleaned_content.startswith("```"):
            cleaned_content = cleaned_content[len("```") :]
        if cleaned_content.endswith("```"):
            cleaned_content = cleaned_content[: -len("```")]
        cleaned_content = cleaned_content.strip()
        # Handle "Extra data" — model may return multiple JSON objects
        try:
            payload = json.loads(cleaned_content)
        except json.JSONDecodeError:
            decoder = json.JSONDecoder()
            payload, _ = decoder.raw_decode(cleaned_content)
        if not isinstance(payload, dict):
            raise ValueError("Summary response is not a JSON object.")
        return payload

    def _format_messages(
        self, messages: list[Message], user_name: str, assistant_name: str
    ) -> str:
        lines: list[str] = []
        for message in messages:
            role = (message.role or "").lower()
            meta = message.meta_info or {}

            if role == "user":
                speaker = user_name
                content = (message.content or "").strip()
            elif role == "assistant":
                if "tool_call" in meta:
                    # Tool call placeholder — format as tool invocation
                    tc = meta["tool_call"]
                    tool_name = tc.get("tool_name", "unknown")
                    args = tc.get("arguments", {})
                    content = f"[调用工具] {tool_name}({json.dumps(args, ensure_ascii=False)})"
                    speaker = assistant_name
                else:
                    speaker = assistant_name
                    content = (message.content or "").strip()
            elif role == "tool":
                # Tool result — keep full content for summary model
                tool_name = meta.get("tool_name", "unknown")
                raw = (message.content or "").strip()
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
                # Tool results without speaker prefix
                lines.append(content)
        return "\n".join(lines)

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
            budget_tokens = int(budget_row.value) if budget_row else (800 if layer_type != "recent" else 2000)
            max_chars = budget_tokens // 2

            if layer_type == "daily":
                prompt = (
                    f"你是{assistant_name}，{user_name}的AI伴侣。你在整理自己今天的记忆。\n\n"
                    f"请将以下内容合并为一段连贯的当日回顾：\n"
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
                merged = _call_model_raw(db, preset, prompt, merge_input, timeout=60.0)
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
                        merged = _call_model_raw(db, fallback, prompt, merge_input, timeout=60.0)
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
                # Cleanup history older than 7 days
                cutoff = datetime.now(TZ_EAST8) - timedelta(days=7)
                db.query(SummaryLayerHistory).filter(
                    SummaryLayerHistory.summary_layer_id == row.id,
                    SummaryLayerHistory.created_at < cutoff,
                ).delete()
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

            # Transfer summaries: merged_into → "longterm"
            # Set merged_at_version = longterm.version + 1 (will be the new version after merge)
            # so merge_layer won't re-fetch them as pending raw summaries
            next_lt_version = longterm.version + 1
            for s in daily_summaries:
                s.merged_into = "longterm"
                s.merged_at_version = next_lt_version

            # Save daily content to daily history before clearing
            if has_daily_content:
                summary_ids = [s.id for s in daily_summaries]
                db.add(SummaryLayerHistory(
                    summary_layer_id=daily.id,
                    layer_type="daily",
                    assistant_id=daily.assistant_id,
                    content=daily.content or "",
                    version=daily.version,
                    merged_summary_ids=json.dumps(summary_ids) if summary_ids else None,
                ))

            # Clear daily — increment version to stay monotonic (don't reset to 1)
            daily.content = ""
            daily.needs_merge = False
            daily.version += 1
            daily.updated_at = now
            db.commit()

            # Now merge longterm (existing longterm content + daily content appended above)
            self.merge_layer(assistant_id, "longterm")
            logger.info("[daily_merge_to_longterm] Completed for assistant_id=%s", assistant_id)
        except Exception:
            logger.exception("[daily_merge_to_longterm] Failed for assistant_id=%s", assistant_id)
        finally:
            db.close()


# ── Module-level midnight cron ───────────────────────────────────────────────


async def daily_merge_cron() -> None:
    """Run at midnight (Beijing time) each day: merge daily → longterm for all assistants."""
    from app.database import SessionLocal

    while True:
        try:
            now_bj = datetime.now(TZ_EAST8)
            tomorrow = (now_bj + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            wait_seconds = (tomorrow - now_bj).total_seconds()
            logger.info("[daily_merge_cron] Next run in %.0f seconds", wait_seconds)
            await asyncio.sleep(wait_seconds)

            logger.info("[daily_merge_cron] Starting midnight merge")
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
                    service.daily_merge_to_longterm(aid)
                except Exception:
                    logger.exception("[daily_merge_cron] Failed for assistant_id=%s", aid)

            logger.info("[daily_merge_cron] Completed for %d assistants", len(assistant_ids))
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("[daily_merge_cron] Unexpected error")
            await asyncio.sleep(60)
