from __future__ import annotations

import json
import re
import logging
import time
import threading
import uuid
from collections.abc import Callable, Iterable
from datetime import datetime, timezone, timedelta
from typing import Any

import requests
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy import text

from app.models.models import (
    ApiProvider, Assistant, ChatSession, Memory, Message,
    Settings,
)
from app.database import SessionLocal
from app.cot_broadcaster import cot_broadcaster
from app.utils import TZ_EAST8
from app.services.memory_service import MemoryService, ToolCall
from app.services.format_converters import (
    _oai_tools_to_anthropic,
    _extract_reasoning_delta,
    _apply_cache_control_oai,
    _reapply_oai_message_bp_at_idx,
    _oai_messages_to_anthropic,
    estimate_tokens,
)

from app.services.chat.response_cleaner import (
    extract_used_memory_ids,
    split_think_and_text,
    clean_response_text,
    clean_tool_call_text,
    inject_anthropic_cache_breakpoint,
    build_anthropic_kwargs,
    build_openai_kwargs,
    finalize_tool_calls_acc,
    extract_thinking_blocks,
    _uses_adaptive_thinking,
    parse_anthropic_response,
    parse_openai_response,
)
from app.services.chat.tool_executor import (
    execute_tool as _exec_tool,
    sanitize_tool_args as _sanitize_args,
)
from app.services.chat.post_reply import (
    maybe_trigger_post_reply as _maybe_trigger_post_reply,
    trigger_summary as _trigger_summary_fn,
)
from app.services.chat.config_helpers import (
    get_prompt_setting as _get_prompt_setting,
    normalize_anthropic_base_url as _normalize_anthropic_base_url,
)
from app.services.chat.persistence import ChatPersistence, content_to_storage
from app.services.chat.request_builder import build_api_call_params
from app.services.chat.streaming import stream_chat_completion as run_stream_chat_completion

logger = logging.getLogger(__name__)

DEFAULT_DIALOGUE_RETAIN_BUDGET = 8000
DEFAULT_DIALOGUE_TRIGGER_THRESHOLD = 16000
DEFAULT_SUMMARY_BUDGET_LONGTERM = 2000
DEFAULT_SUMMARY_BUDGET_DAILY = 2000
DEFAULT_SUMMARY_BUDGET_RECENT = 2000


