from __future__ import annotations

import json
import logging
import re
from typing import Any

from sqlalchemy.orm import Session

from app.cot_broadcaster import cot_broadcaster
from app.models.models import CotRecord, Message
from app.services.memory_service import ToolCall

logger = logging.getLogger(__name__)


def content_to_storage(content: str | list | None) -> str:
    """Convert multimodal content to the text-only message storage format."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text":
            parts.append(item.get("text", ""))
        elif item.get("type") == "image_url":
            image_id = item.get("image_id", "unknown")
            parts.append(f"[图片:{image_id}]")
        elif item.get("type") == "file":
            file_id = item.get("file_id", "unknown")
            file_name = item.get("file_name", "")
            parts.append(f"[文件:{file_id}:{file_name}]")
    return "".join(parts)


class ChatPersistence:
    def __init__(self, db: Session, assistant_id: int | None, source: str | None) -> None:
        self.db = db
        self.assistant_id = assistant_id
        self.source = source

    def persist_request_snapshot(
        self,
        request_id: str,
        round_index: int,
        provider: str,
        payload: dict,
        token_stats: dict,
        cache_bp_positions: dict,
    ) -> None:
        try:
            # Reorder payload keys so the snapshot shows the cache hierarchy
            # (tools -> system -> messages) rather than our dict insertion
            # order. Anthropic's cache layers follow this semantic order
            # regardless of JSON field order. Does not affect the API call.
            front = ["model", "max_tokens", "temperature", "top_p",
                     "thinking", "output_config", "reasoning", "stream"]
            back = ["tools", "tool_choice", "system", "messages"]
            ordered: dict = {}
            for k in front:
                if k in payload:
                    ordered[k] = payload[k]
            for k, v in payload.items():
                if k not in front and k not in back:
                    ordered[k] = v
            for k in back:
                if k in payload:
                    ordered[k] = payload[k]
            snapshot = {
                "provider": provider,
                "payload": ordered,
                "token_stats": token_stats,
                "cache_bp_positions": cache_bp_positions,
            }
            self.write_cot_block(
                request_id=request_id,
                round_index=round_index,
                block_type="request_payload",
                content=json.dumps(snapshot, ensure_ascii=False, default=str),
                tool_name=None,
                broadcast=False,
            )
        except Exception as exc:
            logger.warning("persist request snapshot failed (request_id=%s): %s", request_id, exc)

    def write_cot_block(
        self,
        request_id: str,
        round_index: int,
        block_type: str,
        content: str,
        tool_name: str | None = None,
        broadcast: bool = True,
    ) -> None:
        try:
            record = CotRecord(
                request_id=request_id,
                round_index=round_index,
                block_type=block_type,
                content=content,
                tool_name=tool_name,
                assistant_id=self.assistant_id,
            )
            self.db.add(record)
            self.db.commit()
            if broadcast:
                cot_broadcaster.publish({
                    "type": block_type,
                    "request_id": request_id,
                    "round_index": round_index,
                    "block_type": block_type,
                    "content": content,
                    "tool_name": tool_name,
                    "assistant_id": self.assistant_id,
                })
        except Exception as exc:
            logger.warning("Failed to write COT block (request_id=%s): %s", request_id, exc)
            try:
                self.db.rollback()
            except Exception:
                pass

    def persist_tool_call(self, session_id: int, tool_call: ToolCall) -> None:
        payload = {"tool_name": tool_call.name, "arguments": tool_call.arguments}
        self.persist_message(session_id, "assistant", "", {"tool_call": payload})

    def persist_tool_result(self, session_id: int, tool_name: str, tool_result: dict[str, Any]) -> None:
        content = json.dumps(tool_result, ensure_ascii=False)
        self.persist_message(session_id, "tool", content, {"tool_name": tool_name})

    def persist_message(
        self,
        session_id: int,
        role: str,
        content: str | list,
        metadata: dict[str, Any],
        request_id: str | None = None,
    ) -> Message:
        storage_content = content_to_storage(content)
        # Final safety net: strip all internal markers before DB write.
        if isinstance(storage_content, str) and role == "assistant":
            storage_content = re.sub(r'^\[\d{4}\.\d{2}\.\d{2}\s\d{2}:\d{2}:\d{2}\]\s*', '', storage_content)
            storage_content = re.sub(r'\[\[used:\s*\d+\s*\]\]', '', storage_content)
            storage_content = re.sub(r'\[#\s*\d+\s*\]', '', storage_content)
            storage_content = re.sub(r'\(来源:\s*\w+\)\s*$', '', storage_content, flags=re.MULTILINE)
            # Strip scratchpad / legacy [THINK] blocks before DB write. This
            # prevents the model from seeing its own past fake-thinking in
            # history and copying it.
            storage_content = re.sub(
                r'(?:\[THINK\]|<scratchpad>).*?(?:\[/THINK\]|</THINK>|</thinking>|</scratchpad>)',
                '',
                storage_content,
                flags=re.DOTALL,
            )
            for orphan in ('<scratchpad>', '</scratchpad>', '[THINK]', '[/THINK]', '</THINK>', '</thinking>'):
                storage_content = storage_content.replace(orphan, '')
            storage_content = storage_content.strip()
        if self.source and "source" not in metadata:
            metadata = {**metadata, "source": self.source}
        message = Message(
            session_id=session_id,
            role=role,
            content=storage_content,
            meta_info=metadata,
            request_id=request_id,
        )
        self.db.add(message)
        self.db.commit()
        self.db.refresh(message)
        if role == "user" and self.source in ("telegram", "qq", "wechat", "miniapp"):
            from app.services.proactive_service import touch_last_user_message_at
            touch_last_user_message_at(self.db)
        # Hint the front-end message list to refresh. Best-effort; any failure
        # here must not roll back the write or break the request.
        try:
            cot_broadcaster.publish({
                "type": "messages_updated",
                "assistant_id": self.assistant_id,
                "session_id": session_id,
                "message_id": message.id,
                "role": role,
            })
        except Exception as exc:
            logger.debug("[messages_updated broadcast] failed: %s", exc)
        return message
