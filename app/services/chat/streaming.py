from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from collections.abc import Iterable
from datetime import datetime
from typing import Any

import anthropic
from sqlalchemy import func

from app.cot_broadcaster import cot_broadcaster
from app.models.models import ApiProvider, ChatSession, CotRecord, Memory, Settings
from app.services.chat.client_tools import (
    TERMINAL_OFFLINE_RESULT,
    append_screenshot_tool_result,
    persist_client_tool_call,
    persist_terminal_tool_result,
    terminal_bridge_call,
    write_tool_use,
)
from app.services.chat.post_reply import (
    maybe_trigger_post_reply as _maybe_trigger_post_reply,
    trigger_summary as _trigger_summary_fn,
)
from app.services.chat.response_cleaner import (
    build_anthropic_kwargs,
    build_openai_kwargs,
    clean_response_text,
    clean_tool_call_text,
    extract_thinking_blocks,
    extract_used_memory_ids,
    finalize_tool_calls_acc,
    inject_anthropic_cache_breakpoint,
    split_think_and_text,
)
from app.services.chat.stream_helpers import (
    append_json_tool_result,
    finish_stream,
    resume_with_tool_results,
)
from app.services.chat.tool_executor import execute_tool as _exec_tool
from app.services.format_converters import (
    _apply_cache_control_oai,
    _extract_reasoning_delta,
    _oai_messages_to_anthropic,
    _oai_tools_to_anthropic,
    _reapply_oai_message_bp_at_idx,
)
from app.utils import TZ_EAST8

logger = logging.getLogger(__name__)


