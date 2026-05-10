"""MemoryService – extracted from chat_service.py.

Handles memory CRUD, diary, search (memory / summary / chat history / theater),
and fast_recall with decay-score reranking.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any

from sqlalchemy.orm import Session
from sqlalchemy import text

from app.models.models import (
    Assistant,
    Diary,
    Memory,
    MemoryVersion,
    Message,
    PendingMemory,
    SessionSummary,
    Settings,
    TheaterStory,
)
from app.services.embedding_service import EmbeddingService
from app.constants import KLASS_DEFAULTS
from app.services.mood_detection import (
    NEGATIVE_MOOD_TAGS,
    _load_emotion_config,
)
from app.utils import TZ_EAST8

logger = logging.getLogger(__name__)


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]
    id: str | None = None


class MemoryService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.embedding_service = EmbeddingService()

    def save_memory(self, payload: dict[str, Any]) -> dict[str, Any]:
        content = payload.get("content", "")
        if len(content) > 120:
            trimmed = content.rstrip("。！？…、，,.!?")
            if len(trimmed) <= 120:
                content = trimmed
            else:
                return {"error": f"内容{len(content)}字，超过120字上限，请精简后重试"}
        raw_klass = payload.get("klass", "other")
        klass = raw_klass if raw_klass in KLASS_DEFAULTS else "other"
        klass_config = KLASS_DEFAULTS[klass]
        now_east8 = datetime.now(TZ_EAST8)
        content = f"[{now_east8.strftime('%Y.%m.%d %H:%M')}] {content}"
        disclosure = str(payload.get("disclosure", "")).strip() or None
        embed_text = f"{content} {disclosure}" if disclosure else content
        embedding = self.embedding_service.get_embedding(embed_text)
        source = payload.get("source", "unknown")

        # Deduplication check: find similar memories with similarity > 0.88
        if embedding is not None:
            dup_sql = text(
                """
    SELECT id, content, source, 1 - (embedding <=> :query_embedding) AS similarity
    FROM memories
    WHERE embedding IS NOT NULL
      AND deleted_at IS NULL
      AND 1 - (embedding <=> :query_embedding) > 0.88
    ORDER BY embedding <=> :query_embedding
    LIMIT 1
