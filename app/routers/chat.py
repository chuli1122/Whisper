from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import Assistant, ChatSession, Message as MessageModel
from app.services.chat_service import ChatService

logger = logging.getLogger(__name__)
router = APIRouter()


class ToolCallPayload(BaseModel):
    name: str
    arguments: dict[str, Any]


class ToolResultPayload(BaseModel):
    tool_call_id: str
    name: str
    content: str


class ChatCompletionRequest(BaseModel):
    session_id: int
    message: str | list[dict[str, Any]] | None = None
    messages: list[dict[str, Any]] = []
    tool_calls: list[ToolCallPayload] = []
    tool_results: list[ToolResultPayload] = []
    stream: bool = False
    short_mode: bool = False
    source: str | None = None  # 消息来源标识，如 "terminal", "telegram"


class ChatCompletionResponse(BaseModel):
    messages: list[dict[str, Any]]


def _compress_tool_result(tool_name: str, content: str) -> str:
    """Compress tool_result content into a short summary for context injection."""
    try:
        data = json.loads(content) if content else {}
    except (json.JSONDecodeError, TypeError):
        data = {}

    if not isinstance(data, dict):
        return f"[{tool_name}] {str(data)[:60]}"

    if "error" in data:
        return f"[{tool_name}] 错误: {data['error']}"

    if tool_name == "save_memory":
        mid = data.get("id", "?")
        c = str(data.get("content", ""))[:30]
        if data.get("duplicate"):
            return f"[已存储记忆] 重复, existing_id={data.get('existing_id', '?')}"
        return f"[已存储记忆] id={mid}, {c}..."

    if tool_name == "update_memory":
        mid = data.get("id", "?")
        c = str(data.get("content", ""))[:30]
        return f"[已更新记忆] id={mid}, {c}..."

    if tool_name == "list_memories":
        memories = data.get("memories", [])
        ids = [str(m.get("id", "?")) for m in memories]
        return f"[已列出记忆] 返回{len(memories)}条, ids=[{','.join(ids)}]"

    if tool_name == "search_memory":
        results = data.get("results", [])
        query = data.get("query", "")
        ids = [str(r.get("id", "?")) for r in results]
        return f"[已搜索记忆] 关键词={query}, 返回{len(results)}条, ids=[{','.join(ids)}]"

    if tool_name == "delete_memory":
        mid = data.get("id", "?")
        return f"[已删除记忆] id={mid}"

    if tool_name == "write_diary":
        did = data.get("id", "?")
        title = data.get("title", "")[:20]
        return f"[已写日记] id={did}, {title}"

    if tool_name == "read_diary":
        if "diaries" in data:
            diaries = data["diaries"]
            ids = [str(d.get("id", "?")) for d in diaries]
            return f"[已读日记列表] 返回{len(diaries)}条, ids=[{','.join(ids)}]"
        did = data.get("id", "?")
        title = data.get("title", "")[:20]
        return f"[已读日记] id={did}, {title}"

    if tool_name == "search_summary":
        results = data.get("results", [])
        query = data.get("query", "")
        ids = [str(r.get("id", "?")) for r in results]
        return f"[已搜索摘要] 关键词={query}, 返回{len(results)}条, ids=[{','.join(ids)}]"

    if tool_name == "get_summary_by_id":
        sid = data.get("id", "?")
        sc = str(data.get("summary_content", ""))[:40]
        return f"[已查看摘要] id={sid}, {sc}..."

    if tool_name == "search_chat_history":
        results = data.get("results", [])
        query = data.get("query", "")
        n = len(results)
        return f"[已搜索聊天记录] 关键词={query}, 返回{n}条"

    if tool_name == "search_theater":
        results = data.get("results", [])
        query = data.get("query", "")
        return f"[已搜索小剧场] 关键词={query}, 返回{len(results)}条"

    if tool_name == "web_search":
        results = data.get("results", [])
        query = data.get("query", "")
        return f"[已搜索] query={query}, 返回{len(results)}条结果"

    if tool_name == "web_fetch":
        title = data.get("title", "")
        url = data.get("url", "")
        return f"[已读取网页] url: {url} | title: {title}"

    if tool_name == "run_bash":
        output = data.get("output", "")
        exit_code = data.get("exit_code", "?")
        return f"[已执行命令] exit={exit_code}, {output[:60]}"

    if tool_name == "read_file":
        path = data.get("path", "")
        content = data.get("content", "")
        return f"[已读取文件] {path}, {len(content)}字"

    if tool_name == "write_file":
        path = data.get("path", "")
        written = data.get("bytes_written", "?")
        return f"[已写入文件] {path}, {written}字节"

    # Fallback: tool_name + truncated content
    return f"[{tool_name}] {str(data)[:60]}"


