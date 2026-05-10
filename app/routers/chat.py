from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import Assistant, ChatSession, CotRecord, Message as MessageModel, Settings as SettingsModel
from app.services.chat_service import ChatService
from app.services.format_converters import _TOOL_CACHE_THRESHOLD

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



def _strip_ts_prefix(s: str) -> str:
    """Strip leading [YYYY.MM.DD HH:MM] timestamp prefix from memory content."""
    if s.startswith("[") and "]" in s[:22]:
        return s[s.index("]") + 1:].strip()
    return s


def _compress_tool_result(tool_name: str, content: str) -> str:
    """Compress small tool results into a short summary.

    Results longer than _TOOL_CACHE_THRESHOLD are returned as-is
    (extract_tool_cache will handle them later).
    """
    if len(content) > _TOOL_CACHE_THRESHOLD:
        return content

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
        c = _strip_ts_prefix(str(data.get("content", "")))[:15]
        if data.get("duplicate"):
            return f"[已存储记忆] 重复, existing_id={data.get('existing_id', '?')}"
        return f"[已存储记忆] id={mid}, {c}..."

    if tool_name == "update_memory":
        c = _strip_ts_prefix(str(data.get("content", "")))[:15]
        return f"[已更新记忆] id={data.get('id', '?')}, {c}..."

    if tool_name == "delete_memory":
        c = str(data.get("content", ""))
        return f"[已删除记忆] id={data.get('id', '?')}, {c}"

    if tool_name == "diary":
        title = str(data.get("title", ""))[:15]
        return f"[已写日记] id={data.get('id', '?')}, {title}"

    if tool_name == "reminder":
        if "reminders" in data or "pending_reminders" in data:
            reminders = data.get("reminders") or data.get("pending_reminders", [])
            if not reminders:
                return "[提醒列表] 无"
            lines = []
            for r in reminders:
                lines.append(f"id={r.get('id', '?')} {r.get('reason', '')} ({r.get('remaining_minutes', '?')}分钟后)")
            return "[提醒列表] " + "; ".join(lines)
        if data.get("id"):
            reason = str(data.get("reason", ""))[:15]
            return f"[已设置提醒] id={data.get('id', '?')}, {reason}"
        return f"[闹钟] {data.get('message', data.get('status', ''))}"

    if tool_name == "memo":
        return f"[备忘录] {data.get('message', '')}"

    # Legacy compatibility for old tool names in history
    if tool_name == "write_diary":
        title = str(data.get("title", ""))[:15]
        return f"[已写日记] id={data.get('id', '?')}, {title}"
    if tool_name == "read_diary":
        if data.get("diaries"):
            return f"[日记列表] {data.get('total', '?')}篇"
        if data.get("content"):
            return f"[读日记] #{data.get('id', '?')} {data.get('title', '')}"
        return content
    if tool_name in ("set_reminder", "cancel_reminder", "list_reminders"):
        if "reminders" in data or "pending_reminders" in data:
            reminders = data.get("reminders") or data.get("pending_reminders", [])
            if not reminders:
                return "[提醒列表] 无"
            lines = []
            for r in reminders:
                lines.append(f"id={r.get('id', '?')} {r.get('reason', '')} ({r.get('remaining_minutes', '?')}分钟后)")
            return "[提醒列表] " + "; ".join(lines)
        if data.get("id"):
            return f"[已设置提醒] id={data.get('id', '?')}, {str(data.get('reason', ''))[:15]}"
        return f"[闹钟] {data.get('message', data.get('status', ''))}"
    if tool_name == "web_search":
        return f"[搜索] {data.get('query', '')}"
    if tool_name == "web_fetch":
        return f"[读取网页] {str(data.get('url', ''))[:40]}"
    if tool_name in ("view_image", "view_file"):
        return content

    if tool_name == "get_memory_by_id":
        c = str(data.get("content", ""))
        klass = data.get("klass", "")
        return f"[记忆#{data.get('id', '?')}] ({klass}) {c}"

    if tool_name in ("forum_cli", "forum_guide"):
        result_text = data.get("result", "")
        if result_text:
            return result_text
        return content

    # Fallback: 未明确处理的工具，返回原始内容交给 extract_tool_cache
    return content