"""
            )
            dup_result = self.db.execute(
                dup_sql, {"query_embedding": str(embedding)}
            ).first()
            if dup_result:
                # If source is from auto_extract, silently discard
                if source.startswith("auto_extract"):
                    return {
                        "duplicate": True,
                        "discarded": True,
                        "existing_id": dup_result.id,
                    }
                # If source is main model (assistant name), return duplicate info
                # Check if content differs despite high similarity (possible update)
                result: dict[str, Any] = {
                    "duplicate": True,
                    "existing_id": dup_result.id,
                    "existing_content": dup_result.content,
                }
                if dup_result.content.strip() != content.strip():
                    result["hint"] = "内容相似但有变化，如需更新请调用 update_memory(id={}, content=新内容)".format(dup_result.id)
                return result

        memory = Memory(
            content=content,
            tags=payload.get("tags", {}),
            source=source,
            embedding=embedding,
            klass=klass,
            importance=klass_config["importance"],
            halflife_days=klass_config["halflife_days"],
            disclosure=disclosure,
        )
        self.db.add(memory)
        self.db.commit()
        self.db.refresh(memory)
        result = {
            "id": memory.id,
            "content": memory.content,
            "klass": memory.klass,
            "tags": memory.tags,
            "source": memory.source,
        }
        if memory.disclosure:
            result["disclosure"] = memory.disclosure
        return result

    def update_memory(self, payload: dict[str, Any]) -> dict[str, Any]:
        memory_id = payload.get("id")
        source = payload.get("source", "unknown")
        if memory_id is None:
            return {"error": "id is required"}
        memory = self.db.get(Memory, memory_id)
        if not memory:
            return {"error": "memory not found"}
        # Permission check: can modify own memories and auto_extract:own_name memories
        allowed_sources = {source, "unknown", f"auto_extract:{source}"}
        if memory.source not in allowed_sources and not memory.source.startswith(f"auto_extract:{source}"):
            return {"error": "permission denied"}
        # Snapshot before overwrite
        self.db.add(MemoryVersion(
            memory_id=memory.id,
            content=memory.content,
            klass=memory.klass,
            tags=memory.tags,
            disclosure=memory.disclosure,
            changed_by=f"assistant:{source}",
        ))
        if "content" in payload:
            new_content = payload["content"]
            now_east8 = datetime.now(TZ_EAST8)
            memory.content = f"[{now_east8.strftime('%Y.%m.%d %H:%M')}] {new_content}"
        if "klass" in payload:
            new_klass = payload["klass"]
            if new_klass in KLASS_DEFAULTS:
                memory.klass = new_klass
                memory.importance = KLASS_DEFAULTS[new_klass]["importance"]
                memory.halflife_days = KLASS_DEFAULTS[new_klass]["halflife_days"]
        if "tags" in payload:
            memory.tags = payload["tags"]
        if "disclosure" in payload:
            memory.disclosure = str(payload["disclosure"]).strip() or None
        # Re-generate embedding if content or disclosure changed
        if "content" in payload or "disclosure" in payload:
            embed_text = memory.content
            if memory.disclosure:
                embed_text = f"{embed_text} {memory.disclosure}"
            new_embedding = self.embedding_service.get_embedding(embed_text)
            if new_embedding is not None:
                memory.embedding = new_embedding
        memory.updated_at = datetime.now(TZ_EAST8)
        self.db.commit()
        self.db.refresh(memory)
        result = {
            "id": memory.id,
            "content": memory.content,
            "klass": memory.klass,
            "tags": memory.tags,
            "source": memory.source,
        }
        if memory.disclosure:
            result["disclosure"] = memory.disclosure
        return result

    def delete_memory(self, payload: dict[str, Any]) -> dict[str, Any]:
        memory_id = payload.get("id")
        source = payload.get("source", "unknown")
        if memory_id is None:
            return {"error": "id is required"}
        memory = self.db.get(Memory, memory_id)
        if not memory:
            return {"error": "memory not found"}
        if memory.deleted_at is not None:
            return {"error": "memory already deleted"}
        # Permission check: can delete own memories and auto_extract:own_name memories
        allowed_sources = {source, "unknown", f"auto_extract:{source}"}
        if memory.source not in allowed_sources and not memory.source.startswith(f"auto_extract:{source}"):
            return {"error": "permission denied"}
        deleted_content = memory.content or ""
        memory.deleted_at = datetime.now(TZ_EAST8)
        self.db.commit()
        return {"status": "deleted", "id": memory_id, "content": deleted_content}

    def write_diary(self, payload: dict[str, Any]) -> dict[str, Any]:
        unlock_at = None
        raw_unlock = payload.get("unlock_at")
        if raw_unlock:
            try:
                unlock_at = datetime.fromisoformat(raw_unlock)
                if unlock_at.tzinfo is None:
                    unlock_at = unlock_at.replace(tzinfo=timezone(timedelta(hours=8)))
                unlock_at = unlock_at.astimezone(timezone.utc)
            except (ValueError, TypeError):
                unlock_at = None
        diary = Diary(
            assistant_id=payload.get("assistant_id"),
            author="assistant",
            title=payload.get("title", ""),
            content=payload.get("content", ""),
            is_read=False,
            unlock_at=unlock_at,
        )
        self.db.add(diary)
        self.db.commit()
        self.db.refresh(diary)
        result: dict[str, Any] = {"id": diary.id, "title": diary.title}
        if unlock_at:
            result["unlock_at"] = self._format_time_east8(unlock_at)
        return result

    def _get_author_name(self, author: str, assistant_id: int | None = None) -> str:
        """Resolve author to display name."""
        if author == "user":
            row = self.db.query(Settings).filter(Settings.key == "user_nickname").first()
            return row.value if row and row.value else "user"
        if author == "assistant":
            if assistant_id:
                asst = self.db.query(Assistant).filter(Assistant.id == assistant_id).first()
                if asst:
                    return asst.name
            return "assistant"
        return author

    def read_diary(self, payload: dict[str, Any]) -> dict[str, Any]:
        diary_id = payload.get("diary_id")
        now = datetime.now(TZ_EAST8)
        if diary_id:
            diary = self.db.query(Diary).filter(Diary.id == diary_id, Diary.deleted_at.is_(None)).first()
            if not diary:
                return {"error": "日记不存在"}
            if diary.unlock_at and diary.unlock_at > now:
                return {"error": "该日记尚未解锁", "unlock_at": self._format_time_east8(diary.unlock_at)}
            if diary.author == "user":
                diary.read_at = now
                self.db.commit()
            return {
                "id": diary.id, "title": diary.title, "content": diary.content,
                "author": self._get_author_name(diary.author, diary.assistant_id),
                "created_at": self._format_time_east8(diary.created_at),
                "unlock_at": self._format_time_east8(diary.unlock_at),
                "_cache_key": f"read_diary:read:{diary.id}",
            }
        else:
            query = self.db.query(Diary).filter(Diary.deleted_at.is_(None))
            author = payload.get("author")
            if author:
                query = query.filter(Diary.author == author)
            limit = min(int(payload.get("limit") or 50), 50)
            rows = query.order_by(Diary.created_at.desc()).limit(limit).all()
            items = []
            for r in rows:
                locked = bool(r.unlock_at and r.unlock_at > now)
                items.append({
                    "id": r.id, "title": r.title, "author": self._get_author_name(r.author, r.assistant_id),
                    "created_at": self._format_time_east8(r.created_at),
                    "unlock_at": self._format_time_east8(r.unlock_at),
                    "read_at": self._format_time_east8(getattr(r, "read_at", None)),
                    "locked": locked,
                })
            _ck = f"read_diary:list:{author or 'all'}"
            return {"diaries": items, "total": len(items), "_cache_key": _ck}

    @staticmethod
    def _format_time_east8(value: datetime | None) -> str | None:
        if value is None:
            return None
        if value.tzinfo is None:
            utc_value = value.replace(tzinfo=TZ_EAST8)
        else:
            utc_value = value.astimezone(timezone.utc)
        return utc_value.astimezone(TZ_EAST8).strftime("%Y.%m.%d %H:%M")

    @staticmethod
    def _parse_iso_datetime(raw_value: Any) -> datetime | None:
        if raw_value is None:
            return None
        try:
            import re as _re
            text_value = str(raw_value).strip()
            if not text_value:
                return None
            # Normalize common non-ISO formats the model might produce
            # "2025.2.20" or "2025.02.20" → "2025-02-20"
            m = _re.match(r'^(\d{4})[./年](\d{1,2})[./月](\d{1,2})[日]?$', text_value)
            if m:
                text_value = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
            # "2025.2.20 14:30" style
            m2 = _re.match(r'^(\d{4})[./年](\d{1,2})[./月](\d{1,2})[日]?\s+(\d{1,2}:\d{2}(?::\d{2})?)$', text_value)
            if m2:
                text_value = f"{m2.group(1)}-{int(m2.group(2)):02d}-{int(m2.group(3)):02d}T{m2.group(4)}"
            parsed = datetime.fromisoformat(text_value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                # Treat naive datetimes as East8 (China), not UTC
                parsed = parsed.replace(tzinfo=TZ_EAST8)
            return parsed.astimezone(timezone.utc)
        except Exception:
            logger.warning("Failed to parse datetime: %r", raw_value)
            return None

    def list_memories(self, payload: dict[str, Any]) -> dict[str, Any]:
        start_pos = payload.get("start")
        end_pos = payload.get("end")

        # Position-based mode (for reflection): 1=oldest
        if start_pos is not None and end_pos is not None:
            start_pos = max(1, int(start_pos))
            end_pos = int(end_pos)
            if end_pos - start_pos + 1 > 100:
                end_pos = start_pos + 99
            offset = start_pos - 1
            limit = end_pos - start_pos + 1
            sql = text("""
    SELECT id, content, tags, klass, disclosure, source, created_at
    FROM memories
    WHERE deleted_at IS NULL AND is_pending = FALSE
    ORDER BY created_at ASC
    LIMIT :limit OFFSET :offset