def _load_session_messages(db: Session, session_id: int) -> list[dict[str, Any]]:
    """Load message history from DB for a session, including tool messages.
    Skips messages that have already been summarized (summary_group_id set),
    since their content is represented by summaries in the system prompt."""
    db_msgs = (
        db.query(MessageModel)
        .filter(
            MessageModel.session_id == session_id,
            MessageModel.role.in_(["user", "assistant", "tool", "system"]),
            MessageModel.summary_group_id.is_(None),
        )
        .order_by(MessageModel.id.asc())
        .all()
    )
    messages: list[dict[str, Any]] = [{"role": "system", "content": ""}]
    # Track tool_call_ids for matching tool results to their tool_use blocks
    pending_tc_ids: dict[str, list[str]] = {}  # tool_name → [tc_id, ...]
    for m in db_msgs:
        if m.role == "assistant":
            meta = m.meta_info or {}
            _no_msg = meta.get("no_message", False)
            if "tool_calls" in meta:
                # Bulk tool_calls message — preserve proper format for API
                tc_list = meta["tool_calls"]
                pending_tc_ids = {}
                for tc in tc_list:
                    fn = tc.get("function", {})
                    name = fn.get("name", "")
                    tc_id = tc.get("id", "")
                    pending_tc_ids.setdefault(name, []).append(tc_id)
                msg_dict = {
                    "role": "assistant",
                    "content": m.content or None,
                    "tool_calls": tc_list,
                    "id": m.id,
                    "created_at": m.created_at,
                }
                if _no_msg:
                    msg_dict["no_message"] = True
                messages.append(msg_dict)
            elif "tool_call" in meta:
                # Individual tool call record (redundant with bulk) — skip
                pass
            elif m.content and m.content.strip():
                msg_dict = {
                    "role": m.role,
                    "content": m.content,
                    "id": m.id,
                    "created_at": m.created_at,
                }
                if _no_msg:
                    msg_dict["no_message"] = True
                messages.append(msg_dict)
            # Skip empty assistant messages
        elif m.role == "tool":
            meta = m.meta_info or {}
            _no_msg = meta.get("no_message", False)
            tool_name = meta.get("tool_name", "unknown")
            # Match tool_call_id from pending_tc_ids
            tool_call_id = meta.get("tool_call_id", "")
            if not tool_call_id and tool_name in pending_tc_ids and pending_tc_ids[tool_name]:
                tool_call_id = pending_tc_ids[tool_name].pop(0)
            compressed = _compress_tool_result(tool_name, m.content)
            if tool_call_id:
                # Proper tool result format — _oai_messages_to_anthropic converts to tool_result block
                messages.append({
                    "role": "tool",
                    "name": tool_name,
                    "content": compressed,
                    "tool_call_id": tool_call_id,
                    "id": m.id,
                    "created_at": m.created_at,
                })
            else:
                # Orphaned tool result (no matching tool_use) — fall back to text
                msg_dict = {
                    "role": "assistant",
                    "content": compressed,
                    "id": m.id,
                    "created_at": m.created_at,
                }
                if _no_msg:
                    msg_dict["no_message"] = True
                messages.append(msg_dict)
        elif m.role == "system":
            messages.append({
                "role": "assistant",
                "content": f"[系统通知] {m.content}",
                "id": m.id,
                "created_at": m.created_at,
            })
        else:
            # user messages
            msg_dict: dict[str, Any] = {
                "role": m.role,
                "content": m.content,
                "id": m.id,
                "created_at": m.created_at,
            }
            if m.image_data:
                msg_dict["image_data"] = m.image_data
            messages.append(msg_dict)

    # Validate: every tool_calls message must have complete tool_results after it.
    # Also catch stray tool messages whose tool_use was stripped or summarized.
    validated: list[dict[str, Any]] = []
    valid_tc_ids: set[str] = set()  # tool_use IDs that survived validation
    i = 0
    while i < len(messages):
        msg = messages[i]
        if msg.get("tool_calls"):
            tc_ids = {tc.get("id") for tc in msg["tool_calls"]}
            # Collect following tool result messages
            j = i + 1
            following: list[dict[str, Any]] = []
            while j < len(messages) and messages[j].get("role") == "tool":
                following.append(messages[j])
                j += 1
            found_ids = {tr.get("tool_call_id") for tr in following}
            if tc_ids - found_ids:
                # Missing tool_results — strip tool_calls, fall back to text
                plain = {k: v for k, v in msg.items() if k != "tool_calls"}
                plain["content"] = plain.get("content") or "[工具调用]"
                validated.append(plain)
                for tr in following:
                    validated.append({
                        "role": "assistant",
                        "content": tr.get("content", ""),
                        "id": tr.get("id"),
                        "created_at": tr.get("created_at"),
                    })
                i = j
            else:
                # All paired — keep as-is
                valid_tc_ids.update(tc_ids)
                for k in range(i, j):
                    validated.append(messages[k])
                i = j
        elif msg.get("role") == "tool" and msg.get("tool_call_id"):
            # Stray tool message (not immediately after its tool_calls) —
            # its tool_use was stripped or summarized, convert to text
            if msg["tool_call_id"] not in valid_tc_ids:
                validated.append({
                    "role": "assistant",
                    "content": msg.get("content", ""),
                    "id": msg.get("id"),
                    "created_at": msg.get("created_at"),
                })
            else:
                validated.append(msg)
            i += 1
        else:
            validated.append(msg)
            i += 1

    return validated