def _expire_stale_tool_results(messages: list[dict[str, Any]], db: Session) -> int:
    """Shrink old tool results (> configured hours) in the messages list.

    Replaces content with `{_build_tool_index(...)} [已过期]` — same visual
    format as the tool-cache-over-30k drop-oldest path. Operates on the returned
    messages list only; DB content is preserved for UI history.

    All tool types including cafe_chat follow the same time-based expiry.
    Returns the number of messages replaced.
    """
    from datetime import datetime, timedelta, timezone
    from app.services.format_converters import _build_tool_index

    # Read expiry threshold from settings (default 24h)
    row = db.query(SettingsModel).filter(SettingsModel.key == "tool_result_expire_hours").first()
    try:
        hours = int(row.value) if row and row.value else 24
    except (ValueError, TypeError):
        hours = 24
    if hours <= 0:
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    _EXCLUDED: set[str] = set()
    replaced = 0
    for msg in messages:
        if msg.get("role") != "tool":
            continue
        tool_name = msg.get("name") or (msg.get("meta_info") or {}).get("tool_name") or "unknown"
        if tool_name in _EXCLUDED:
            continue
        content = msg.get("content", "")
        if not isinstance(content, str) or not content.startswith("{"):
            # already compressed / short / expired
            continue
        created = msg.get("created_at")
        if not created:
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if created > cutoff:
            continue
        index_text = _build_tool_index(tool_name, content, len(content))
        msg["content"] = f"{index_text} [已过期]"
        replaced += 1
    return replaced


def _inject_recent_scratchpad(messages: list[dict[str, Any]], db: Session) -> int:
    """Inject scratchpad/thinking_fake content from cot_records back into recent
    assistant messages, so the model has cross-request continuity for follow-ups
    (esp. proactive flows where it needs to recall what it just did).

    Cutoff = `tool_result_expire_hours` (default 24h, shared with tool expiry).
    Older messages are not injected — keeps prompt bounded and avoids the
    "model sees its own old draft and stops thinking" pollution.

    For each unique request_id, all thinking_fake records (across rounds) are
    concatenated and prepended (wrapped in <scratchpad>...</scratchpad>) to the
    earliest assistant message of that request. DB content is not modified.
    Returns number of assistant messages whose content was modified.
    """
    from datetime import datetime, timedelta, timezone

    row = db.query(SettingsModel).filter(SettingsModel.key == "tool_result_expire_hours").first()
    try:
        hours = int(row.value) if row and row.value else 24
    except (ValueError, TypeError):
        hours = 24
    if hours <= 0:
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    # Map request_id → ordered list of assistant messages (by created_at).
    # Index in list corresponds to round_index.
    rid_to_assistants: dict[str, list[dict[str, Any]]] = {}
    recent_rids: set[str] = set()
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        rid = msg.get("request_id")
        if not rid:
            continue
        rid = str(rid)
        created = msg.get("created_at")
        if not created:
            continue
        if isinstance(created, datetime) and created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if created < cutoff:
            continue
        recent_rids.add(rid)
        rid_to_assistants.setdefault(rid, []).append(msg)

    if not recent_rids:
        return 0

    # Batch fetch all thinking_fake records
    rows = (
        db.query(CotRecord)
        .filter(
            CotRecord.request_id.in_(list(recent_rids)),
            CotRecord.block_type == "thinking_fake",
        )
        .order_by(CotRecord.request_id, CotRecord.round_index, CotRecord.id)
        .all()
    )
    # Group by (request_id, round_index)
    by_rid_round: dict[tuple[str, int], list[str]] = {}
    for r in rows:
        text = (r.content or "").strip()
        if text:
            by_rid_round.setdefault((r.request_id, r.round_index), []).append(text)

    injected = 0
    for (rid, round_idx), parts in by_rid_round.items():
        assistants = rid_to_assistants.get(rid)
        if not assistants or round_idx >= len(assistants):
            continue
        target = assistants[round_idx]
        scratchpad_text = "\n---\n".join(parts).strip()
        if not scratchpad_text:
            continue
        original = target.get("content")
        scratchpad_block = f"<scratchpad>\n{scratchpad_text}\n</scratchpad>"
        if isinstance(original, str):
            target["content"] = f"{scratchpad_block}\n\n{original}".strip()
        elif isinstance(original, list):
            target["content"] = [{"type": "text", "text": scratchpad_block}] + original
        else:
            target["content"] = scratchpad_block
        injected += 1
    return injected