""")
            rows = self.db.execute(sql, {"limit": limit, "offset": offset}).all()
            total = self.db.execute(text(
                "SELECT count(*) FROM memories WHERE deleted_at IS NULL AND is_pending = FALSE"
            )).scalar() or 0
            results = [
                {
                    "id": row.id,
                    "content": row.content,
                    "tags": row.tags,
                    "klass": row.klass,
                    "disclosure": row.disclosure,
                    "source": row.source,
                    "created_at": self._format_time_east8(row.created_at),
                }
                for row in rows
            ]
            return {"results": results, "total": total, "range": f"{start_pos}-{end_pos}"}

        # Time/klass-based mode (original)
        start_time = self._parse_iso_datetime(payload.get("start_time"))
        end_time = self._parse_iso_datetime(payload.get("end_time"))
        klass = payload.get("klass")
        try:
            limit = min(20, max(1, int(payload.get("limit", 10))))
        except Exception:
            limit = 10

        if start_time and end_time and start_time > end_time:
            start_time, end_time = end_time, start_time

        where_clauses = ["deleted_at IS NULL", "is_pending = FALSE"]
        params: dict[str, Any] = {"limit": limit}

        if start_time is not None:
            where_clauses.append("created_at >= :start_time")
            params["start_time"] = start_time
        if end_time is not None:
            where_clauses.append("created_at <= :end_time")
            params["end_time"] = end_time
        if klass:
            where_clauses.append("klass = :klass")
            params["klass"] = klass

        where_clause = " AND ".join(where_clauses)
        sql = text(
            f"""
    SELECT id, content, tags, klass, disclosure, source, created_at
    FROM memories
    WHERE {where_clause}
    ORDER BY created_at DESC
    LIMIT :limit