from app.services.chat.prompt_defaults import (
    DEFAULT_IMPORTANT_NOTICE,
    DEFAULT_LONG_MODE,
    DEFAULT_LONG_MODE_LEGACY,
    DEFAULT_LONG_MODE_SUFFIX,
    DEFAULT_SHORT_MODE,
    DEFAULT_SHORT_MODE_LEGACY,
    DEFAULT_SHORT_MODE_SUFFIX,
)
class ChatService:
    interactive_tools = {
        "save_memory",
        "update_memory",
        "delete_memory",
        "diary",
        "web",
        "submit_reflection",
        "ios_control",
    }
    silent_tools = {"list_memories", "search_memory", "search_summary", "get_summary_by_id", "search_chat_history", "view", "read_yoru_memory"}
    # Tools that must be executed on the client side (terminal CLI)
    client_side_tools = {"pc_control"}
    tool_display_names = {
        "save_memory": "创建记忆",
        "update_memory": "更新记忆",
        "delete_memory": "删除记忆",
        "diary": "交换日记",
        "list_memories": "列出记忆",
        "search_memory": "搜索记忆",
        "search_summary": "搜索对话摘要",
        "get_summary_by_id": "查看摘要",
        "search_chat_history": "搜索聊天记录",
        "web": "搜索/读取网页",
        "run_bash": "执行命令",
        "read_file": "读取文件",
        "write_file": "写入文件",
        "submit_reflection": "记忆反思",
        "reminder": "闹钟管理",
        "view": "查看图片/文件",
        "ios_control": "操作手机",
    }

    def __init__(
        self,
        db: Session,
        assistant_name: str,
        session_factory: sessionmaker | None = None,
        assistant_id: int | None = None,
        source: str | None = None,
    ) -> None:
        self.db = db
        self.assistant_name = assistant_name
        self.assistant_id = assistant_id
        self.source = source
        self.memory_service = MemoryService(db)
        self._persistence = ChatPersistence(db, assistant_id, source)
        self.session_factory = session_factory or SessionLocal
        self.proactive_extra_prompt: str | None = None
        self.api_timeout: float | None = None
        self._trimmed_messages: list[dict[str, Any]] = []
        self._trimmed_message_ids: list[int] = []
        budgets = self._load_context_budgets()
        self.dialogue_retain_budget = budgets[0]
        self.dialogue_trigger_threshold = budgets[1]
        self.summary_budget_longterm = budgets[2]
        self.summary_budget_daily = budgets[3]
        self.summary_budget_recent = budgets[4]

    def _persistence_context(self) -> ChatPersistence:
        self._persistence.db = self.db
        self._persistence.assistant_id = self.assistant_id
        self._persistence.source = self.source
        return self._persistence

    def _load_context_budgets(self) -> tuple[int, int, int, int, int]:
        retain_budget = DEFAULT_DIALOGUE_RETAIN_BUDGET
        trigger_threshold = DEFAULT_DIALOGUE_TRIGGER_THRESHOLD
        sb_longterm = DEFAULT_SUMMARY_BUDGET_LONGTERM
        sb_daily = DEFAULT_SUMMARY_BUDGET_DAILY
        sb_recent = DEFAULT_SUMMARY_BUDGET_RECENT
        try:
            rows = (
                self.db.query(Settings)
                .filter(
                    Settings.key.in_([
                        "dialogue_retain_budget", "dialogue_trigger_threshold",
                        "summary_budget_longterm", "summary_budget_daily", "summary_budget_recent",
                    ])
                )
                .all()
            )
            kv = {row.key: row.value for row in rows}
            retain_budget = self._safe_int(
                kv.get("dialogue_retain_budget"), DEFAULT_DIALOGUE_RETAIN_BUDGET
            )
            trigger_threshold = self._safe_int(
                kv.get("dialogue_trigger_threshold"),
                DEFAULT_DIALOGUE_TRIGGER_THRESHOLD,
            )
            sb_longterm = self._safe_int(
                kv.get("summary_budget_longterm"), DEFAULT_SUMMARY_BUDGET_LONGTERM
            )
            sb_daily = self._safe_int(
                kv.get("summary_budget_daily"), DEFAULT_SUMMARY_BUDGET_DAILY
            )
            sb_recent = self._safe_int(
                kv.get("summary_budget_recent"), DEFAULT_SUMMARY_BUDGET_RECENT
            )
        except Exception:
            logger.exception("Failed to load context budget settings, using defaults.")
        retain_budget = max(1, retain_budget)
        trigger_threshold = max(retain_budget, trigger_threshold)
        sb_longterm = max(200, sb_longterm)
        sb_daily = max(200, sb_daily)
        sb_recent = max(500, sb_recent)
        return retain_budget, trigger_threshold, sb_longterm, sb_daily, sb_recent

    @staticmethod
    def _safe_int(raw_value: Any, default: int) -> int:
        try:
            return int(str(raw_value).strip())
        except Exception:
            return default

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        return estimate_tokens(text)

    def _consume_diary_notifications(self) -> str:
        """Check for un-notified user diaries and mark them notified.
        Returns hint text to append after user message, or empty string."""
        from app.models.models import Diary
        from sqlalchemy import or_
        now = datetime.now(TZ_EAST8)
        try:
            diaries = (
                self.db.query(Diary)
                .filter(
                    Diary.deleted_at.is_(None),
                    Diary.author == "user",
                    Diary.notified_at.is_(None),
                    or_(
                        Diary.unlock_at.is_(None),
                        Diary.unlock_at <= now,
                    ),
                )
                .order_by(Diary.created_at.asc())
                .all()
            )
            if not diaries:
                return ""
            lines = []
            for d in diaries:
                title = d.title or "无标题"
                lines.append(f"[系统提醒] 用户给你写了一封信「{title}」，现在可以打开看了。用 diary 工具读取（action=read, diary_id={d.id}）。")
                d.notified_at = now
            self.db.commit()
            return "\n".join(lines)
        except Exception:
            logger.exception("[diary-notify] Failed to consume diary notifications")
            return ""

    def chat_completion(
        self,
        session_id: int,
        messages: list[dict[str, Any]],
        tool_calls: Iterable[ToolCall],
        event_callback: Callable[[dict[str, Any]], None] | None = None,
        background_tasks: BackgroundTasks | None = None,
        short_mode: bool = False,
    ) -> list[dict[str, Any]]:
        from app.services.generation_coordinator import set_current_request_id
        request_id = str(uuid.uuid4())
        set_current_request_id(request_id)
        start_time = time.monotonic()
        self._total_prompt_tokens = 0
        self._total_completion_tokens = 0
        self._total_input_raw = 0
        # Persist all NEW user messages (those without a DB id)
        for msg in messages:
            if msg.get("role") == "user" and not msg.get("id"):
                user_content = msg.get("content", "")
                has_content = bool(user_content) if isinstance(user_content, list) else bool(user_content and user_content.strip())
                if has_content:
                    self._persist_message(session_id, "user", user_content, {}, request_id=request_id)
        all_trimmed_messages: list[dict[str, Any]] = []
        all_trimmed_message_ids: list[int] = []
        round_index = 0
        if tool_calls:
            pending_tool_calls = list(tool_calls)
        else:
            pending_tool_calls = list(self._fetch_next_tool_calls(
                messages, session_id, short_mode=short_mode,
                request_id=request_id, round_index=round_index,
            ))
            all_trimmed_messages.extend(self._trimmed_messages)
            all_trimmed_message_ids.extend(self._trimmed_message_ids)
            # Broadcast injected memories (non-streaming path)
            if getattr(self, "_last_recall_results", None):
                cot_broadcaster.publish({
                    "type": "injected_memories",
                    "request_id": request_id,
                    "memories": [{"id": m.get("id"), "content": m.get("content", ""), "recall_source": m.get("recall_source")} for m in self._last_recall_results],
                    "assistant_id": self.assistant_id,
                })
        while pending_tool_calls:
            # Execute ALL tool calls in this batch before calling the API again
            for tool_call in pending_tool_calls:
                tool_name = tool_call.name
                _is_interactive = tool_name in self.interactive_tools
                # diary: only show notification on write (read/list is silent)
                if tool_name == "diary" and tool_call.arguments.get("action") != "write":
                    _is_interactive = False
                if _is_interactive and event_callback:
                    display_name = self.tool_display_names.get(tool_name, tool_name)
                    sanitized_args = _sanitize_args(tool_call)
                    event_callback(
                        {
                            "event": "tool_call_start",
                            "display_name": display_name,
                            "tool_name": tool_name,
                            "arguments": sanitized_args,
                        }
                    )
                # Write tool_use COT block before execution (paired with tool_result)
                self._write_cot_block(
                    request_id, round_index, "tool_use",
                    json.dumps(tool_call.arguments, ensure_ascii=False),
                    tool_name=tool_name,
                )
                try:
                    self._persist_tool_call(session_id, tool_call)
                except Exception as e:
                    logger.error("Failed to persist tool call %s: %s", tool_name, e)
                    try:
                        self.db.rollback()
                    except Exception:
                        pass
                try:
                    # Inject seen_memory_ids for search_memory related
                    if tool_call.name == "search_memory" and tool_call.arguments.get("action") == "related":
                        tool_call.arguments["_seen_memory_ids"] = list(getattr(self, "_seen_memory_ids", set()))
                    # Block switch_channel in proactive mode
                    if tool_call.name == "switch_channel" and self.proactive_extra_prompt:
                        tool_result = {"result": "当前为主动消息模式，不可使用switch_channel。请在回复开头使用[VIA:telegram]或[VIA:qq]切换渠道。"}
                    else:
                        tool_result = _exec_tool(tool_call, db=self.db, memory_service=self.memory_service, assistant_name=self.assistant_name, current_assistant_id=getattr(self, "_current_assistant_id", None), current_session_id=getattr(self, "_current_session_id", None))
                    # Update seen_memory_ids from related results
                    if isinstance(tool_result, dict) and "_related_ids" in tool_result:
                        if not hasattr(self, "_seen_memory_ids"):
                            self._seen_memory_ids = set()
                        self._seen_memory_ids.update(tool_result.pop("_related_ids"))
                    # Handle switch_channel
                    if isinstance(tool_result, dict) and "_switch_channel" in tool_result:
                        self._switched_channel = tool_result.pop("_switch_channel")
                        self.source = self._switched_channel
                        self.short_mode = (self._switched_channel in ("qq", "wechat"))
                except Exception as e:
                    logger.error("Tool execution error (%s): %s", tool_name, e)
                    tool_result = {"error": str(e)}
                # Handle view_image: inject image content block
                _img_ref = tool_result.get("_image_ref") if isinstance(tool_result, dict) else None
                if _img_ref:
                    _fn = _img_ref[6:] if _img_ref.startswith("media:") else _img_ref
                    from app.services.media_service import get_file_path as _gfp
                    _img_path = _gfp(_fn)
                    if _img_path:
                        import base64 as _b64mod
                        _img_bytes = _img_path.read_bytes()
                        _b64 = _b64mod.b64encode(_img_bytes).decode("ascii")
                        _mime = "image/png" if _img_bytes[:8] == b'\x89PNG\r\n\x1a\n' else "image/webp" if _img_bytes[:4] == b'RIFF' and _img_bytes[8:12] == b'WEBP' else "image/jpeg"
                        _cot_text = f"[查看图片] {_fn}"
                        self._write_cot_block(request_id, round_index, "tool_result", _cot_text, tool_name=tool_name)
                        messages.append({
                            "role": "tool", "name": tool_name,
                            "content": [
                                {"type": "text", "text": _cot_text},
                                {"type": "image", "source": {"type": "base64", "media_type": _mime, "data": _b64}},
                            ],
                            "tool_call_id": tool_call.id,
                        })
                        _persist = {"type": "view_image", "media_ref": _img_ref}
                    else:
                        tool_result = {"error": "图片已过期或不存在"}
                        _persist = None  # fall through to normal handling
                if _img_ref and _persist is not None:
                    try:
                        self._persist_tool_result(session_id, tool_name, _persist)
                    except Exception as e:
                        logger.error("Failed to persist tool result %s: %s", tool_name, e)
                        try:
                            self.db.rollback()
                        except Exception:
                            pass
                else:
                    self._write_cot_block(
                        request_id, round_index, "tool_result",
                        json.dumps(tool_result, ensure_ascii=False),
                        tool_name=tool_name,
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "name": tool_name,
                            "content": json.dumps(tool_result, ensure_ascii=False),
                            "tool_call_id": tool_call.id,
                        }
                    )
                    try:
                        self._persist_tool_result(session_id, tool_name, tool_result)
                    except Exception as e:
                        logger.error("Failed to persist tool result %s: %s", tool_name, e)
                        try:
                            self.db.rollback()
                        except Exception:
                            pass
            # All tool results added, now call API again for next response
            # Broadcast running token totals so COT page shows tokens during tool rounds
            cot_broadcaster.publish({
                "type": "tokens_update", "request_id": request_id,
                "prompt_tokens": self._total_prompt_tokens, "completion_tokens": self._total_completion_tokens,
                "total_input": self._total_input_raw,
                "assistant_id": self.assistant_id,
            })
            logger.info("[chat_completion] Tool calls done, making follow-up API call (session=%s)", session_id)
            round_index += 1
            pending_tool_calls = list(self._fetch_next_tool_calls(
                messages, session_id, short_mode=short_mode,
                request_id=request_id, round_index=round_index,
            ))
            all_trimmed_messages.extend(self._trimmed_messages)
            all_trimmed_message_ids.extend(self._trimmed_message_ids)
        session = self.db.get(ChatSession, session_id)
        # Post-reply triggers: image description + file summarization
        _ns_assistant_id = session.assistant_id if session and session.assistant_id else None
        if _ns_assistant_id is None:
            _ns_assistant_row = self.db.query(Assistant).first()
            if _ns_assistant_row:
                _ns_assistant_id = _ns_assistant_row.id
        _maybe_trigger_post_reply(session_id, _ns_assistant_id, background_tasks)
        # Collect IDs that need summary: trimmed (covered) + pending (only if stuck)
        _all_ids_for_summary = list(all_trimmed_message_ids)
        _pending = getattr(self, "_pending_summary_ids", [])
        _remaining_tokens = getattr(self, "_last_remaining_tokens", 0)
        if _pending and _remaining_tokens > self.dialogue_retain_budget:
            _all_ids_for_summary.extend(_pending)
        if _all_ids_for_summary:
            if _ns_assistant_id is not None:
                unique_trimmed_ids = list(
                    dict.fromkeys(
                        message_id
                        for message_id in _all_ids_for_summary
                        if isinstance(message_id, int)
                    )
                )
                if background_tasks:
                    background_tasks.add_task(
                        _trigger_summary_fn,
                        self.session_factory,
                        session_id,
                        unique_trimmed_ids,
                        _ns_assistant_id,
                    )
                else:
                    threading.Thread(
                        target=_trigger_summary_fn,
                        args=(self.session_factory, session_id, unique_trimmed_ids, _ns_assistant_id),
                        daemon=True,
                    ).start()
        elapsed_ms = int((time.monotonic() - start_time) * 1000)
        if self._total_prompt_tokens or self._total_completion_tokens or elapsed_ms:
            self._write_cot_block(
                request_id, 9999, "usage",
                json.dumps({"prompt_tokens": self._total_prompt_tokens, "completion_tokens": self._total_completion_tokens, "elapsed_ms": elapsed_ms, "total_input": self._total_input_raw}),
            )
        cot_broadcaster.publish({
            "type": "done", "request_id": request_id,
            "prompt_tokens": self._total_prompt_tokens, "completion_tokens": self._total_completion_tokens,
            "elapsed_ms": elapsed_ms, "total_input": self._total_input_raw,
            "assistant_id": self.assistant_id,
        })
        return messages

    def _build_api_call_params(
        self, messages: list[dict[str, Any]], session_id: int, *, short_mode: bool = False,
        source: str | None = None,
    ) -> tuple | None:
        return build_api_call_params(
            self,
            messages,
            session_id,
            short_mode=short_mode,
            source=source,
        )

    def stream_chat_completion(
        self,
        session_id: int,
        messages: list[dict[str, Any]],
        background_tasks: BackgroundTasks | None = None,
        short_mode: bool = False,
        source: str | None = None,
        tool_results: list[dict[str, Any]] | None = None,
    ) -> Iterable[str]:
        return run_stream_chat_completion(
            self,
            session_id=session_id,
            messages=messages,
            background_tasks=background_tasks,
            short_mode=short_mode,
            source=source,
            tool_results=tool_results,
        )

    def _fetch_next_tool_calls(
        self,
        messages: list[dict[str, Any]],
        session_id: int,
        *,
        short_mode: bool = False,
        request_id: str | None = None,
        round_index: int = 0,
    ) -> Iterable[ToolCall]:
        if not hasattr(self, "_total_prompt_tokens"):
            self._total_prompt_tokens = 0
            self._total_completion_tokens = 0
            self._total_input_raw = 0
        params = self._build_api_call_params(messages, session_id, short_mode=short_mode)
        if params is None:
            return []
        client, model_name, api_messages, tools, preset_temperature, preset_top_p, use_anthropic, is_oauth, preset_max_tokens, preset_thinking_budget, provider_base_url = params
        logger.info("[_fetch_next_tool_calls] Calling model: %s (session=%s, msg_count=%d, anthropic=%s, oauth=%s)",
                    model_name, session_id, len(api_messages), use_anthropic, is_oauth)

        def _persist_error(err: Exception) -> None:
            error_content = f"(API调用失败: {err})"
            messages.append({"role": "assistant", "content": error_content})
            try:
                self._persist_message(session_id, "assistant", error_content, {})
            except Exception:
                try:
                    self.db.rollback()
                except Exception:
                    pass

        def _persist_text(raw_content: str) -> None:
            used_ids = extract_used_memory_ids(raw_content)
            now_utc = datetime.now(TZ_EAST8)
            for memory_id in used_ids:
                memory = self.db.get(Memory, int(memory_id))
                if memory:
                    memory.hits += 1
                    memory.last_access_ts = now_utc
            if used_ids:
                self.db.commit()
            _used_meta = {"used_memory_ids": used_ids} if used_ids else {}
            clean_content = clean_response_text(raw_content, short_mode=short_mode, is_proactive=False)
            _no_msg_check = re.sub(r'\[NEXT\]', '', clean_content).strip()
            if _no_msg_check == "[NO_MESSAGE]":
                messages.append({"role": "assistant", "content": clean_content, "no_message": True})
                self._persist_message(session_id, "assistant", clean_content, {**_used_meta, "no_message": True})
            elif "[NEXT]" in clean_content:
                parts = [p.strip() for p in clean_content.split("[NEXT]") if p.strip()]
                for part in parts:
                    part_meta = {**_used_meta}
                    if part == "[NO_MESSAGE]":
                        part_meta["no_message"] = True
                        messages.append({"role": "assistant", "content": part, "no_message": True})
                    else:
                        messages.append({"role": "assistant", "content": part})
                    self._persist_message(session_id, "assistant", part, part_meta)
            else:
                messages.append({"role": "assistant", "content": clean_content})
                self._persist_message(session_id, "assistant", clean_content, {**_used_meta})

        if use_anthropic:
            anth_system, anth_msgs = _oai_messages_to_anthropic(api_messages)
            anth_tools = _oai_tools_to_anthropic(tools)
            inject_anthropic_cache_breakpoint(anth_msgs)
            try:
                anth_kwargs = build_anthropic_kwargs(
                    model_name, anth_msgs, anth_system, anth_tools,
                    max_tokens=preset_max_tokens, thinking_budget=preset_thinking_budget,
                    top_p=preset_top_p, is_oauth=is_oauth,
                )
                response = client.messages.create(**anth_kwargs)
            except Exception as e:
                logger.error("[_fetch_next_tool_calls] Anthropic API FAILED (session=%s): %s", session_id, e)
                _persist_error(e)
                return []
            parsed = parse_anthropic_response(response)
            self._total_prompt_tokens += parsed.prompt_tokens
            self._total_input_raw += parsed.input_raw
            self._total_completion_tokens += parsed.completion_tokens
            if request_id:
                if parsed.thinking_content:
                    self._write_cot_block(request_id, round_index, "thinking", parsed.thinking_content)
                if parsed.text_content:
                    self._write_cot_block(request_id, round_index, "text", parsed.text_content)
            if parsed.tool_calls:
                clean_tc_text = clean_tool_call_text(parsed.text_content) if parsed.text_content else ""
                messages.append({"role": "assistant", "content": clean_tc_text or None, "tool_calls": parsed.tool_calls_payload})
                self._persist_message(session_id, "assistant", clean_tc_text, {"tool_calls": parsed.tool_calls_payload}, request_id=request_id)
                return parsed.tool_calls
            if parsed.text_content:
                _persist_text(parsed.text_content)
                return []
            else:
                logger.warning("[_fetch_next_tool_calls] Anthropic model returned empty content (session=%s), ending", session_id)
                return []

        # OpenAI path
        _apply_cache_control_oai(api_messages, use_blocks="openrouter.ai" in (provider_base_url or ""))
        _is_codex_here = is_oauth and not use_anthropic
        try:
            call_params = build_openai_kwargs(
                model_name, api_messages, tools,
                temperature=preset_temperature, top_p=preset_top_p,
                thinking_budget=preset_thinking_budget,
            )
            if _is_codex_here:
                call_params.pop("extra_body", None)
                call_params.pop("stream_options", None)
                from app.services.chat.codex_client import thinking_budget_to_effort as _tb2effort
                _effort = _tb2effort(preset_thinking_budget)
                if _effort:
                    call_params["reasoning"] = {"effort": _effort, "summary": "auto"}
            elif not use_anthropic and "deepseek.com" in (provider_base_url or "").lower():
                call_params.pop("extra_body", None)
                from app.services.chat.codex_client import thinking_budget_to_effort as _tb2effort
                _effort = _tb2effort(preset_thinking_budget)
                if _effort:
                    call_params["extra_body"] = {"thinking": {"type": "enabled"}}
                    call_params["reasoning_effort"] = _effort
                    for _m in call_params.get("messages", []):
                        if _m.get("role") == "assistant" and "reasoning_content" not in _m:
                            _m["reasoning_content"] = ""
            response = client.chat.completions.create(**call_params)
        except Exception as e:
            logger.error("[_fetch_next_tool_calls] API request FAILED (session=%s): %s", session_id, e)
            _persist_error(e)
            return []
        parsed = parse_openai_response(response)
        if parsed is None:
            logger.warning("[_fetch_next_tool_calls] LLM response had no choices (session=%s)", session_id)
            return []
        self._total_prompt_tokens += parsed.prompt_tokens
        self._total_input_raw += parsed.input_raw
        self._total_completion_tokens += parsed.completion_tokens
        if parsed.tool_calls:
            if request_id:
                if parsed.thinking_content:
                    self._write_cot_block(request_id, round_index, "thinking", parsed.thinking_content)
                if parsed.text_content:
                    self._write_cot_block(request_id, round_index, "text", parsed.text_content)
            clean_oai_text = clean_tool_call_text(parsed.text_content)
            messages.append({"role": "assistant", "content": clean_oai_text or None, "tool_calls": parsed.tool_calls_payload})
            self._persist_message(session_id, "assistant", clean_oai_text, {"tool_calls": parsed.tool_calls_payload}, request_id=request_id)
            return parsed.tool_calls
        if parsed.text_content:
            if request_id:
                if parsed.thinking_content:
                    self._write_cot_block(request_id, round_index, "thinking", parsed.thinking_content)
                self._write_cot_block(request_id, round_index, "text", parsed.text_content)
            _persist_text(parsed.text_content)
            return []
        else:
            logger.warning("[_fetch_next_tool_calls] OpenAI model returned empty content (session=%s), ending", session_id)
            return []

    def fetch_available_models(self) -> list[dict[str, Any]]:
        api_provider = self.db.query(ApiProvider).first()
        if not api_provider:
            return []
        base_url = api_provider.base_url.rstrip("/")
        response = requests.get(
            f"{base_url}/v1/models",
            headers={"Authorization": f"Bearer {api_provider.api_key}"},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        return payload.get("data", [])

    def _persist_request_snapshot(
        self,
        request_id: str,
        round_index: int,
        provider: str,
        payload: dict,
        token_stats: dict,
        cache_bp_positions: dict,
    ) -> None:
        self._persistence_context().persist_request_snapshot(
            request_id=request_id,
            round_index=round_index,
            provider=provider,
            payload=payload,
            token_stats=token_stats,
            cache_bp_positions=cache_bp_positions,
        )

    def _write_cot_block(
        self,
        request_id: str,
        round_index: int,
        block_type: str,
        content: str,
        tool_name: str | None = None,
        broadcast: bool = True,
    ) -> None:
        self._persistence_context().write_cot_block(
            request_id=request_id,
            round_index=round_index,
            block_type=block_type,
            content=content,
            tool_name=tool_name,
            broadcast=broadcast,
        )

    def _sanitize_tool_args(self, tool_call: ToolCall) -> dict[str, Any]:
        return _sanitize_args(tool_call)

    def _persist_tool_call(self, session_id: int, tool_call: ToolCall) -> None:
        self._persistence_context().persist_tool_call(session_id, tool_call)

    def _persist_tool_result(self, session_id: int, tool_name: str, tool_result: dict[str, Any]) -> None:
        self._persistence_context().persist_tool_result(session_id, tool_name, tool_result)

    @staticmethod
    def _content_to_storage(content: str | list | None) -> str:
        return content_to_storage(content)

    def _consume_alarm_inject(self) -> str | None:
        """Check for pending alarm injection and return the system message text, or None.
        Clears the injection after consuming."""
        import json as _json
        row = self.db.query(Settings).filter(Settings.key == "proactive_alarm_inject").first()
        if not row or not row.value:
            return None
        try:
            data = _json.loads(row.value)
            stored_at = datetime.fromisoformat(data["stored_at"])
            now = datetime.now(TZ_EAST8)
            # Only inject if within 5 minutes (not expired — expired ones go to 场景4)
            if now - stored_at > timedelta(minutes=5):
                return None
            # Build the injection text
            now_str = now.strftime("%H:%M")
            parts = []
            for alarm in data.get("alarms", []):
                t = alarm.get("time", "")
                r = alarm.get("reason", "")
                if r:
                    parts.append(f"[闹钟提醒] 当前时间{now_str}，你设在{t}的闹钟到了。备注：{r}")
                else:
                    parts.append(f"[闹钟提醒] 当前时间{now_str}，你设在{t}的闹钟到了。")
            # Clear
            row.value = ""
            self.db.commit()
            return "\n".join(parts) if parts else None
        except (KeyError, ValueError, _json.JSONDecodeError):
            row.value = ""
            self.db.commit()
            return None

    def _persist_message(
        self,
        session_id: int,
        role: str,
        content: str | list,
        metadata: dict[str, Any],
        request_id: str | None = None,
    ) -> Message:
        return self._persistence_context().persist_message(
            session_id=session_id,
            role=role,
            content=content,
            metadata=metadata,
            request_id=request_id,
        )