@router.post("/chat/completions")
async def chat_completions(
    payload: ChatCompletionRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    # Resolve assistant
    session = db.get(ChatSession, payload.session_id)
    if session and session.assistant_id:
        assistant = db.get(Assistant, session.assistant_id)
    else:
        assistant = db.query(Assistant).first()
    assistant_name = assistant.name if assistant else "unknown"
    chat_service = ChatService(db, assistant_name, assistant_id=assistant.id if assistant else None, source="web")

    # Build messages list
    if payload.message is not None:
        messages = _load_session_messages(db, payload.session_id)
        # Only append non-empty user messages (empty = receive mode)
        if isinstance(payload.message, list) or (payload.message and payload.message.strip()):
            messages.append({"role": "user", "content": payload.message})
    elif payload.messages and any(m.get("role") == "system" for m in payload.messages):
        messages = payload.messages
    else:
        messages = _load_session_messages(db, payload.session_id)
        for m in payload.messages:
            content = m.get("content", "")
            # Only append non-empty user messages (empty = receive mode)
            if isinstance(content, list) or (content and content.strip()):
                messages.append({"role": "user", "content": content})

    # Convert tool_results to dicts for service layer
    tool_results_dicts = [tr.model_dump() for tr in payload.tool_results] if payload.tool_results else None

    if payload.stream:
        def generate():
            yield from chat_service.stream_chat_completion(
                payload.session_id, messages, background_tasks=background_tasks,
                short_mode=payload.short_mode, source=payload.source,
                tool_results=tool_results_dicts,
            )
        return StreamingResponse(generate(), media_type="text/event-stream")

    # Non-streaming path — run in threadpool so event loop stays free for
    # WebSocket COT broadcasts (call_soon_threadsafe needs a non-blocked loop)
    max_msg = (
        db.query(MessageModel.id)
        .filter(MessageModel.session_id == payload.session_id)
        .order_by(MessageModel.id.desc())
        .first()
    )
    max_id_before = max_msg[0] if max_msg else 0

    def _consume():
        for _ in chat_service.stream_chat_completion(
            payload.session_id, messages,
            background_tasks=background_tasks,
            short_mode=payload.short_mode, source=payload.source,
            tool_results=tool_results_dicts,
        ):
            pass

    await asyncio.to_thread(_consume)

    new_msgs = (
        db.query(MessageModel)
        .filter(
            MessageModel.session_id == payload.session_id,
            MessageModel.id > max_id_before,
            MessageModel.role == "assistant",
            MessageModel.content.isnot(None),
            MessageModel.content != "",
        )
        .order_by(MessageModel.id.asc())
        .all()
    )
    return ChatCompletionResponse(
        messages=[{"role": "assistant", "content": m.content} for m in new_msgs]
    )