"""
        )
        rows = self.db.execute(sql, params).all()

        results = [
            {
                "id": row.id,
                "content": row.content,
                "tags": row.tags,
                "klass": row.klass,
                "disclosure": row.disclosure,
                "source": row.source,
                "created_at": self._format_time_east8(row.created_at),
            }
            for row in rows
        ]
        return {"results": results}

    def get_memory_by_id(self, payload: dict[str, Any]) -> dict[str, Any]:
        memory_id = payload.get("id")
        if memory_id is None:
            return {"error": "id is required"}
        memory = self.db.get(Memory, memory_id)
        if not memory:
            return {"error": "memory not found"}
        if memory.deleted_at is not None:
            return {"error": "memory has been deleted"}
        return {
            "id": memory.id,
            "content": memory.content,
            "tags": memory.tags,
            "klass": memory.klass,
            "source": memory.source,
            "importance": memory.importance,
            "created_at": self._format_time_east8(memory.created_at),
            "updated_at": self._format_time_east8(memory.updated_at),
        }

    def search_memory(self, payload: dict[str, Any]) -> dict[str, Any]:
        query = str(payload.get("query", "") or "").strip()
        source = payload.get("source")
        query_vector = self.embedding_service.get_embedding(query) if query else None

        # Vector search top 10
        vector_rows = []
        if query_vector is not None:
            vector_where = "WHERE embedding IS NOT NULL AND deleted_at IS NULL AND is_pending = FALSE"
            vector_params = {"query_embedding": str(query_vector)}
            if source and source != "all":
                vector_where += " AND source = :source"
                vector_params["source"] = source

            vector_sql = text(
                """
    SELECT id, content, tags, klass, created_at
    FROM memories
    {vector_where}
    ORDER BY embedding <=> :query_embedding
    LIMIT 10
""".format(vector_where=vector_where)
            )
            vector_rows = self.db.execute(vector_sql, vector_params).all()

        # Pgroonga full-text search top 10
        pgroonga_rows = []
        if query:
            pgroonga_where = "WHERE deleted_at IS NULL AND is_pending = FALSE AND search_text &@~ :query"
            pgroonga_params = {"query": query}
            if source and source != "all":
                pgroonga_where += " AND source = :source"
                pgroonga_params["source"] = source

            pgroonga_sql = text(
                """
    SELECT id, content, tags, klass, created_at
    FROM memories
    {pgroonga_where}
    ORDER BY pgroonga_score(tableoid, ctid) DESC
    LIMIT 10
""".format(pgroonga_where=pgroonga_where)
            )
            pgroonga_rows = self.db.execute(pgroonga_sql, pgroonga_params).all()

        # Merge by memory id, deduplicate
        results = []
        seen_ids = set()
        for row in vector_rows:
            if row.id in seen_ids:
                continue
            seen_ids.add(row.id)
            results.append(
                {
                    "id": row.id,
                    "content": row.content or "",
                    "tags": row.tags,
                    "klass": row.klass,
                    "created_at": self._format_time_east8(row.created_at),
                }
            )

        for row in pgroonga_rows:
            if row.id in seen_ids:
                continue
            seen_ids.add(row.id)
            results.append(
                {
                    "id": row.id,
                    "content": row.content or "",
                    "tags": row.tags,
                    "klass": row.klass,
                    "created_at": self._format_time_east8(row.created_at),
                }
            )

        # Cap at 10 results to avoid bloating assistant context
        results = results[:10]

        return {"query": query, "results": results, "_cache_key": f"search_memory:{query}"}

    def search_summary(self, payload: dict[str, Any]) -> dict[str, Any]:
        query = str(payload.get("query", "") or "").strip()
        limit = min(max(1, int(payload.get("limit", 5) or 5)), 10)
        try:
            offset = max(0, int(payload.get("offset", 0) or 0))
        except Exception:
            offset = 0
        assistant_id = payload.get("assistant_id")
        start_time = self._parse_iso_datetime(payload.get("start_time"))
        end_time = self._parse_iso_datetime(payload.get("end_time"))
        if start_time and end_time and start_time > end_time:
            start_time, end_time = end_time, start_time

        if not query:
            return {"query": query, "total": 0, "offset": offset, "limit": limit, "results": []}

        # Pgroonga search on session_summaries
        pgroonga_where = "WHERE summary_content &@~ :query AND deleted_at IS NULL"
        pgroonga_params: dict[str, Any] = {"query": query, "limit": limit, "offset": offset}
        if assistant_id is not None:
            pgroonga_where += " AND assistant_id = :assistant_id"
            pgroonga_params["assistant_id"] = assistant_id
        if start_time is not None:
            pgroonga_where += " AND time_end >= :start_time"
            pgroonga_params["start_time"] = start_time
        if end_time is not None:
            pgroonga_where += " AND time_start <= :end_time"
            pgroonga_params["end_time"] = end_time

        # Count total matches
        count_sql = text(
            "SELECT COUNT(*) FROM session_summaries {pgroonga_where}".format(
                pgroonga_where=pgroonga_where
            )
        )
        total = self.db.execute(count_sql, pgroonga_params).scalar() or 0

        pgroonga_sql = text(
            """
    SELECT id, summary_content, session_id, assistant_id, msg_id_start, msg_id_end,
           time_start, time_end, mood_tag
    FROM session_summaries
    {pgroonga_where}
    ORDER BY pgroonga_score(tableoid, ctid) DESC
    LIMIT :limit OFFSET :offset