def _reorder_after_request_anchor(db_msgs: list) -> list:
    """Re-order user messages that arrived while a generation was running.
    Those messages have meta.after_request_id set to the running request's id;
    DB-id order would place them before the request's assistant reply (since
    user rows are inserted immediately but assistant rows are persisted only
    after the stream finishes). Move each such user message to sit after the
    last message of its anchor request_id, so the model sees:
        user(batch1) → assistant(R1 reply) → user(arrived during R1) → ...
    instead of user(batch1+arrived-during-R1 merged) → assistant(R1)."""
    pending_by_rid: dict[str, list] = {}
    regular: list = []
    for m in db_msgs:
        meta = m.meta_info or {}
        after_rid = meta.get("after_request_id")
        if m.role == "user" and after_rid:
            pending_by_rid.setdefault(after_rid, []).append(m)
        else:
            regular.append(m)
    if not pending_by_rid:
        return db_msgs
    result: list = []
    for i, m in enumerate(regular):
        result.append(m)
        curr_rid = m.request_id
        if not curr_rid or curr_rid not in pending_by_rid:
            continue
        is_last_of_rid = (i + 1 >= len(regular)) or (regular[i + 1].request_id != curr_rid)
        if is_last_of_rid:
            result.extend(pending_by_rid.pop(curr_rid))
    # Anchor request timed out or was summarized — strip after_request_id
    # and insert by created_at so they stay in chronological position.
    if pending_by_rid:
        orphans = [m for msgs in pending_by_rid.values() for m in msgs]
        for m in orphans:
            if m.meta_info and "after_request_id" in m.meta_info:
                m.meta_info = {k: v for k, v in m.meta_info.items() if k != "after_request_id"}
        for orphan in orphans:
            inserted = False
            for j in range(len(result) - 1, -1, -1):
                if hasattr(result[j], 'id') and hasattr(orphan, 'id') and result[j].id < orphan.id:
                    result.insert(j + 1, orphan)
                    inserted = True
                    break
            if not inserted:
                result.insert(0, orphan)
    return result


_TZ8 = timezone(timedelta(hours=8))
_WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _inject_date_dividers(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Insert date divider pseudo-messages between messages on different days."""
    result: list[dict[str, Any]] = []
    prev_date: str | None = None
    for msg in messages:
        ca = msg.get("created_at")
        if ca and isinstance(ca, datetime):
            dt = ca.astimezone(_TZ8)
            cur_date = dt.strftime("%Y-%m-%d")
            if prev_date and cur_date != prev_date:
                wd = _WEEKDAYS[dt.weekday()]
                label = f"── {dt.month}月{dt.day}日（{wd}）──"
                result.append({"role": "user", "content": label, "_date_divider": True})
            prev_date = cur_date
        result.append(msg)
    return result


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
    db_msgs = _reorder_after_request_anchor(db_msgs)
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
                    "meta_info": meta,
                    "request_id": m.request_id,
                }
                # Restore thinking blocks (signature only) for cross-request visibility
                if "_thinking_blocks" in meta:
                    msg_dict["_thinking_blocks"] = [
                        {"type": b["type"], "thinking": "", "signature": b["signature"]}
                        if b.get("type") == "thinking" and b.get("signature")
                        else b
                        for b in meta["_thinking_blocks"]
                    ]
                if _no_msg:
                    msg_dict["no_message"] = True
                messages.append(msg_dict)
            elif "tool_call" in meta:
                # Individual tool call record (redundant with bulk) — skip
                pass
            elif meta.get("cafe_reply") or meta.get("qq_group_reply"):
                # UI-visible "[TG群回复] / [QQ群回复]" duplicates of the tool-call output.
                # The model already sees the text via the tool_call payload, so skip
                # here to avoid loading the same reply twice into history.
                pass
            elif m.content and m.content.strip():
                msg_dict = {
                    "role": m.role,
                    "content": m.content,
                    "id": m.id,
                    "created_at": m.created_at,
                    "meta_info": meta,
                    "request_id": m.request_id,
                }
                if _no_msg:
                    msg_dict["no_message"] = True
                if "_thinking_blocks" in meta:
                    msg_dict["_thinking_blocks"] = [
                        {"type": b["type"], "thinking": "", "signature": b["signature"]}
                        if b.get("type") == "thinking" and b.get("signature")
                        else b
                        for b in meta["_thinking_blocks"]
                    ]
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
            compressed = _compress_tool_result(tool_name, m.content or "")
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
                "role": "user",
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

    # Expire old (>24h by default) tool results to keep context bounded
    # when summarization hasn't kicked in for a while.
    _n_expired = _expire_stale_tool_results(validated, db)
    if _n_expired:
        logger.info("[load] expired %d stale tool results", _n_expired)

    # Inject recent (<24h) scratchpad / thinking_fake content from cot_records
    # back into assistant messages, so the model has cross-request continuity
    # for follow-up turns (esp. proactive). DB messages.content stays clean.
    _n_injected = _inject_recent_scratchpad(validated, db)
    if _n_injected:
        logger.info("[load] injected scratchpad into %d assistant messages", _n_injected)

    # Keep thinking blocks (signature) only on the most recent assistant message
    _kept = 0
    for _msg in reversed(validated):
        if _msg.get("role") != "assistant":
            continue
        if "_thinking_blocks" not in _msg:
            continue
        _kept += 1
        if _kept > 1:
            del _msg["_thinking_blocks"]

    # Inject date dividers between messages on different days
    validated = _inject_date_dividers(validated)

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