def stream_chat_completion(
    service: Any,
    session_id: int,
    messages: list[dict[str, Any]],
    background_tasks: Any | None = None,
    short_mode: bool = False,
    source: str | None = None,
    tool_results: list[dict[str, Any]] | None = None,
) -> Iterable[str]:
    """Streaming chat completion. Yields SSE events."""
    self = service
    from app.services.generation_coordinator import set_current_request_id
    self.short_mode = short_mode
    request_id = str(uuid.uuid4())
    set_current_request_id(request_id)
    start_time = time.monotonic()
    if tool_results:
        request_id, messages = resume_with_tool_results(
            self,
            session_id=session_id,
            messages=messages,
            tool_results=tool_results,
            request_id=request_id,
        )
    if messages and not tool_results and source not in ("reflection",):
        last_message = messages[-1]
        user_content = last_message.get("content", "")
        has_content = bool(user_content) if isinstance(user_content, list) else bool(user_content and user_content.strip())
        if last_message.get("role") == "user" and has_content and not last_message.get("id"):
            self._persist_message(session_id, "user", user_content, {}, request_id=request_id)
    # Check for pending alarm injection (场景3: alarm during active chat)
    if source != "proactive":
        alarm_inject_text = self._consume_alarm_inject()
        if alarm_inject_text:
            # Insert alarm system message before the last user message
            alarm_msg = {"role": "system", "content": alarm_inject_text}
            if messages and messages[-1].get("role") == "user":
                messages.insert(-1, alarm_msg)
            else:
                messages.append(alarm_msg)
            logger.info("[stream] Injected alarm reminder into chat context")

    all_trimmed_message_ids: list[int] = []
    total_prompt_tokens = 0
    total_completion_tokens = 0
    total_input_raw = 0
    anth_cache_hit = False
    # Continue round_index from previous request when resuming with tool_results
    round_index = 0
    if tool_results:
        prev_max = (
            self.db.query(func.max(CotRecord.round_index))
            .filter(CotRecord.request_id == request_id, CotRecord.round_index < 9999)
            .scalar()
        )
        if prev_max is not None:
            round_index = prev_max + 1
    _overload_retries = 0
    _MAX_OVERLOAD_RETRIES = 3
    _oauth_401_retried = False
    while True:
        try:
            params = self._build_api_call_params(messages, session_id, short_mode=short_mode, source=source)
        except Exception as e:
            logger.error("[stream] Failed to build API call params (session=%s): %s", session_id, e)
            self._write_cot_block(request_id, round_index, "error", str(e), broadcast=False)
            cot_broadcaster.publish({"type": "done", "request_id": request_id, "error": str(e), "assistant_id": self.assistant_id})
            yield f'data: {json.dumps({"error": str(e)})}\n\n'
            yield 'data: [DONE]\n\n'
            return
        if params is None:
            logger.error("[stream] _build_api_call_params returned None (session=%s)", session_id)
            self._write_cot_block(request_id, round_index, "error", "Failed to build API params", broadcast=False)
            cot_broadcaster.publish({"type": "done", "request_id": request_id, "error": "Failed to build API params", "assistant_id": self.assistant_id})
            yield 'data: [DONE]\n\n'
            return
        client, model_name, api_messages, tools, preset_temperature, preset_top_p, use_anthropic, is_oauth, preset_max_tokens, preset_thinking_budget, provider_base_url = params
        all_trimmed_message_ids.extend(self._trimmed_message_ids)
        # Write model_info and injected memories only once (skip on retry)
        if round_index == 0 and _overload_retries == 0:
            _model_info = {
                "model_name": model_name,
                "preset_name": getattr(self, "_current_preset_name", "") or "",
                "source": source or "对话",
            }
            self._write_cot_block(
                request_id, 0, "model_info",
                json.dumps(_model_info, ensure_ascii=False),
            )
            if getattr(self, "_last_recall_results", None):
                memories_list = [{"id": m.get("id"), "content": m.get("content", ""), "recall_source": m.get("recall_source")} for m in self._last_recall_results]
                cot_broadcaster.publish({
                    "type": "injected_memories",
                    "request_id": request_id,
                    "memories": memories_list,
                    "assistant_id": self.assistant_id,
                })
                self._write_cot_block(
                    request_id, 0, "injected_memories",
                    json.dumps(memories_list, ensure_ascii=False),
                    broadcast=False,
                )
        content_chunks: list[str] = []
        thinking_chunks_oai: list[str] = []
        _round_thinking_blocks: list[dict[str, Any]] = []
        tool_calls_acc: dict[int, dict] = {}
        current_round = round_index
        _oai_pending_usage = None
        if use_anthropic:
            anth_system, anth_msgs = _oai_messages_to_anthropic(api_messages)
            anth_tools = _oai_tools_to_anthropic(tools)
            # Debug: log system block structure
            _bp_info = [(i, len(b.get("text", "")), bool(b.get("cache_control"))) for i, b in enumerate(anth_system)]
            logger.info("[cache-debug] system blocks: %s, msg count: %d", _bp_info, len(anth_msgs))
            import hashlib as _hl
            import json as _dbg_json
            # Inject cache breakpoint at second-to-last user message.
            # On subsequent rounds, re-apply at the SAME position as round 0
            # to avoid moving cache_control (which is part of the cache key).
            if round_index == 0:
                inject_anthropic_cache_breakpoint(anth_msgs)
                # Remember the breakpoint index for subsequent rounds
                self._stream_cache_bp_idx = None
                for _bpi in range(len(anth_msgs) - 1, -1, -1):
                    _m = anth_msgs[_bpi]
                    if _m["role"] == "user" and isinstance(_m.get("content"), list) and any(
                        isinstance(c, dict) and "cache_control" in c for c in _m["content"]
                    ):
                        self._stream_cache_bp_idx = _bpi
                        break
            elif getattr(self, "_stream_cache_bp_idx", None) is not None:
                _idx = self._stream_cache_bp_idx
                if _idx < len(anth_msgs):
                    _m = anth_msgs[_idx]
                    if isinstance(_m.get("content"), list) and _m["content"]:
                        _m["content"][-1]["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
                    elif isinstance(_m.get("content"), str):
                        _m["content"] = [{"type": "text", "text": _m["content"], "cache_control": {"type": "ephemeral", "ttl": "1h"}}]
            # Debug: dump full request to file for diffing
            import json as _dbg_json
            _dump = {"system": anth_system, "messages": [{"role": m["role"], "content": m.get("content")} for m in anth_msgs], "tools": anth_tools}
            _dump_path = f"/tmp/anth_request_r{round_index}_{int(time.time())}.json"
            with open(_dump_path, "w") as _df:
                _dbg_json.dump(_dump, _df, ensure_ascii=False, default=str, indent=2)
            logger.info("[cache-dump] Saved request to %s (%d msgs)", _dump_path, len(anth_msgs))
            _sys_hash = _hl.md5(_dbg_json.dumps(anth_system, ensure_ascii=False, default=str).encode()).hexdigest()[:12]
            _msg0_hash = _hl.md5(_dbg_json.dumps(anth_msgs[0], ensure_ascii=False, default=str).encode()).hexdigest()[:12] if anth_msgs else "empty"
            _bp_positions = [(i, m["role"]) for i, m in enumerate(anth_msgs) if isinstance(m.get("content"), list) and any(isinstance(c, dict) and "cache_control" in c for c in m["content"])]
            logger.info("[cache-deep] round=%d sys_hash=%s msg0_hash=%s bp_positions=%s", round_index, _sys_hash, _msg0_hash, _bp_positions)
            try:
                anth_kwargs = build_anthropic_kwargs(
                    model_name, anth_msgs, anth_system, anth_tools,
                    max_tokens=preset_max_tokens, thinking_budget=preset_thinking_budget,
                    top_p=preset_top_p, is_oauth=is_oauth,
                )
                # ── Token breakdown measurement ──
                def _measure_content_tokens(content) -> int:
                    if isinstance(content, str):
                        return self._estimate_tokens(content)
                    if isinstance(content, list):
                        total = 0
                        for b in content:
                            if isinstance(b, dict):
                                if b.get("type") == "text":
                                    total += self._estimate_tokens(b.get("text", ""))
                                elif b.get("type") in ("thinking", "redacted_thinking"):
                                    total += self._estimate_tokens(b.get("thinking", "") or b.get("data", ""))
                                elif b.get("type") == "tool_use":
                                    total += self._estimate_tokens(json.dumps(b.get("input", {}), ensure_ascii=False))
                                    total += 20  # name, id overhead
                                elif b.get("type") == "tool_result":
                                    tc = b.get("content", "")
                                    total += self._estimate_tokens(tc) if isinstance(tc, str) else 0
                                    total += 10  # id overhead
                        return total
                    return 0
                _sys_tokens = sum(self._estimate_tokens(b.get("text", "")) for b in anth_kwargs.get("system", []) if isinstance(b, dict))
                _tool_def_tokens = self._estimate_tokens(json.dumps(anth_kwargs.get("tools", []), ensure_ascii=False))
                _msg_by_role: dict[str, int] = {}
                _msg_count_by_role: dict[str, int] = {}
                for _dm in anth_kwargs.get("messages", []):
                    _dr = _dm.get("role", "?")
                    _t = _measure_content_tokens(_dm.get("content"))
                    _msg_by_role[_dr] = _msg_by_role.get(_dr, 0) + _t
                    _msg_count_by_role[_dr] = _msg_count_by_role.get(_dr, 0) + 1
                _msg_total = sum(_msg_by_role.values())
                logger.info("[token-breakdown] system=%d, tools=%d, msgs=%d (%s), counts=(%s), grand_total=%d",
                            _sys_tokens, _tool_def_tokens, _msg_total,
                            ", ".join(f"{k}={v}" for k, v in sorted(_msg_by_role.items())),
                            ", ".join(f"{k}={v}" for k, v in sorted(_msg_count_by_role.items())),
                            _sys_tokens + _tool_def_tokens + _msg_total)
                try:
                    _sys_bp_idx = [i for i, b in enumerate(anth_kwargs.get("system", []))
                                   if isinstance(b, dict) and "cache_control" in b]
                    self._persist_request_snapshot(
                        request_id=request_id,
                        round_index=current_round,
                        provider="anthropic",
                        payload=anth_kwargs,
                        token_stats={
                            "system": _sys_tokens,
                            "tools": _tool_def_tokens,
                            "messages_by_role": _msg_by_role,
                            "messages_count_by_role": _msg_count_by_role,
                            "messages_total": _msg_total,
                            "grand_total": _sys_tokens + _tool_def_tokens + _msg_total,
                        },
                        cache_bp_positions={
                            "system": _sys_bp_idx,
                            "messages": _bp_positions,
                        },
                    )
                except Exception as _snap_exc:
                    logger.warning("persist request snapshot failed: %s", _snap_exc)
                with client.messages.stream(**anth_kwargs) as anth_stream:
                    # Raw event iteration: send thinking blocks to COT in real-time
                    _cur_block_type = None
                    _thinking_buf: list[str] = []
                    for event in anth_stream:
                        if event.type == "content_block_start":
                            _cur_block_type = getattr(event.content_block, "type", None)
                            logger.info("[anth-stream] round=%d block_start type=%s", current_round, _cur_block_type)
                            if _cur_block_type == "thinking":
                                _thinking_buf = []
                        elif event.type == "content_block_delta":
                            delta = event.delta
                            if hasattr(delta, "thinking"):
                                _thinking_buf.append(delta.thinking)
                                cot_broadcaster.publish({
                                    "type": "thinking_delta",
                                    "request_id": request_id,
                                    "round_index": current_round,
                                    "content": delta.thinking,
                                    "assistant_id": self.assistant_id,
                                })
                                yield f'data: {json.dumps({"thinking": delta.thinking})}\n\n'
                            elif hasattr(delta, "text"):
                                content_chunks.append(delta.text)
                                yield f'data: {json.dumps({"content": delta.text})}\n\n'
                                cot_broadcaster.publish({
                                    "type": "text_delta",
                                    "request_id": request_id,
                                    "round_index": current_round,
                                    "content": delta.text,
                                    "assistant_id": self.assistant_id,
                                })
                        elif event.type == "content_block_stop":
                            if _cur_block_type == "thinking" and _thinking_buf:
                                self._write_cot_block(request_id, current_round, "thinking", "".join(_thinking_buf), broadcast=False)
                                _thinking_buf = []
                            _cur_block_type = None
                    final_msg = anth_stream.get_final_message()
                if hasattr(final_msg, "usage") and final_msg.usage:
                    _u = final_msg.usage
                    _cache_read = getattr(_u, "cache_read_input_tokens", 0) or 0
                    if _cache_read > 0:
                        anth_cache_hit = True
                    logger.info(
                        "[Anthropic usage] input=%s cache_create=%s cache_read=%s output=%s",
                        getattr(_u, "input_tokens", None),
                        getattr(_u, "cache_creation_input_tokens", None),
                        _cache_read or None,
                        getattr(_u, "output_tokens", None),
                    )
                    _cache_create = getattr(_u, "cache_creation_input_tokens", 0) or 0
                    _raw_input = getattr(_u, "input_tokens", 0)
                    _output_tokens = getattr(_u, "output_tokens", 0)
                    total_prompt_tokens += _raw_input
                    total_input_raw += _raw_input + _cache_read + _cache_create
                    total_completion_tokens += _output_tokens
                    # Estimate native thinking tokens: output - visible text - tool_use args
                    _text_est = self._estimate_tokens("".join(content_chunks)) if content_chunks else 0
                    _tool_est = sum(
                        self._estimate_tokens(json.dumps(getattr(_b, "input", {}), ensure_ascii=False))
                        for _b in final_msg.content if _b.type == "tool_use"
                    )
                    _thinking_est = max(0, _output_tokens - _text_est - _tool_est)
                    # Write per-round usage COT block
                    self._write_cot_block(
                        request_id, round_index, "round_usage",
                        json.dumps({"input": _raw_input, "cache_create": _cache_create, "cache_read": _cache_read, "output": _output_tokens, "thinking_est": _thinking_est}),
                    )
                _round_thinking_blocks = extract_thinking_blocks(final_msg)
                for idx, block in enumerate(b for b in final_msg.content if b.type == "tool_use"):
                    tool_calls_acc[idx] = {
                        "id": block.id,
                        "name": block.name,
                        "arguments": json.dumps(block.input),
                    }
                _overload_retries = 0  # reset on success
            except Exception as e:
                # Auto-refresh OAuth token on 401 (only if this connection uses OAuth)
                # Anthropic may invalidate a token before local expires_at — so force
                # a synchronous refresh here, don't trust the cache.
                if is_oauth and ("401" in str(e) or "authentication" in str(e).lower()) and not _oauth_401_retried:
                    _oauth_401_retried = True
                    from app.services.chat.oauth_helper import ensure_valid_token
                    from app.models.models import ApiProvider
                    _refresh_provider = self.db.query(ApiProvider).filter(
                        ApiProvider.auth_type.in_(("oauth_token", "oauth_claude", "oauth_codex"))
                    ).first()
                    new_token = ensure_valid_token(self.db, _refresh_provider, force=True) if _refresh_provider else None
                    if new_token:
                        logger.info("OAuth token force-refreshed after 401, retrying...")
                        client = anthropic.Anthropic(
                            auth_token=new_token,
                            default_headers={
                                "anthropic-beta": "claude-code-20250219,oauth-2025-04-20",
                                "user-agent": "claude-code/2.1.77 (external, cli)",
                                "x-app": "cli",
                            },
                        )
                        continue  # retry the while loop
                # Retry on Anthropic overloaded / 529
                _err_str = str(e).lower()
                if ("overloaded" in _err_str or "529" in _err_str) and _overload_retries < _MAX_OVERLOAD_RETRIES:
                    _overload_retries += 1
                    _wait = 2 ** _overload_retries  # 2s, 4s, 8s
                    logger.warning("Anthropic overloaded (attempt %d/%d), retrying in %ds...", _overload_retries, _MAX_OVERLOAD_RETRIES, _wait)
                    self._write_cot_block(request_id, current_round, "info", f"API overloaded, retrying in {_wait}s (attempt {_overload_retries}/{_MAX_OVERLOAD_RETRIES})...")
                    time.sleep(_wait)
                    continue  # retry the while loop
                logger.error(f"Anthropic streaming error: {e}")
                self._write_cot_block(request_id, current_round, "error", str(e), broadcast=False)
                cot_broadcaster.publish({"type": "done", "request_id": request_id, "error": str(e), "assistant_id": self.assistant_id})
                yield f'data: {json.dumps({"error": str(e)})}\n\n'
                yield 'data: [DONE]\n\n'
                return
        else:
            # Mirror the OAuth path (line ~1281): first call of the
            # request places the message-level cache breakpoint and
            # remembers its absolute index; subsequent rounds skip the
            # auto-inject and reapply at the saved index. Anthropic's
            # cache key includes cache_control position, so pinning the
            # index prevents drift across rounds even if the natural
            # "second-to-last user" computation would pick a different
            # position after new tool/assistant messages are appended.
            _use_blocks = "openrouter.ai" in (provider_base_url or "")
            _first_bp_pass = not hasattr(self, "_stream_cache_bp_idx_oai")
            if _first_bp_pass:
                _apply_cache_control_oai(api_messages, use_blocks=_use_blocks)
                self._stream_cache_bp_idx_oai = None
                if _use_blocks:
                    for _bpi in range(len(api_messages) - 1, -1, -1):
                        _m = api_messages[_bpi]
                        if _m.get("role") == "user" and isinstance(_m.get("content"), list) and any(
                            isinstance(c, dict) and "cache_control" in c for c in _m["content"]
                        ):
                            self._stream_cache_bp_idx_oai = _bpi
                            break
            else:
                _apply_cache_control_oai(api_messages, use_blocks=_use_blocks, skip_message_bp=True)
                if self._stream_cache_bp_idx_oai is not None and _use_blocks:
                    _reapply_oai_message_bp_at_idx(api_messages, self._stream_cache_bp_idx_oai)
            try:
                stream_params = build_openai_kwargs(
                    model_name, api_messages, tools,
                    temperature=preset_temperature, top_p=preset_top_p,
                    thinking_budget=preset_thinking_budget, stream=True,
                )
                if is_oauth and not use_anthropic:
                    # Codex path: Responses API uses `reasoning.effort`
                    # instead of the OpenRouter-style
                    # `extra_body.reasoning.max_tokens`.
                    stream_params.pop("extra_body", None)
                    stream_params.pop("stream_options", None)
                    from app.services.chat.codex_client import thinking_budget_to_effort as _tb2effort
                    _effort = _tb2effort(preset_thinking_budget)
                    if _effort:
                        stream_params["reasoning"] = {"effort": _effort, "summary": "auto"}
                elif not use_anthropic and "deepseek.com" in (provider_base_url or "").lower():
                    # DeepSeek path: uses `extra_body.thinking.type` +
                    # top-level `reasoning_effort`, not OpenRouter's schema.
                    stream_params.pop("extra_body", None)
                    from app.services.chat.codex_client import thinking_budget_to_effort as _tb2effort
                    _effort = _tb2effort(preset_thinking_budget)
                    if _effort:
                        stream_params["extra_body"] = {"thinking": {"type": "enabled"}}
                        stream_params["reasoning_effort"] = _effort
                        # DS requires `reasoning_content` on every historic
                        # assistant message when tool calls are involved.
                        # Pre-DS history has none, so stub empty string.
                        for _m in stream_params.get("messages", []):
                            if _m.get("role") == "assistant" and "reasoning_content" not in _m:
                                _m["reasoning_content"] = ""
                try:
                    _oai_msgs = stream_params.get("messages", [])
                    _oai_tools = stream_params.get("tools", [])
                    _oai_msg_by_role: dict[str, int] = {}
                    _oai_msg_count_by_role: dict[str, int] = {}
                    _oai_msg_bp: list = []
                    for _oi, _om in enumerate(_oai_msgs):
                        _or = _om.get("role", "?")
                        _oc = _om.get("content")
                        if isinstance(_oc, str):
                            _ot = self._estimate_tokens(_oc)
                        elif isinstance(_oc, list):
                            _ot = 0
                            for _b in _oc:
                                if isinstance(_b, dict):
                                    _ot += self._estimate_tokens(_b.get("text", "") or "")
                                    if "cache_control" in _b:
                                        _oai_msg_bp.append((_oi, _or))
                        else:
                            _ot = 0
                        _oai_msg_by_role[_or] = _oai_msg_by_role.get(_or, 0) + _ot
                        _oai_msg_count_by_role[_or] = _oai_msg_count_by_role.get(_or, 0) + 1
                    _oai_msg_total = sum(_oai_msg_by_role.values())
                    _oai_tool_tokens = self._estimate_tokens(json.dumps(_oai_tools, ensure_ascii=False)) if _oai_tools else 0
                    self._persist_request_snapshot(
                        request_id=request_id,
                        round_index=current_round,
                        provider="openrouter" if _use_blocks else "openai-compat",
                        payload=stream_params,
                        token_stats={
                            "system": 0,
                            "tools": _oai_tool_tokens,
                            "messages_by_role": _oai_msg_by_role,
                            "messages_count_by_role": _oai_msg_count_by_role,
                            "messages_total": _oai_msg_total,
                            "grand_total": _oai_tool_tokens + _oai_msg_total,
                        },
                        cache_bp_positions={"system": [], "messages": _oai_msg_bp},
                    )
                except Exception as _snap_exc:
                    logger.warning("persist OR request snapshot failed: %s", _snap_exc)
                # Dump OAI (DS/DeepSeek in particular) request payload for cache debugging.
                try:
                    import hashlib as _hl
                    import json as _dbg_json
                    _dump_path = f"/tmp/oai_request_r{current_round}_{int(time.time())}.json"
                    with open(_dump_path, "w") as _df:
                        _dbg_json.dump(stream_params, _df, ensure_ascii=False, default=str, indent=2)
                    _sys_msg = next((m for m in _oai_msgs if m.get("role") == "system"), None)
                    _sys_content = _sys_msg.get("content", "") if _sys_msg else ""
                    _sys_text = _sys_content if isinstance(_sys_content, str) else _dbg_json.dumps(_sys_content, ensure_ascii=False, default=str)
                    _sys_hash = _hl.md5(_sys_text.encode()).hexdigest()[:12]
                    _sys_head_hash = _hl.md5(_sys_text[:2000].encode()).hexdigest()[:12]
                    _tools_hash = _hl.md5(_dbg_json.dumps(_oai_tools, ensure_ascii=False, default=str).encode()).hexdigest()[:12]
                    _first_user = next((m for m in _oai_msgs if m.get("role") == "user"), None)
                    _first_user_hash = _hl.md5(_dbg_json.dumps(_first_user, ensure_ascii=False, default=str).encode()).hexdigest()[:12] if _first_user else "none"
                    logger.info(
                        "[oai-dump] %s (%d msgs) sys_hash=%s sys_head2k=%s tools_hash=%s first_user_hash=%s",
                        _dump_path, len(_oai_msgs), _sys_hash, _sys_head_hash, _tools_hash, _first_user_hash,
                    )
                except Exception as _dump_exc:
                    logger.warning("oai request dump failed: %s", _dump_exc)
                stream = client.chat.completions.create(**stream_params)
            except Exception as e:
                err_str = str(e)
                if "unknown variant `image`" in err_str and not getattr(self, '_image_fallback_done', False):
                    logger.warning("[image-fallback] provider doesn't support image blocks, replacing with text placeholders and retrying")
                    self._image_fallback_done = True
                    for m in stream_params.get("messages", []):
                        content = m.get("content")
                        if isinstance(content, list):
                            m["content"] = [
                                c if c.get("type") != "image_url" and c.get("type") != "image" else {"type": "text", "text": "[图片]"}
                                for c in content
                            ]
                    try:
                        stream = client.chat.completions.create(**stream_params)
                    except Exception as e2:
                        logger.error(f"Streaming request failed after image fallback: {e2}")
                        self._write_cot_block(request_id, current_round, "error", str(e2), broadcast=False)
                        cot_broadcaster.publish({"type": "done", "request_id": request_id, "error": str(e2), "assistant_id": self.assistant_id})
                        yield f'data: {json.dumps({"error": str(e2)})}\n\n'
                        yield 'data: [DONE]\n\n'
                        return
                else:
                    logger.error(f"Streaming request failed: {e}")
                    self._write_cot_block(request_id, current_round, "error", err_str, broadcast=False)
                    cot_broadcaster.publish({"type": "done", "request_id": request_id, "error": err_str, "assistant_id": self.assistant_id})
                    yield f'data: {json.dumps({"error": err_str})}\n\n'
                    yield 'data: [DONE]\n\n'
                    return
            oai_finish_reason = None
            _oai_pending_usage = None
            try:
                for chunk in stream:
                    if hasattr(chunk, "usage") and chunk.usage:
                        _p = getattr(chunk.usage, "prompt_tokens", 0) or 0
                        _details = getattr(chunk.usage, "prompt_tokens_details", None)
                        def _get_detail(key: str) -> int:
                            if _details is None:
                                return 0
                            v = getattr(_details, key, None)
                            if v is None and isinstance(_details, dict):
                                v = _details.get(key)
                            return int(v or 0)
                        _cached = _get_detail("cached_tokens")
                        _cache_write = _get_detail("cache_write_tokens")
                        if _cached:
                            anth_cache_hit = True
                        _p -= _cached + _cache_write
                        _oai_output = getattr(chunk.usage, "completion_tokens", 0) or 0
                        _oai_pending_usage = {
                            "input": _p, "cache_create": _cache_write,
                            "cache_read": _cached, "output": _oai_output,
                            "_raw_total": getattr(chunk.usage, "prompt_tokens", 0) or 0,
                        }
                    if not chunk.choices:
                        continue
                    choice0 = chunk.choices[0]
                    if getattr(choice0, "finish_reason", None):
                        oai_finish_reason = choice0.finish_reason
                    delta = choice0.delta
                    # Handle reasoning/thinking delta (OpenRouter)
                    reasoning_text = _extract_reasoning_delta(delta)
                    if reasoning_text:
                        thinking_chunks_oai.append(reasoning_text)
                        cot_broadcaster.publish({
                            "type": "thinking_delta",
                            "request_id": request_id,
                            "round_index": current_round,
                            "content": reasoning_text,
                            "assistant_id": self.assistant_id,
                        })
                        yield f'data: {json.dumps({"thinking": reasoning_text})}\n\n'
                    if getattr(delta, "content", None):
                        content_chunks.append(delta.content)
                        yield f'data: {json.dumps({"content": delta.content})}\n\n'
                        cot_broadcaster.publish({
                            "type": "text_delta",
                            "request_id": request_id,
                            "round_index": current_round,
                            "content": delta.content,
                            "assistant_id": self.assistant_id,
                        })
                    if getattr(delta, "tool_calls", None):
                        for tc_delta in delta.tool_calls:
                            idx = tc_delta.index
                            if idx not in tool_calls_acc:
                                tool_calls_acc[idx] = {"id": "", "name": "", "arguments": ""}
                            if tc_delta.id:
                                tool_calls_acc[idx]["id"] = tc_delta.id
                            if tc_delta.function:
                                if tc_delta.function.name:
                                    tool_calls_acc[idx]["name"] = tc_delta.function.name
                                if tc_delta.function.arguments:
                                    tool_calls_acc[idx]["arguments"] += tc_delta.function.arguments
            except Exception as e:
                logger.error(f"Stream iteration error: {e}")
                self._write_cot_block(request_id, current_round, "error", str(e), broadcast=False)
                cot_broadcaster.publish({"type": "done", "request_id": request_id, "error": str(e), "assistant_id": self.assistant_id})
                yield f'data: {json.dumps({"error": str(e)})}\n\n'
                yield 'data: [DONE]\n\n'
                return
        if _oai_pending_usage:
            total_prompt_tokens += _oai_pending_usage["input"]
            total_input_raw += _oai_pending_usage["_raw_total"]
            total_completion_tokens += _oai_pending_usage["output"]
            _usage_out = {k: v for k, v in _oai_pending_usage.items() if k != "_raw_total"}
            logger.info(
                "[OAI usage] prompt=%s cached=%s cache_write=%s effective=%s output=%s",
                _oai_pending_usage["_raw_total"], _oai_pending_usage["cache_read"],
                _oai_pending_usage["cache_create"], _oai_pending_usage["input"],
                _oai_pending_usage["output"],
            )
            self._write_cot_block(
                request_id, round_index, "round_usage",
                json.dumps(_usage_out),
            )
        if tool_calls_acc:
            tool_calls_payload, parsed_tool_calls = finalize_tool_calls_acc(tool_calls_acc)
            full_content = "".join(content_chunks)
            # Write thinking COT block from OpenRouter reasoning (already streamed as deltas)
            full_thinking = "".join(thinking_chunks_oai)
            if full_thinking:
                self._write_cot_block(request_id, current_round, "thinking", full_thinking, broadcast=False)
            # Split [THINK] blocks and text into alternating COT blocks.
            # [THINK]...[/THINK] in the model's text output is a "fake" thinking block
            # written by the model into its reply — distinguish from native thinking.
            _segments = split_think_and_text(full_content) if full_content else []
            for _seg_type, _seg_text in _segments:
                _effective_type = "thinking_fake" if _seg_type == "thinking" else _seg_type
                self._write_cot_block(request_id, current_round, _effective_type, _seg_text, broadcast=False)
            _assistant_msg: dict[str, Any] = {
                "role": "assistant", "content": full_content or None,
                "tool_calls": tool_calls_payload,
            }
            # Preserve Anthropic thinking blocks for next round
            if _round_thinking_blocks:
                _assistant_msg["_thinking_blocks"] = _round_thinking_blocks
            messages.append(_assistant_msg)
            # Persist intermediate round text as a separate visible message
            if full_content.strip() and source != "reflection":
                clean_mid = clean_tool_call_text(full_content)
                if clean_mid:
                    try:
                        _mid_msg = self._persist_message(session_id, "assistant", clean_mid, {"intermediate": True}, request_id=request_id)
                        # Signal consumers to send — strip [THINK] for delivery
                        _send_mid = re.sub(r'(?:\[THINK\]|<scratchpad>).*?(?:\[/THINK\]|</THINK>|</thinking>|</scratchpad>)', '', clean_mid, flags=re.DOTALL)
                        for _orphan in ('<scratchpad>', '</scratchpad>', '[THINK]', '[/THINK]', '</THINK>', '</thinking>'):
                            _send_mid = _send_mid.replace(_orphan, '')
                        _send_mid = _send_mid.strip()
                        if _send_mid:
                            yield f'data: {json.dumps({"intermediate": {"content": _send_mid, "db_id": _mid_msg.id}})}\n\n'
                    except Exception as e:
                        logger.error("Failed to persist intermediate text message: %s", e)
                        try:
                            self.db.rollback()
                        except Exception:
                            pass
            # Persist tool_calls message (hidden from chat list by API filter)
            if source != "reflection":
                try:
                    _tc_meta: dict[str, Any] = {"tool_calls": tool_calls_payload}
                    if _round_thinking_blocks:
                        _tc_meta["_thinking_blocks"] = _round_thinking_blocks
                    self._persist_message(session_id, "assistant", "", _tc_meta, request_id=request_id)
                except Exception as e:
                    logger.error("Failed to persist assistant tool_calls message: %s", e)
                    try:
                        self.db.rollback()
                    except Exception:
                        pass
            # Separate server-side and client-side tool calls
            server_calls = [tc for tc in parsed_tool_calls if tc.name not in self.client_side_tools]
            client_calls = [tc for tc in parsed_tool_calls if tc.name in self.client_side_tools]
            # Execute server-side tools: write tool_use then tool_result COT blocks in pairs
            for tc in server_calls:
                self._write_cot_block(
                    request_id, current_round, "tool_use",
                    tc.arguments if isinstance(tc.arguments, str) else json.dumps(tc.arguments, ensure_ascii=False),
                    tool_name=tc.name,
                )
                if source != "reflection":
                    try:
                        self._persist_tool_call(session_id, tc)
                    except Exception as e:
                        logger.error("Failed to persist tool call %s: %s", tc.name, e)
                        try:
                            self.db.rollback()
                        except Exception:
                            pass
                try:
                    if tc.name == "search_memory" and tc.arguments.get("action") == "related":
                        tc.arguments["_seen_memory_ids"] = list(getattr(self, "_seen_memory_ids", set()))
                    if tc.name == "switch_channel" and self.proactive_extra_prompt:
                        tool_result = {"result": "当前为主动消息模式，不可使用switch_channel。请在回复开头使用[VIA:telegram]或[VIA:qq]切换渠道。"}
                    else:
                        tool_result = _exec_tool(tc, db=self.db, memory_service=self.memory_service, assistant_name=self.assistant_name, current_assistant_id=getattr(self, "_current_assistant_id", None), current_session_id=getattr(self, "_current_session_id", None))
                    if isinstance(tool_result, dict) and "_related_ids" in tool_result:
                        if not hasattr(self, "_seen_memory_ids"):
                            self._seen_memory_ids = set()
                        self._seen_memory_ids.update(tool_result.pop("_related_ids"))
                    if isinstance(tool_result, dict) and "_switch_channel" in tool_result:
                        self._switched_channel = tool_result.pop("_switch_channel")
                        self.source = self._switched_channel
                        self.short_mode = (self._switched_channel in ("qq", "wechat"))
                        short_mode = self.short_mode
                except Exception as e:
                    logger.error("Tool execution error (%s): %s", tc.name, e)
                    tool_result = {"error": str(e)}
                # Handle view_image: inject image content block
                _img_ref = tool_result.get("_image_ref") if isinstance(tool_result, dict) else None
                if _img_ref:
                    if _img_ref.startswith("screenshots:"):
                        _fn = _img_ref[len("screenshots:"):]
                        from pathlib import Path as _Path
                        _img_path = _Path("/srv/ai-companion/screenshots") / _fn
                        _img_path = _img_path if _img_path.exists() else None
                    else:
                        _fn = _img_ref[6:] if _img_ref.startswith("media:") else _img_ref
                        from app.services.media_service import get_file_path as _gfp
                        _img_path = _gfp(_fn)
                    if _img_path:
                        import base64 as _b64mod
                        _img_bytes = _img_path.read_bytes()
                        _b64 = _b64mod.b64encode(_img_bytes).decode("ascii")
                        _mime = "image/png" if _img_bytes[:8] == b'\x89PNG\r\n\x1a\n' else "image/webp" if _img_bytes[:4] == b'RIFF' and _img_bytes[8:12] == b'WEBP' else "image/jpeg"
                        _cot_text = f"[查看图片] {_fn}"
                        self._write_cot_block(request_id, current_round, "tool_result", _cot_text, tool_name=tc.name)
                        messages.append({
                            "role": "tool", "name": tc.name,
                            "content": [
                                {"type": "text", "text": _cot_text},
                                {"type": "image", "source": {"type": "base64", "media_type": _mime, "data": _b64}},
                            ],
                            "tool_call_id": tc.id,
                        })
                        _persist = {"type": "view_image", "media_ref": _img_ref}
                    else:
                        tool_result = {"error": "图片已过期或不存在"}
                        _persist = None
                if _img_ref and _persist is not None:
                    if source != "reflection":
                        try:
                            self._persist_tool_result(session_id, tc.name, _persist)
                        except Exception as e:
                            logger.error("Failed to persist tool result %s: %s", tc.name, e)
                            try:
                                self.db.rollback()
                            except Exception:
                                pass
                else:
                    append_json_tool_result(
                        self,
                        messages,
                        session_id=session_id,
                        request_id=request_id,
                        round_index=current_round,
                        tool_name=tc.name,
                        tool_call_id=tc.id,
                        tool_result=tool_result,
                        persist=True,
                        source=source,
                    )
            # Client-side tools (pc_control)
            if client_calls:
                from app.services.terminal_bridge import bridge as _tb_check
                if source != "terminal" and not _tb_check.is_online():
                    # Terminal offline — return error for all client calls
                    for tc in client_calls:
                        write_tool_use(self, request_id, current_round, tc)
                        append_json_tool_result(
                            self,
                            messages,
                            session_id=session_id,
                            request_id=request_id,
                            round_index=current_round,
                            tool_name=tc.name,
                            tool_call_id=tc.id,
                            tool_result=TERMINAL_OFFLINE_RESULT,
                            persist=False,
                            source=source,
                        )
                elif source == "terminal":
                    # Direct CLI: yield SSE events for CLI to execute locally
                    for tc in client_calls:
                        write_tool_use(self, request_id, current_round, tc)
                        persist_client_tool_call(self, session_id, tc)
                        yield f'data: {json.dumps({"tool_call": {"id": tc.id, "name": tc.name, "arguments": tc.arguments}})}\n\n'
                    cot_broadcaster.publish({
                        "type": "done", "request_id": request_id,
                        "prompt_tokens": total_prompt_tokens, "completion_tokens": total_completion_tokens,
                        "elapsed_ms": int((time.monotonic() - start_time) * 1000),
                        "assistant_id": self.assistant_id,
                        "cache_hit": anth_cache_hit, "total_input": total_input_raw,
                    })
                    yield 'data: [DONE]\n\n'
                    return
                else:
                    # Telegram with terminal online: execute via WebSocket bridge
                    from app.services.terminal_bridge import bridge as _tb
                    for tc in client_calls:
                        write_tool_use(self, request_id, current_round, tc)
                        persist_client_tool_call(self, session_id, tc)
                        _bridge_name, args = terminal_bridge_call(tc)
                        _device = args.pop("_device", None)
                        tool_result = _tb.execute(_bridge_name, args, device=_device)
                        if not append_screenshot_tool_result(
                            self,
                            messages,
                            request_id=request_id,
                            round_index=current_round,
                            tool_call=tc,
                            tool_result=tool_result,
                        ):
                            append_json_tool_result(
                                self,
                                messages,
                                session_id=session_id,
                                request_id=request_id,
                                round_index=current_round,
                                tool_name=tc.name,
                                tool_call_id=tc.id,
                                tool_result=tool_result,
                                persist=False,
                                source=source,
                            )
                        persist_terminal_tool_result(self, session_id, tc.name, tool_result)
            # Broadcast running token totals so COT page shows tokens during tool rounds
            cot_broadcaster.publish({
                "type": "tokens_update", "request_id": request_id,
                "prompt_tokens": total_prompt_tokens, "completion_tokens": total_completion_tokens,
                "cache_hit": anth_cache_hit, "total_input": total_input_raw,
                "assistant_id": self.assistant_id,
            })
            logger.info("[stream] Tool calls done, making follow-up API call (session=%s)", session_id)
            round_index += 1
            # Read configurable max tool rounds
            _max_rounds_row = self.db.query(Settings).filter(Settings.key == "max_tool_rounds").first()
            _max_rounds = int(_max_rounds_row.value) if _max_rounds_row else 15
            if round_index >= _max_rounds:
                logger.warning("[stream] Max tool rounds reached (%d, session=%s)", _max_rounds, session_id)
                yield f'data: {json.dumps({"content": "(已达到最大工具调用轮次)"})}\n\n'
                break
            # Stop after specific tool+action combos (e.g. cafe_chat:send — no follow-up needed)
            # Format: {"tool_name:action"} where action is checked in tool arguments
            _stop_actions = getattr(self, "_stop_after_tool_actions", None)
            if _stop_actions and parsed_tool_calls:
                _should_stop = False
                for tc in parsed_tool_calls:
                    _args = tc.arguments if isinstance(tc.arguments, dict) else {}
                    _key = f"{tc.name}:{_args.get('action', '')}"
                    if _key in _stop_actions:
                        logger.info("[stream] Stop-after-tool triggered (%s), ending stream", _key)
                        _should_stop = True
                        break
                if _should_stop:
                    break
            continue
        # If model returned nothing after tool calls, just end
        if not content_chunks and not thinking_chunks_oai and round_index > 0:
            logger.warning("[stream] Empty response after tool calls, ending stream (session=%s, round=%s)", session_id, round_index)
            break

        # Final text response (already streamed as deltas)
        full_thinking = "".join(thinking_chunks_oai)
        if full_thinking:
            self._write_cot_block(request_id, current_round, "thinking", full_thinking, broadcast=False)
        full_content = "".join(content_chunks)
        # Split [THINK] blocks and text into alternating COT blocks
        _segments = split_think_and_text(full_content) if full_content else [("text", full_content)]
        for _seg_type, _seg_text in _segments:
            _effective_type = "thinking_fake" if _seg_type == "thinking" else _seg_type
            self._write_cot_block(request_id, current_round, _effective_type, _seg_text, broadcast=False)
        used_ids = extract_used_memory_ids(full_content)
        now_utc = datetime.now(TZ_EAST8)
        for memory_id in used_ids:
            memory = self.db.get(Memory, int(memory_id))
            if memory:
                memory.hits += 1
                memory.last_access_ts = now_utc
        if used_ids:
            self.db.commit()
        _used_meta: dict[str, Any] = {"used_memory_ids": used_ids} if used_ids else {}
        if _round_thinking_blocks:
            _used_meta["_thinking_blocks"] = _round_thinking_blocks
        clean_content = clean_response_text(
            full_content,
            short_mode=self.short_mode,
            is_proactive=bool(getattr(self, "proactive_extra_prompt", None)),
        )
        # Reflection: don't persist messages to chat history
        if source != "reflection":
            # Hide NO_MESSAGE — only when the entire reply is just [NO_MESSAGE]
            # (not when the model mentions it in conversational text)
            _no_msg_check = re.sub(r'\[NEXT\]', '', clean_content).strip()
            if _no_msg_check == "[NO_MESSAGE]":
                self._persist_message(session_id, "assistant", clean_content, {**_used_meta, "no_message": True}, request_id=request_id)
            elif "[NEXT]" in clean_content:
                parts = [p.strip() for p in clean_content.split("[NEXT]") if p.strip()]
                for idx, part in enumerate(parts):
                    part_meta = {**_used_meta}
                    if idx > 0:
                        part_meta.pop("_thinking_blocks", None)
                    if part == "[NO_MESSAGE]":
                        part_meta["no_message"] = True
                    self._persist_message(session_id, "assistant", part, part_meta, request_id=request_id)
            else:
                self._persist_message(session_id, "assistant", clean_content, {**_used_meta}, request_id=request_id)
        if source != "reflection":
            session = self.db.get(ChatSession, session_id)
            if session:
                session.updated_at = datetime.now(TZ_EAST8)
                self.db.commit()
            # Post-reply triggers: image description + file summarization
            _stream_assistant_id = session.assistant_id if session else None
            _maybe_trigger_post_reply(session_id, _stream_assistant_id, background_tasks)
            _stream_all_ids = list(all_trimmed_message_ids)
            _stream_pending = getattr(self, "_pending_summary_ids", [])
            _stream_remaining = getattr(self, "_last_remaining_tokens", 0)
            if _stream_pending and _stream_remaining > self.dialogue_retain_budget:
                _stream_all_ids.extend(_stream_pending)
            if _stream_all_ids:
                assistant_id = session.assistant_id if session else None
                if assistant_id:
                    unique_ids = list(dict.fromkeys(
                        mid for mid in _stream_all_ids if isinstance(mid, int)
                    ))
                    if background_tasks:
                        background_tasks.add_task(
                            _trigger_summary_fn, self.session_factory, session_id, unique_ids, assistant_id,
                        )
                    else:
                        threading.Thread(
                            target=_trigger_summary_fn,
                            args=(self.session_factory, session_id, unique_ids, assistant_id),
                            daemon=True,
                        ).start()
        finish_stream(
            self,
            request_id,
            start_time,
            prompt_tokens=total_prompt_tokens,
            completion_tokens=total_completion_tokens,
            cache_hit=anth_cache_hit,
            total_input=total_input_raw,
        )
        yield 'data: [DONE]\n\n'
        return

    # ===== break 退出循环后（如最大工具轮次），补发 done 事件 =====
    finish_stream(
        self,
        request_id,
        start_time,
        prompt_tokens=total_prompt_tokens,
        completion_tokens=total_completion_tokens,
        cache_hit=anth_cache_hit,
        total_input=total_input_raw,
    )
    yield 'data: [DONE]\n\n'