""".format(pgroonga_where=pgroonga_where)
        )
        rows = self.db.execute(pgroonga_sql, pgroonga_params).all()

        results = [
            {
                "id": row.id,
                "summary_content": row.summary_content,
                "session_id": row.session_id,
                "assistant_id": row.assistant_id,
                "msg_id_start": row.msg_id_start,
                "msg_id_end": row.msg_id_end,
                "time_start": self._format_time_east8(row.time_start),
                "time_end": self._format_time_east8(row.time_end),
                "mood_tag": row.mood_tag,
            }
            for row in rows
        ]
        return {"query": query, "total": total, "offset": offset, "limit": limit, "results": results, "_cache_key": f"search_summary:{query}"}

    def get_summary_by_id(self, payload: dict[str, Any]) -> dict[str, Any]:
        summary_id = payload.get("id")
        if summary_id is None:
            return {"error": "id is required"}
        row = self.db.get(SessionSummary, summary_id)
        if not row or row.deleted_at is not None:
            return {"error": "summary not found"}
        return {
            "id": row.id,
            "summary_content": row.summary_content,
            "session_id": row.session_id,
            "msg_id_start": row.msg_id_start,
            "msg_id_end": row.msg_id_end,
            "time_start": self._format_time_east8(row.time_start),
            "time_end": self._format_time_east8(row.time_end),
            "mood_tag": row.mood_tag,
        }

    def search_chat_history(self, payload: dict[str, Any]) -> dict[str, Any]:
        query = str(payload.get("query", "") or "").strip()
        session_id = payload.get("session_id")
        try:
            msg_id_start = (
                int(payload.get("msg_id_start"))
                if payload.get("msg_id_start") is not None
                else None
            )
        except Exception:
            msg_id_start = None
        try:
            msg_id_end = (
                int(payload.get("msg_id_end"))
                if payload.get("msg_id_end") is not None
                else None
            )
        except Exception:
            msg_id_end = None
        try:
            message_id = (
                int(payload.get("message_id"))
                if payload.get("message_id") is not None
                else None
            )
        except Exception:
            message_id = None
        try:
            offset = max(0, int(payload.get("offset", 0) or 0))
        except Exception:
            offset = 0

        if (
            msg_id_start is not None
            and msg_id_end is not None
            and msg_id_start > msg_id_end
        ):
            msg_id_start, msg_id_end = msg_id_end, msg_id_start

        context_size = 3
        ID_RANGE_LIMIT = 20

        # Mode 1: ID range mode (max 20 messages)
        use_id_range = msg_id_start is not None and msg_id_end is not None
        if use_id_range:
            messages_query = self.db.query(Message).filter(
                Message.role.in_(["user", "assistant"]),
                Message.content.is_not(None),
                Message.content != "",
            )
            if session_id is not None:
                messages_query = messages_query.filter(Message.session_id == session_id)
            total_in_range = (
                messages_query.filter(Message.id.between(msg_id_start, msg_id_end))
                .count()
            )
            hit_messages = (
                messages_query.filter(Message.id.between(msg_id_start, msg_id_end))
                .order_by(Message.id.asc())
                .limit(ID_RANGE_LIMIT)
                .all()
            )
            results = [
                {
                    "id": message.id,
                    "session_id": message.session_id,
                    "role": message.role,
                    "content": message.content,
                    "created_at": self._format_time_east8(message.created_at),
                }
                for message in hit_messages
            ]
            return {
                "query": query,
                "total": total_in_range,
                "limit": ID_RANGE_LIMIT,
                "results": results,
                "mode": "id_range",
                "_cache_key": f"search_chat:range:{msg_id_start}:{msg_id_end}",
            }

        # Mode 2: Single message ID mode (returns message + 3 before + 3 after)
        if message_id is not None:
            target_message = self.db.get(Message, message_id)
            if not target_message:
                return {
                    "query": query,
                    "results": [],
                    "mode": "message_id",
                }
            # Get 3 messages before
            prev_messages = (
                self.db.query(Message)
                .filter(
                    Message.session_id == target_message.session_id,
                    Message.id < message_id,
                )
                .order_by(Message.id.desc())
                .limit(context_size)
                .all()
            )
            # Get 3 messages after
            next_messages = (
                self.db.query(Message)
                .filter(
                    Message.session_id == target_message.session_id,
                    Message.id > message_id,
                )
                .order_by(Message.id.asc())
                .limit(context_size)
                .all()
            )
            all_messages = list(reversed(prev_messages)) + [target_message] + next_messages
            results = [
                {
                    "id": msg.id,
                    "session_id": msg.session_id,
                    "role": msg.role,
                    "content": msg.content,
                    "created_at": self._format_time_east8(msg.created_at),
                    "is_target": msg.id == message_id,
                }
                for msg in all_messages
            ]
            return {
                "query": query,
                "results": results,
                "mode": "message_id",
                "_cache_key": f"search_chat:msg:{message_id}",
            }

        # Mode 3: Keyword search using pgroonga (with pagination)
        if query:
            base_where = "WHERE content &@~ :query AND role IN ('user', 'assistant')"
            base_params: dict[str, Any] = {"query": query}
            if session_id is not None:
                base_where += " AND session_id = :session_id"
                base_params["session_id"] = session_id

            count_sql = text(
                "SELECT COUNT(*) FROM messages {w}".format(w=base_where)
            )
            total = self.db.execute(count_sql, base_params).scalar() or 0

            search_params = {**base_params, "limit": 10, "offset": offset}
            pgroonga_sql = text(
                """
    SELECT id, session_id, role, content, created_at
    FROM messages
    {w}
    ORDER BY pgroonga_score(tableoid, ctid) DESC
    LIMIT :limit OFFSET :offset
