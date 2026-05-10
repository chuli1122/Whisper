from __future__ import annotations

import json
import logging
import time
from typing import Any

from sqlalchemy import func

from app.cot_broadcaster import cot_broadcaster
from app.models.models import CotRecord, Message

logger = logging.getLogger(__name__)


def resume_with_tool_results(
    service: Any,
    session_id: int,
    messages: list[dict[str, Any]],
    tool_results: list[dict[str, Any]],
    request_id: str,
) -> tuple[str, list[dict[str, Any]]]:
    """Rebuild the pending tool round when the client posts tool results."""
    last_tc_msg = (
        service.db.query(Message)
        .filter(
            Message.session_id == session_id,
            Message.role == "assistant",
            Message.meta_info.has_key("tool_calls"),  # noqa: W601 - JSONB operator
        )
        .order_by(Message.id.desc())
        .first()
    )
    if not last_tc_msg:
        logger.warning("[stream] tool_results provided but no pending tool_calls found (session=%s)", session_id)
        return request_id, messages

    if last_tc_msg.request_id:
        request_id = str(last_tc_msg.request_id)
    raw_tool_calls = last_tc_msg.meta_info["tool_calls"]
    tc_msg_id = last_tc_msg.id

    messages = [m for m in messages if not m.get("id") or m["id"] < tc_msg_id]
    messages.append({
        "role": "assistant",
        "content": last_tc_msg.content or None,
        "tool_calls": raw_tool_calls,
    })

    server_results = (
        service.db.query(Message)
        .filter(
            Message.session_id == session_id,
            Message.role == "tool",
            Message.id > tc_msg_id,
        )
        .order_by(Message.id.asc())
        .all()
    )
    for server_result in server_results:
        result_meta = server_result.meta_info or {}
        tool_name = result_meta.get("tool_name", "unknown")
        tool_call_id = result_meta.get("tool_call_id")
        if not tool_call_id:
            for tool_call_payload in raw_tool_calls:
                fn = tool_call_payload.get("function", {})
                if fn.get("name") == tool_name:
                    tool_call_id = tool_call_payload.get("id")
                    break
        messages.append({
            "role": "tool",
            "name": tool_name,
            "content": server_result.content,
            "tool_call_id": tool_call_id or "",
        })

    prev_round = (
        service.db.query(func.max(CotRecord.round_index))
        .filter(CotRecord.request_id == request_id, CotRecord.round_index < 9999)
        .scalar()
    ) or 0
    for tool_result in tool_results:
        try:
            result_data = json.loads(tool_result["content"])
        except (json.JSONDecodeError, TypeError):
            result_data = {"output": tool_result["content"]}
        try:
            service._persist_tool_result(session_id, tool_result["name"], result_data)
        except Exception:
            pass
        service._write_cot_block(
            request_id,
            prev_round,
            "tool_result",
            json.dumps(result_data, ensure_ascii=False),
            tool_name=tool_result["name"],
        )
        messages.append({
            "role": "tool",
            "name": tool_result["name"],
            "content": tool_result["content"],
            "tool_call_id": tool_result["tool_call_id"],
        })
    logger.info("[stream] Resuming with %d client tool results (session=%s)", len(tool_results), session_id)
    return request_id, messages


def finish_stream(
    service: Any,
    request_id: str,
    start_time: float,
    *,
    prompt_tokens: int,
    completion_tokens: int,
    cache_hit: bool,
    total_input: int,
) -> int:
    elapsed_ms = int((time.monotonic() - start_time) * 1000)
    if prompt_tokens or completion_tokens or elapsed_ms:
        service._write_cot_block(
            request_id,
            9999,
            "usage",
            json.dumps({
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "elapsed_ms": elapsed_ms,
                "cache_hit": cache_hit,
                "total_input": total_input,
            }),
        )
    cot_broadcaster.publish({
        "type": "done",
        "request_id": request_id,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "elapsed_ms": elapsed_ms,
        "cache_hit": cache_hit,
        "total_input": total_input,
        "assistant_id": service.assistant_id,
    })
    return elapsed_ms


def append_json_tool_result(
    service: Any,
    messages: list[dict[str, Any]],
    *,
    session_id: int,
    request_id: str,
    round_index: int,
    tool_name: str,
    tool_call_id: str,
    tool_result: Any,
    persist: bool,
    source: str | None,
) -> None:
    content = json.dumps(tool_result, ensure_ascii=False)
    service._write_cot_block(
        request_id,
        round_index,
        "tool_result",
        content,
        tool_name=tool_name,
    )
    messages.append({
        "role": "tool",
        "name": tool_name,
        "content": content,
        "tool_call_id": tool_call_id,
    })
    if persist and source != "reflection":
        try:
            service._persist_tool_result(session_id, tool_name, tool_result)
        except Exception as exc:
            logger.error("Failed to persist tool result %s: %s", tool_name, exc)
            try:
                service.db.rollback()
            except Exception:
                pass