""".format(w=base_where)
            )
            rows = self.db.execute(pgroonga_sql, search_params).all()
            results = [
                {
                    "id": row.id,
                    "session_id": row.session_id,
                    "role": row.role,
                    "content": row.content,
                    "created_at": self._format_time_east8(row.created_at),
                }
                for row in rows
            ]
            return {
                "query": query,
                "total": total,
                "offset": offset,
                "limit": 10,
                "results": results,
                "mode": "keyword",
                "_cache_key": f"search_chat:kw:{query}:{offset}",
            }

        return {
            "query": query,
            "results": [],
            "mode": "unknown",
        }

    def search_theater(self, payload: dict[str, Any]) -> dict[str, Any]:
        query = str(payload.get("query", "") or "").strip()
        try:
            limit = max(1, int(payload.get("limit", 5)))
        except Exception:
            limit = 5
        if not query:
            return {"query": query, "results": []}

        # Use pgroonga for full-text search on summary
        pgroonga_sql = text(
            """
    SELECT title, ai_partner, summary, story_timespan
    FROM theater_stories
    WHERE summary IS NOT NULL AND summary &@~ :query
    ORDER BY pgroonga_score(tableoid, ctid) DESC
    LIMIT :limit
"""
        )
        rows = self.db.execute(pgroonga_sql, {"query": query, "limit": limit}).all()
        results = [
            {
                "title": row.title,
                "ai_partner": row.ai_partner,
                "summary": row.summary,
                "story_timespan": row.story_timespan,
            }
            for row in rows
        ]
        return {"query": query, "results": results}

    def fast_recall(
        self, query: str, limit: int = 5, current_mood_tag: str | None = None
    ) -> list[dict[str, Any]]:
        """Dual-path recall: vector top 20 + pgroonga top 20, then rerank and decay-score."""
        CANDIDATE_POOL_SIZE = 20
        rerank_top_n = max(limit, 1)
        query_vector = self.embedding_service.get_embedding(query)
        if query_vector is None:
            return []

        # Vector search top 20
        vector_sql = text(
            """
    SELECT id, content, tags, source, klass, importance, manual_boost, hits,
           halflife_days, last_access_ts, created_at, disclosure
    FROM memories
    WHERE embedding IS NOT NULL
      AND deleted_at IS NULL
      AND is_pending = FALSE
      AND 1 - (embedding <=> :query_embedding) >= :min_similarity
    ORDER BY embedding <=> :query_embedding
    LIMIT :limit
"""
        )
        vector_rows = self.db.execute(
            vector_sql,
            {
                "query_embedding": str(query_vector),
                "limit": CANDIDATE_POOL_SIZE,
                "min_similarity": 0.35,
            },
        ).all()

        # Pgroonga full-text search top 20
        pgroonga_sql = text(
            """
    SELECT id, content, tags, source, klass, importance, manual_boost, hits,
           halflife_days, last_access_ts, created_at, disclosure
    FROM memories
    WHERE deleted_at IS NULL AND is_pending = FALSE AND search_text &@~ :query
    ORDER BY pgroonga_score(tableoid, ctid) DESC
    LIMIT 20
"""
        )
        pgroonga_rows = self.db.execute(pgroonga_sql, {"query": query}).all()

        # Deduplicate by memory id (embedding + pgroonga)
        seen_ids = set()
        candidate_rows = []
        for row in vector_rows:
            if row.id not in seen_ids:
                seen_ids.add(row.id)
                candidate_rows.append(row)
        for row in pgroonga_rows:
            if row.id not in seen_ids:
                seen_ids.add(row.id)
                candidate_rows.append(row)

        # Rerank to get top 5
        if len(candidate_rows) <= 5:
            primary_rows = list(candidate_rows)
        else:
            documents = [row.content or "" for row in candidate_rows]
            rerank_results = self.embedding_service.rerank(
                query, documents, top_n=rerank_top_n
            )
            if not rerank_results:
                primary_rows = list(candidate_rows[:5])
            else:
                primary_rows = []
                seen_indices = set()
                for item in rerank_results:
                    if not isinstance(item, dict):
                        continue
                    index = item.get("index")
                    if not isinstance(index, int):
                        continue
                    if index < 0 or index >= len(candidate_rows):
                        continue
                    if index in seen_indices:
                        continue
                    seen_indices.add(index)
                    primary_rows.append(candidate_rows[index])
                if not primary_rows:
                    primary_rows = list(candidate_rows[:5])
                else:
                    primary_rows = primary_rows[:rerank_top_n]

        # Always apply decay score weighting after reranking
        try:
            scored_rows: list[tuple[float, Any]] = []
            now_utc = datetime.now(TZ_EAST8)
            mood = (current_mood_tag or "").strip().lower()
            is_negative_mood = mood in NEGATIVE_MOOD_TAGS
            _, _mood_weights = _load_emotion_config(self.db)

            for row in primary_rows:
                created_at = row.created_at
                if created_at is None:
                    age_days = 0.0
                else:
                    if created_at.tzinfo is None:
                        created_utc = created_at.replace(tzinfo=TZ_EAST8)
                    else:
                        created_utc = created_at.astimezone(timezone.utc)
                    last_access_ts = row.last_access_ts
                    if last_access_ts is not None:
                        if last_access_ts.tzinfo is None:
                            last_access_utc = last_access_ts.replace(
                                tzinfo=timezone.utc
                            )
                        else:
                            last_access_utc = last_access_ts.astimezone(
                                timezone.utc
                            )
                        base_time = last_access_utc
                    else:
                        base_time = created_utc
                    age_days = max(
                        0.0, (now_utc - base_time).total_seconds() / 86400.0
                    )
                base = min(
                    max((row.importance or 0.5) + (row.manual_boost or 0.0), 0.0),
                    1.0,
                )
                halflife = row.halflife_days or 60.0
                boost = 1 + 0.35 * math.log(1 + (row.hits or 0))
                decayed_score = (
                    base * math.exp(-math.log(2) / halflife * age_days) * boost
                )
                # Mood-based klass weight boost
                klass_weights = _mood_weights.get(mood, {})
                if row.klass in klass_weights:
                    decayed_score *= klass_weights[row.klass]
                scored_rows.append((decayed_score, row))
            scored_rows.sort(key=lambda item: item[0], reverse=True)
            primary_rows = [row for _, row in scored_rows]
        except Exception as exc:
            logger.warning("Decay score weighting failed in fast_recall: %s", exc)

        logger.info(
            "[fast_recall] vector=%d, pgroonga=%d, candidates=%d, primary=%d",
            len(vector_rows), len(pgroonga_rows), len(candidate_rows), len(primary_rows),
        )

        # Step 1: primary results (up to 5) — recall_source: "search"
        # Disclosure memories now participate in mixed search via embedding + search_text
        combined: list[tuple[Any, str]] = []
        for row in primary_rows[:5]:
            combined.append((row, "search"))

        # Step 2: tags expansion — supplementary, max 3 — recall_source: "tags"
        TAG_EXPANSION_SLOTS = 3
        if TAG_EXPANSION_SLOTS > 0:
            all_ids = {getattr(r, "id", None) for r, _ in combined}
            collected_tags: set[str] = set()
            for row, _ in combined:
                tags = getattr(row, "tags", None)
                if isinstance(tags, dict):
                    for value in tags.values():
                        if isinstance(value, list):
                            for item in value:
                                item_text = str(item).strip()
                                if item_text:
                                    collected_tags.add(item_text)
            if collected_tags and all_ids:
                try:
                    tag_list = list(collected_tags)
                    exclude_ids = [i for i in all_ids if i is not None]
                    exp_sql = text("""
                        SELECT id, content, tags, source, klass, importance, manual_boost, hits,
                               halflife_days, last_access_ts, created_at, disclosure
                        FROM memories
                        WHERE id != ALL(:exclude_ids)
                          AND deleted_at IS NULL
                          AND is_pending = FALSE
                          AND EXISTS (
                            SELECT 1 FROM jsonb_each(tags) AS t(k, v),
                            LATERAL jsonb_array_elements_text(
                              CASE jsonb_typeof(v) WHEN 'array' THEN v ELSE '[]' END
                            ) AS elem
                            WHERE elem = ANY(:tag_list)
                          )
                        ORDER BY created_at DESC
                        LIMIT :limit
                    """)
                    tag_rows = self.db.execute(exp_sql, {
                        "exclude_ids": exclude_ids,
                        "tag_list": tag_list,
                        "limit": TAG_EXPANSION_SLOTS,
                    }).all()
                    for row in tag_rows:
                        combined.append((row, "tags"))
                except Exception as exc:
                    logger.warning("Tag-based memory expansion failed: %s", exc)

        results = []
        for row, recall_source in combined[:8]:
            # If this memory has a disclosure field and came from search, mark as "disclosure"
            if recall_source == "search" and getattr(row, "disclosure", None):
                recall_source = "disclosure"
            results.append(
                {
                    "id": row.id,
                    "content": row.content,
                    "tags": row.tags,
                    "source": row.source,
                    "recall_source": recall_source,
                    "created_at": row.created_at.replace(tzinfo=TZ_EAST8).strftime("%Y.%m.%d %H:%M"),
                }
            )
        return results

    def related_memory(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Given a memory id, return its source summary and sibling memories.

        Falls back to embedding+tags search if no summary is linked.
        """
        memory_id = payload.get("memory_id")
        if not memory_id:
            return {"error": "需要 memory_id"}

        memory = self.db.get(Memory, memory_id)
        if not memory:
            return {"error": f"记忆 #{memory_id} 不存在"}

        # Get seen_memory_ids from payload (injected by chat_service)
        seen_ids: set[int] = set(payload.get("_seen_memory_ids", []))
        seen_ids.add(memory_id)

        # Find summary via pending_memories
        pending = (
            self.db.query(PendingMemory)
            .filter(PendingMemory.memory_id == memory_id, PendingMemory.summary_id.isnot(None))
            .first()
        )

        if pending and pending.summary_id:
            # ── Has summary: return summary + sibling memories ──
            summary = self.db.get(SessionSummary, pending.summary_id)
            if not summary:
                return self._related_fallback(memory, seen_ids)

            # Find sibling memories from same summary
            siblings = (
                self.db.query(PendingMemory)
                .filter(
                    PendingMemory.summary_id == pending.summary_id,
                    PendingMemory.status == "confirmed",
                    PendingMemory.memory_id.isnot(None),
                    PendingMemory.memory_id.notin_(seen_ids),
                )
                .all()
            )

            sibling_memories = []
            for sib in siblings:
                mem = self.db.get(Memory, sib.memory_id)
                if mem and not mem.deleted_at:
                    sibling_memories.append({
                        "id": mem.id,
                        "content": mem.content,
                    })

            parts = [
                f"[来源摘要] #{summary.id}（消息范围: {summary.msg_id_start} ~ {summary.msg_id_end}）",
                summary.content or "",
            ]
            if sibling_memories:
                parts.append("\n[同期记忆]")
                for m in sibling_memories:
                    parts.append(f"- [#{m['id']}] {m['content']}")

            # Return ids for seen tracking
            result_ids = [m["id"] for m in sibling_memories]
            return {"result": "\n".join(parts), "_related_ids": result_ids}

        # ── No summary: fallback ──
        return self._related_fallback(memory, seen_ids)

    def _related_fallback(self, memory: Memory, seen_ids: set[int]) -> dict[str, Any]:
        """Fallback: use embedding + tags to find related memories."""
        results: list[dict[str, Any]] = []
        exclude_ids = list(seen_ids)

        # Embedding search
        if memory.embedding is not None:
            vector_sql = text("""
                SELECT id, content
                FROM memories
                WHERE embedding IS NOT NULL
                  AND deleted_at IS NULL
                  AND is_pending = FALSE
                  AND id != ALL(:exclude_ids)
                  AND 1 - (embedding <=> :query_embedding) >= 0.4
                ORDER BY embedding <=> :query_embedding
                LIMIT 5
            """)
            rows = self.db.execute(vector_sql, {
                "query_embedding": str(memory.embedding),
                "exclude_ids": exclude_ids,
            }).all()
            for row in rows:
                if row.id not in seen_ids:
                    results.append({"id": row.id, "content": row.content})
                    seen_ids.add(row.id)

        # Tags search
        if memory.tags and isinstance(memory.tags, dict) and len(results) < 3:
            collected_tags: list[str] = []
            for value in memory.tags.values():
                if isinstance(value, list):
                    for item in value:
                        item_text = str(item).strip()
                        if item_text:
                            collected_tags.append(item_text)
            if collected_tags:
                tag_sql = text("""
                    SELECT id, content
                    FROM memories
                    WHERE id != ALL(:exclude_ids)
                      AND deleted_at IS NULL
                      AND is_pending = FALSE
                      AND EXISTS (
                        SELECT 1 FROM jsonb_each(tags) AS t(k, v),
                        LATERAL jsonb_array_elements_text(
                          CASE jsonb_typeof(v) WHEN 'array' THEN v ELSE '[]' END
                        ) AS elem
                        WHERE elem = ANY(:tag_list)
                      )
                    ORDER BY created_at DESC
                    LIMIT :limit
                """)
                tag_rows = self.db.execute(tag_sql, {
                    "exclude_ids": list(seen_ids),
                    "tag_list": collected_tags,
                    "limit": 3 - len(results),
                }).all()
                for row in tag_rows:
                    if row.id not in seen_ids:
                        results.append({"id": row.id, "content": row.content})
                        seen_ids.add(row.id)

        results = results[:3]
        parts = ["[无匹配摘要，以下为语义关联记忆]"]
        for m in results:
            parts.append(f"- [#{m['id']}] {m['content']}")

        if not results:
            parts.append("未找到关联记忆")

        result_ids = [m["id"] for m in results]
        return {"result": "\n".join(parts), "_related_ids": result_ids}
