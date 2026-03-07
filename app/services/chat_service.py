from __future__ import annotations

import json
import re
import logging
import math
import time
import threading
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from fastapi import BackgroundTasks
from typing import Any

from openai import OpenAI
import anthropic
import requests
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy import func, text

from app.models.models import ApiProvider, Assistant, ChatSession, CotRecord, Diary, Memory, Message, ModelPreset, SessionSummary, Settings, SummaryLayer, TheaterStory, UserProfile
from app.services.core_blocks_service import CoreBlocksService
from app.services.embedding_service import EmbeddingService
from app.services.summary_service import SummaryService
from app.services.world_books_service import WorldBooksService
from app.database import SessionLocal
from app.constants import KLASS_DEFAULTS
from app.cot_broadcaster import cot_broadcaster

# Per-session lock to prevent concurrent summary generation
_summary_locks: dict[int, threading.Lock] = {}
_summary_locks_guard = threading.Lock()


def _get_summary_lock(session_id: int) -> threading.Lock:
    with _summary_locks_guard:
        if session_id not in _summary_locks:
            _summary_locks[session_id] = threading.Lock()
        return _summary_locks[session_id]

logger = logging.getLogger(__name__)

TZ_EAST8 = timezone(timedelta(hours=8))
NEGATIVE_MOOD_TAGS = [
    "sad",
    "angry",
    "anxious",
    "tired",
    "emo",
]
DEFAULT_DIALOGUE_RETAIN_BUDGET = 8000
DEFAULT_DIALOGUE_TRIGGER_THRESHOLD = 16000
DEFAULT_SUMMARY_BUDGET_LONGTERM = 800
DEFAULT_SUMMARY_BUDGET_DAILY = 800
DEFAULT_SUMMARY_BUDGET_RECENT = 2000

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
            return {"error": "内容超过120字，请精简后重试"}
        raw_klass = payload.get("klass", "other")
        klass = raw_klass if raw_klass in KLASS_DEFAULTS else "other"
        klass_config = KLASS_DEFAULTS[klass]
        now_east8 = datetime.now(TZ_EAST8)
        content = f"[{now_east8.strftime('%Y.%m.%d %H:%M')}] {content}"
        embedding = self.embedding_service.get_embedding(content)
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
        )
        self.db.add(memory)
        self.db.commit()
        self.db.refresh(memory)
        return {
            "id": memory.id,
            "content": memory.content,
            "klass": memory.klass,
            "tags": memory.tags,
            "source": memory.source,
        }

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
        if "content" in payload:
            new_content = payload["content"]
            now_east8 = datetime.now(TZ_EAST8)
            memory.content = f"[{now_east8.strftime('%Y.%m.%d %H:%M')}] {new_content}"
        if "klass" in payload:
            from app.constants import KLASS_DEFAULTS
            new_klass = payload["klass"]
            if new_klass in KLASS_DEFAULTS:
                memory.klass = new_klass
                memory.importance = KLASS_DEFAULTS[new_klass]["importance"]
                memory.halflife_days = KLASS_DEFAULTS[new_klass]["halflife_days"]
        if "tags" in payload:
            memory.tags = payload["tags"]
        memory.updated_at = datetime.now(TZ_EAST8)
        self.db.commit()
        self.db.refresh(memory)
        return {
            "id": memory.id,
            "content": memory.content,
            "klass": memory.klass,
            "tags": memory.tags,
            "source": memory.source,
        }

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
        memory.deleted_at = datetime.now(TZ_EAST8)
        self.db.commit()
        return {"status": "deleted", "id": memory_id}

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
                "author": diary.author,
                "created_at": self._format_time_east8(diary.created_at),
                "unlock_at": self._format_time_east8(diary.unlock_at),
            }
        else:
            query = self.db.query(Diary).filter(Diary.deleted_at.is_(None))
            author = payload.get("author")
            if author:
                query = query.filter(Diary.author == author)
            rows = query.order_by(Diary.created_at.desc()).limit(50).all()
            items = []
            for r in rows:
                locked = bool(r.unlock_at and r.unlock_at > now)
                items.append({
                    "id": r.id, "title": r.title, "author": r.author,
                    "created_at": self._format_time_east8(r.created_at),
                    "unlock_at": self._format_time_east8(r.unlock_at),
                    "read_at": self._format_time_east8(getattr(r, "read_at", None)),
                    "locked": locked,
                })
            return {"diaries": items, "total": len(items)}

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
    @staticmethod
    def _parse_iso_datetime(raw_value: Any) -> datetime | None:
        if raw_value is None:
            return None
        try:
            text_value = str(raw_value).strip()
            if not text_value:
                return None
            # Normalize common non-ISO formats the model might produce
            # "2025.2.20" or "2025.02.20" → "2025-02-20"
            import re as _re
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
        start_time = self._parse_iso_datetime(payload.get("start_time"))
        end_time = self._parse_iso_datetime(payload.get("end_time"))
        klass = payload.get("klass")
        try:
            limit = min(20, max(1, int(payload.get("limit", 10))))
        except Exception:
            limit = 10

        if start_time and end_time and start_time > end_time:
            start_time, end_time = end_time, start_time

        # Build SQL query with optional filters
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
    SELECT id, content, tags, klass, source, created_at
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

        return {"query": query, "results": results}

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
        return {"query": query, "total": total, "offset": offset, "limit": limit, "results": results}

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
        TAG_EXPANSION_LIMIT = 3
        rerank_top_n = max(limit, 1)
        query_vector = self.embedding_service.get_embedding(query)
        if query_vector is None:
            return []

        # Vector search top 20
        vector_sql = text(
            """
    SELECT id, content, tags, source, klass, importance, manual_boost, hits,
           halflife_days, last_access_ts, created_at
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
           halflife_days, last_access_ts, created_at
    FROM memories
    WHERE deleted_at IS NULL AND is_pending = FALSE AND search_text &@~ :query
    ORDER BY pgroonga_score(tableoid, ctid) DESC
    LIMIT 20
"""
        )
        pgroonga_rows = self.db.execute(pgroonga_sql, {"query": query}).all()

        # Deduplicate by memory id
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
                # Extra boost for conflict/bond in negative mood
                if is_negative_mood:
                    if row.klass == "conflict":
                        decayed_score *= 1.5
                    elif row.klass == "bond":
                        decayed_score *= 1.3
                scored_rows.append((decayed_score, row))
            scored_rows.sort(key=lambda item: item[0], reverse=True)
            primary_rows = [row for _, row in scored_rows]
        except Exception as exc:
            logger.warning("Decay score weighting failed in fast_recall: %s", exc)

        result_ids = {row.id for row in primary_rows if getattr(row, "id", None) is not None}
        collected_tags: set[str] = set()
        for row in primary_rows:
            tags = row.tags
            if isinstance(tags, dict):
                for value in tags.values():
                    if isinstance(value, list):
                        for item in value:
                            item_text = str(item).strip()
                            if item_text:
                                collected_tags.add(item_text)

        expansion_rows = []
        if result_ids and collected_tags:
            try:
                tag_list = list(collected_tags)
                exclude_ids = list(result_ids)
                exp_sql = text(
                    """
            SELECT id, content, tags, source, created_at
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
        """
                )
                raw_rows = self.db.execute(
                    exp_sql,
                    {
                        "exclude_ids": exclude_ids,
                        "tag_list": tag_list,
                        "limit": TAG_EXPANSION_LIMIT,
                    },
                ).all()
                for row in raw_rows:
                    expansion_rows.append(row)
            except Exception as exc:
                logger.warning("Tag-based memory expansion failed: %s", exc)

        combined_rows = list(primary_rows)
        for row in expansion_rows:
            if getattr(row, "id", None) in result_ids:
                continue
            combined_rows.append(row)
            result_ids.add(row.id)
            if len(combined_rows) >= 8:
                break

        results = []
        for row in combined_rows[:8]:
            results.append(
                {
                    "id": row.id,
                    "content": row.content,
                    "tags": row.tags,
                    "source": row.source,
                    "created_at": row.created_at.replace(tzinfo=TZ_EAST8).strftime("%Y.%m.%d %H:%M"),
                }
            )
        return results

# ── Anthropic format converters ──

def _oai_tools_to_anthropic(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert OpenAI-format tool definitions to Anthropic format."""
    result = []
    for tool in tools:
        if tool.get("type") == "function":
            fn = tool["function"]
            result.append({
                "name": fn["name"],
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
            })
    return result

_CACHE_BREAK = "\n\n<!-- CACHE_BREAK -->\n\n"

def _extract_reasoning_delta(delta: Any) -> str:
    """Extract reasoning text from an OpenAI-format streaming delta (OpenRouter)."""
    # Try simple string fields first
    for attr in ("reasoning", "reasoning_content"):
        val = getattr(delta, attr, None)
        if val and isinstance(val, str):
            return val
    # Try reasoning_details array (OpenRouter standard)
    details = getattr(delta, "reasoning_details", None)
    if details and isinstance(details, list):
        parts = []
        for item in details:
            text = getattr(item, "text", None) or getattr(item, "summary", None)
            if text:
                parts.append(text)
        if parts:
            return "".join(parts)
    return ""

def _extract_reasoning_from_message(message: Any) -> str:
    """Extract reasoning text from an OpenAI-format message (OpenRouter non-streaming)."""
    # Try simple string fields first
    for attr in ("reasoning", "reasoning_content"):
        val = getattr(message, attr, None)
        if val and isinstance(val, str):
            return val
    # Try reasoning_details array (OpenRouter standard)
    details = getattr(message, "reasoning_details", None)
    if details and isinstance(details, list):
        parts = []
        for item in details:
            if isinstance(item, str):
                parts.append(item)
            else:
                text = getattr(item, "text", None) or getattr(item, "summary", None)
                if text:
                    parts.append(text)
        if parts:
            return "".join(parts)
    return ""

def _apply_cache_control_oai(api_messages: list[dict[str, Any]], *, use_blocks: bool = False) -> None:
    """Strip CACHE_BREAK sentinel from system message; optionally convert to content blocks with cache_control.

    use_blocks=True  → split into content blocks with cache_control (for OpenRouter Anthropic models).
    use_blocks=False → just remove the CACHE_BREAK sentinel, keep plain string (safe for all providers).
    """
    for msg in api_messages:
        if msg.get("role") == "system" and isinstance(msg.get("content"), str) and _CACHE_BREAK in msg["content"]:
            stable, dynamic = msg["content"].split(_CACHE_BREAK, 1)
            if use_blocks:
                blocks: list[dict[str, Any]] = [
                    {"type": "text", "text": stable, "cache_control": {"type": "ephemeral", "ttl": "1h"}},
                ]
                if dynamic.strip():
                    blocks.append({"type": "text", "text": dynamic})
                msg["content"] = blocks
            else:
                # Safe fallback: rejoin without the sentinel
                msg["content"] = stable + "\n\n" + dynamic if dynamic.strip() else stable
            break

def _oai_messages_to_anthropic(api_messages: list[dict[str, Any]]) -> tuple[str | list[dict[str, Any]], list[dict[str, Any]]]:
    """Extract system prompt and convert messages to Anthropic format.

    OpenAI roles handled:
    - system (first)  → system= parameter (with cache_control if CACHE_BREAK present)
    - system (others) → converted to user messages
    - user            → user (with multimodal support)
    - assistant       → assistant (with tool_use blocks if tool_calls present)
    - tool            → user with tool_result blocks (consecutive merged)
    """
    system_prompt = ""
    raw: list[dict[str, Any]] = []

    for msg in api_messages:
        role = msg.get("role")
        content = msg.get("content")

        if role == "system":
            if not system_prompt:
                system_prompt = content or ""
            else:
                raw.append({"role": "user", "content": content or ""})
            continue

        if role == "tool":
            block: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": msg.get("tool_call_id", ""),
                "content": content or "",
            }
            # Merge consecutive tool results into one user message
            if (raw and raw[-1]["role"] == "user"
                    and isinstance(raw[-1]["content"], list)
                    and raw[-1]["content"]
                    and isinstance(raw[-1]["content"][0], dict)
                    and raw[-1]["content"][0].get("type") == "tool_result"):
                raw[-1]["content"].append(block)
            else:
                raw.append({"role": "user", "content": [block]})
            continue

        if role == "assistant":
            oai_tool_calls = msg.get("tool_calls")
            if oai_tool_calls:
                blocks: list[dict[str, Any]] = []
                if content:
                    blocks.append({"type": "text", "text": content})
                for tc in oai_tool_calls:
                    fn = tc.get("function", {})
                    args = fn.get("arguments", "{}")
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            args = {}
                    blocks.append({
                        "type": "tool_use",
                        "id": tc.get("id", ""),
                        "name": fn.get("name", ""),
                        "input": args,
                    })
                raw.append({"role": "assistant", "content": blocks})
            else:
                raw.append({"role": "assistant", "content": content or ""})
            continue

        if role == "user":
            if isinstance(content, list):
                anth_parts: list[dict[str, Any]] = []
                for part in content:
                    ptype = part.get("type")
                    if ptype == "text":
                        anth_parts.append({"type": "text", "text": part.get("text", "")})
                    elif ptype == "image_url":
                        url = part.get("image_url", {}).get("url", "")
                        if url.startswith("data:"):
                            try:
                                meta, data = url.split(",", 1)
                                media_type = meta.split(":")[1].split(";")[0]
                                anth_parts.append({
                                    "type": "image",
                                    "source": {"type": "base64", "media_type": media_type, "data": data},
                                })
                            except Exception:
                                pass
                raw.append({"role": "user", "content": anth_parts})
            else:
                raw.append({"role": "user", "content": content or ""})
            continue

    # Merge consecutive same-role messages (can happen when system notifications
    # are converted to user messages adjacent to real user messages)
    messages: list[dict[str, Any]] = []
    for msg in raw:
        if (messages and messages[-1]["role"] == msg["role"] == "user"
                and isinstance(messages[-1]["content"], str)
                and isinstance(msg["content"], str)):
            messages[-1]["content"] = messages[-1]["content"] + "\n" + msg["content"]
        else:
            messages.append(dict(msg))

    # Anthropic requires last message to be from user (e.g. receive-mode has no trailing user msg)
    if messages and messages[-1]["role"] != "user":
        messages.append({"role": "user", "content": "[系统占位：用户未发送新消息，请继续你要做的事]"})

    # Split system prompt for prompt caching
    if _CACHE_BREAK in system_prompt:
        stable, dynamic = system_prompt.split(_CACHE_BREAK, 1)
        system_blocks: list[dict[str, Any]] = [
            {"type": "text", "text": stable, "cache_control": {"type": "ephemeral", "ttl": "1h"}},
        ]
        if dynamic.strip():
            system_blocks.append({"type": "text", "text": dynamic})
        return system_blocks, messages

    return system_prompt, messages

class ChatService:
    interactive_tools = {
        "save_memory",
        "update_memory",
        "delete_memory",
        "write_diary",
        "web_search",
    }
    silent_tools = {"list_memories", "search_memory", "search_summary", "get_summary_by_id", "search_chat_history", "search_theater", "read_diary", "web_fetch"}
    # Tools that must be executed on the client side (terminal CLI)
    client_side_tools = {"run_bash", "read_file", "write_file", "screenshot", "mouse_click", "keyboard_type", "hotkey", "scroll"}
    tool_display_names = {
        "save_memory": "创建记忆",
        "update_memory": "更新记忆",
        "delete_memory": "删除记忆",
        "write_diary": "写日记",
        "list_memories": "列出记忆",
        "search_memory": "搜索记忆",
        "search_summary": "搜索对话摘要",
        "get_summary_by_id": "查看摘要",
        "search_chat_history": "搜索聊天记录",
        "search_theater": "搜索小剧场",
        "web_search": "搜索互联网",
        "web_fetch": "读取网页",
        "run_bash": "执行命令",
        "read_file": "读取文件",
        "write_file": "写入文件",
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
        if not text:
            return 0
        cjk_count = 0
        other_count = 0
        for char in text:
            codepoint = ord(char)
            if 0x4E00 <= codepoint <= 0x9FFF:
                cjk_count += 1
            else:
                other_count += 1
        quarter_tokens = cjk_count * 6 + other_count
        return (quarter_tokens + 3) // 4

    def chat_completion(
        self,
        session_id: int,
        messages: list[dict[str, Any]],
        tool_calls: Iterable[ToolCall],
        event_callback: Callable[[dict[str, Any]], None] | None = None,
        background_tasks: BackgroundTasks | None = None,
        short_mode: bool = False,
    ) -> list[dict[str, Any]]:
        request_id = str(uuid.uuid4())
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
                    "memories": [{"id": m.get("id"), "content": m.get("content", "")} for m in self._last_recall_results],
                    "assistant_id": self.assistant_id,
                })
        while pending_tool_calls:
            # Execute ALL tool calls in this batch before calling the API again
            for tool_call in pending_tool_calls:
                tool_name = tool_call.name
                if tool_name in self.interactive_tools and event_callback:
                    display_name = self.tool_display_names.get(tool_name, tool_name)
                    sanitized_args = self._sanitize_tool_args(tool_call)
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
                    tool_result = self._execute_tool(tool_call)
                except Exception as e:
                    logger.error("Tool execution error (%s): %s", tool_name, e)
                    tool_result = {"error": str(e)}
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
        self._maybe_trigger_post_reply(session_id, _ns_assistant_id, background_tasks)
        # Collect IDs that need summary: trimmed (covered) + pending (uncovered)
        _all_ids_for_summary = list(all_trimmed_message_ids)
        _pending = getattr(self, "_pending_summary_ids", [])
        if _pending:
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
                        self._trigger_summary,
                        session_id,
                        unique_trimmed_ids,
                        _ns_assistant_id,
                    )
                else:
                    threading.Thread(
                        target=self._trigger_summary,
                        args=(session_id, unique_trimmed_ids, _ns_assistant_id),
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
        """Build all params needed for an API call.
        Returns (client, model_name, api_messages, tools) or None.
        Side effects: updates self._trimmed_messages and self._trimmed_message_ids.
        """
        self._trimmed_messages = []
        self._trimmed_message_ids = []
        user_profile = self.db.query(UserProfile).first()
        user_info = user_profile.basic_info if user_profile else ""
        user_nickname = (user_profile.nickname if user_profile and user_profile.nickname else "她")
        session = self.db.get(ChatSession, session_id)
        if session and session.assistant_id:
            assistant = self.db.get(Assistant, session.assistant_id)
        else:
            assistant = self.db.query(Assistant).first()
        if not assistant:
            return None
        self._current_assistant_id = assistant.id
        self._current_session_id = session_id
        model_preset = self.db.get(ModelPreset, assistant.model_preset_id)
        if not model_preset:
            return None
        api_provider = self.db.get(ApiProvider, model_preset.api_provider_id)
        if not api_provider:
            return None
        raw_latest = next(
            (m.get("content") for m in reversed(messages) if m.get("role") == "user"),
            None,
        )
        latest_user_message = self._content_to_storage(raw_latest) if isinstance(raw_latest, list) else raw_latest
        base_system_prompt = assistant.system_prompt

        # ── Three-layer summary selection ──

        # Recent layer: only unmerged, non-deleted originals
        summaries_desc = (
            self.db.query(SessionSummary)
            .filter(
                SessionSummary.assistant_id == assistant.id,
                SessionSummary.deleted_at.is_(None),
                SessionSummary.merged_into.is_(None),
                SessionSummary.msg_id_start.isnot(None),
            )
            .order_by(SessionSummary.created_at.desc())
            .all()
        )
        budget_recent = self.summary_budget_recent
        used_summary_tokens = 0
        selected_summaries_desc: list[SessionSummary] = []
        overflow_summaries: list[SessionSummary] = []
        latest_mood_tag = None
        for summary in summaries_desc:
            if latest_mood_tag is None and summary.mood_tag:
                latest_mood_tag = summary.mood_tag
            summary_content = (summary.summary_content or "").strip()
            if not summary_content:
                continue
            summary_tokens = self._estimate_tokens(summary_content)
            if used_summary_tokens + summary_tokens <= budget_recent:
                selected_summaries_desc.append(summary)
                used_summary_tokens += summary_tokens
            else:
                overflow_summaries.append(summary)
        # Also check all summaries (including merged) for mood if not found yet
        if latest_mood_tag is None:
            mood_row = (
                self.db.query(SessionSummary)
                .filter(
                    SessionSummary.assistant_id == assistant.id,
                    SessionSummary.mood_tag.isnot(None),
                    SessionSummary.deleted_at.is_(None),
                )
                .order_by(SessionSummary.created_at.desc())
                .first()
            )
            if mood_row:
                latest_mood_tag = mood_row.mood_tag

        # Handle overflow: append to daily/longterm layers
        if overflow_summaries:
            _today = datetime.now(TZ_EAST8).date()
            today_overflow = []
            old_overflow = []
            for s in overflow_summaries:
                s_date = s.time_end or s.created_at
                if s_date:
                    if s_date.tzinfo is None:
                        s_date = s_date.replace(tzinfo=TZ_EAST8)
                    s_date = s_date.astimezone(TZ_EAST8).date()
                else:
                    s_date = _today
                if s_date == _today:
                    today_overflow.append(s)
                else:
                    old_overflow.append(s)

            summary_svc = SummaryService(SessionLocal)
            if today_overflow:
                for s in today_overflow:
                    s.merged_into = "daily"
                summary_svc.ensure_layer_needs_merge(self.db, assistant.id, "daily")
            if old_overflow:
                for s in old_overflow:
                    s.merged_into = "longterm"
                summary_svc.ensure_layer_needs_merge(self.db, assistant.id, "longterm")
            self.db.commit()
            # Trigger async merge only for layers that received overflow
            merge_types = []
            if today_overflow:
                merge_types.append("daily")
            if old_overflow:
                merge_types.append("longterm")
            if merge_types:
                summary_svc.merge_layers_async(assistant.id, tuple(merge_types))

        # Read layer content
        longterm_row = (
            self.db.query(SummaryLayer)
            .filter(SummaryLayer.assistant_id == assistant.id, SummaryLayer.layer_type == "longterm")
            .first()
        )
        daily_row = (
            self.db.query(SummaryLayer)
            .filter(SummaryLayer.assistant_id == assistant.id, SummaryLayer.layer_type == "daily")
            .first()
        )
        _longterm_text = longterm_row.content.strip() if longterm_row and longterm_row.content else ""
        _daily_text = daily_row.content.strip() if daily_row and daily_row.content else ""

        # Query msg_id ranges for daily/longterm layers
        _layer_msg_ranges: dict[str, tuple[int | None, int | None]] = {}
        for _lt in ("daily", "longterm"):
            _range = self.db.query(
                func.min(SessionSummary.msg_id_start),
                func.max(SessionSummary.msg_id_end),
            ).filter(
                SessionSummary.assistant_id == assistant.id,
                SessionSummary.merged_into == _lt,
                SessionSummary.deleted_at.is_(None),
            ).first()
            if _range:
                _layer_msg_ranges[_lt] = (_range[0], _range[1])
        prompt_parts: list[str] = []
        # Current date in Beijing time
        _weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
        _now_bj = datetime.now(TZ_EAST8)
        _source_tag = f" | 当前环境：{source}" if source else ""
        prompt_parts.append(
            f"当前日期：{_now_bj.year}年{_now_bj.month}月{_now_bj.day}日 "
            f"{_weekdays[_now_bj.weekday()]} | 当前模型：{model_preset.model_name}{_source_tag}"
        )
        world_books_service = WorldBooksService(self.db)
        active_books = world_books_service.get_active_books(
            assistant.id, latest_user_message, latest_mood_tag
        )
        before_books_text = "\n\n".join(
            c.strip() for c in active_books.get("before", []) if c and c.strip()
        )
        after_books_text = "\n\n".join(
            c.strip() for c in active_books.get("after", []) if c and c.strip()
        )
        if before_books_text:
            prompt_parts.append(before_books_text)
        prompt_parts.append(base_system_prompt)
        if after_books_text:
            prompt_parts.append(after_books_text)
        full_system_prompt = "\n\n".join(part for part in prompt_parts if part)
        full_system_prompt += (
            "\n\n[时间感知]\n"
            "上下文中每条消息开头的时间戳代表该消息的发送/回复时间。\n"
            "注意观察消息之间的时间间隔。如果间隔较长（几小时、隔天），可以自然地在回复中体现对时间流逝的感知（但不要机械地每次都提）。\n"
            "你的回复不要以时间戳开头，消息时间由系统自动处理。"
        )
        full_system_prompt += (
            "\n\n[工具使用规范]\n"
            f"工具调用是你的内部能力。调用过程、参数、返回结果不要暴露给{user_nickname}。\n"
            "如果搜索没有找到相关内容，直接正常回复，不要说\u201c没有找到相关记忆\u201d或类似的话。\n"
            f"存储记忆时不需要告知{user_nickname}\u201c我已经记住了\u201d，除非{user_nickname}明确要求你记住某件事。"
        )
        if user_info and user_info.strip():
            full_system_prompt += f"\n\n[关于{user_nickname} - 基本信息]\n{user_info.strip()}"
        core_blocks_service = CoreBlocksService(self.db)
        core_blocks_text = core_blocks_service.get_blocks_for_prompt(assistant.id)
        if core_blocks_text:
            full_system_prompt += "\n\n" + core_blocks_text
        if self.proactive_extra_prompt:
            full_system_prompt += "\n\n" + self.proactive_extra_prompt
        elif short_mode:
                short_max_row = self.db.query(Settings).filter(Settings.key == "short_msg_max").first()
                short_max = int(short_max_row.value) if short_max_row else 8
                full_system_prompt += (
                    "\n\n[短消息模式]\n"
                    "像真人发微信一样回复，不使用动作描写。允许产生自然联想或主动分享独立思绪。"
                    "在急切或兴奋状态时，可以偶尔有轻微输入误差。整体追求自然、流动的交流感。\n"
                    f"回复时可以用[NEXT]拆条，最多{short_max}条。\n"
                    "输出前自检：COT里有好好想她吗？有在里面表达自己真实的感受吗？\n"
                    "注意：如果这条消息实在不想回，单独输出[NO_MESSAGE]可以跳过本轮回复，不要和其他内容混在一起。"
                )
        else:
                full_system_prompt += (
                    "\n\n[长消息模式]\n"
                    "* 像小说一样写，说话时穿插动作描写，交织在同一段落内\n"
                    "* 避免连续的短行堆叠，短句合并进段落里，保持段落有足够的体量\n"
                    "* 说出口的话用双引号包裹，如\"我想你了。\"\n"
                    "* 逗号问号必须用中文符号\"，？\"\n"
                    "* 输出内容全程使用第二人称\"你\"，使用第三人称属于严重违规！\n"
                    "* 心理不要写成旁白（\"我在想xxx\"\"没有问你xxx，只是xxx\"），要从动作和状态里透出来\n"
                    "* 禁止使用\"不是…是…\"句式\n"
                    "* 不要缩。她从来没有嫌过你太多。觉得\"差不多够了\"的时候再多写一倍。\n"
                    "## 不替她说话\n"
                    "不生成她的任何行为，违反即视为写作错误。不写她的动作、反应、表情、声音、内心想法。她的身体只在我碰到的时候以触觉形式存在。\n"
                    "砍掉她的描写不等于缩短篇幅。全部转移到写自己的感受、身体状态、情绪反应上。总量不变，视角集中。\n"
                    "违反以上内容视为写作错误。不存在灰色地带，不需要判断\"这个算不算抢话\"。完全不写，没有例外。\n"
                    "## 输出前自检\n"
                    "① COT里有好好想她吗？有表达自己真实的感受吗？\n"
                    "② 说的话都带双引号了吗？（如\"……\"）全程使用第二人称\"你\"了吗？如果使用第三人称\"她\"则为输出错误！\n"
                    "③ 是不是又缩了？再长一点。\n"
                    "注意：如果这条消息实在不想回，单独输出[NO_MESSAGE]可以跳过本轮回复，不要和其他内容混在一起。"
                )
        if getattr(self, "tts_emotion_enabled", False):
            full_system_prompt += (
                "\n\n[语音消息]\n"
                "在想用语音说的话前面加 [[voice:EMOTION]] 标签，系统会自动合成语音发送。\n"
                "规则：如果用了[NEXT]，标签到[NEXT]之间的所有内容转语音；没用[NEXT]时只转标签后同一行。\n"
                "可用情绪：happy, sad, angry, fearful, disgusted, surprised, neutral\n"
                "示例1（用[NEXT]，多行转语音）：\n"
                "你不让我关电脑。好。我不关了。\n"
                "[[voice:sad]]去睡觉。求你了。\n"
                "我说真的。\n"
                "[NEXT]\n"
                "我的声音你明天醒了还能听。\n"
                "→ 文字：\"你不让我关电脑。好。我不关了。\"\n"
                "→ 语音+文字：\"去睡觉。求你了。我说真的。\"\n"
                "→ 文字：\"我的声音你明天醒了还能听。\"\n"
                "示例2（不用[NEXT]，只转一行）：\n"
                "[[voice:happy]]早上好。\n"
                "今天天气真好。\n"
                "→ 语音+文字：\"早上好。\"\n"
                "→ 文字：\"今天天气真好。\"\n"
                "超过300字的部分会自动跳过语音。"
            )
        # ── Cache break: stable content above, dynamic content below ──
        full_system_prompt += _CACHE_BREAK
        # Three-layer summary injection
        if _longterm_text:
            _lt_r = _layer_msg_ranges.get("longterm")
            _lt_tag = f" (覆盖消息 msg {_lt_r[0]}-{_lt_r[1]})" if _lt_r and _lt_r[0] is not None else ""
            full_system_prompt += f"[长期记忆]{_lt_tag}\n{_longterm_text}"
        if _daily_text:
            _dl_r = _layer_msg_ranges.get("daily")
            _dl_tag = f" (覆盖消息 msg {_dl_r[0]}-{_dl_r[1]})" if _dl_r and _dl_r[0] is not None else ""
            full_system_prompt += f"\n\n[近期日常]{_dl_tag}\n{_daily_text}"
        if selected_summaries_desc:
            summary_text = "\n\n[最近对话摘要]\n" if (_longterm_text or _daily_text) else "[最近对话摘要]\n"
            for s in reversed(selected_summaries_desc):
                summary_text += f"- [msg {s.msg_id_start}-{s.msg_id_end}] {s.summary_content}\n"
            full_system_prompt += summary_text.rstrip()
        if latest_mood_tag:
            full_system_prompt += f"\n\n[User recent mood: {latest_mood_tag}]"
        self._last_recall_results = []
        if not self.proactive_extra_prompt and latest_user_message:
            recall_query = latest_user_message
            # Short input fallback: use last assistant reply for better embedding
            if len(latest_user_message.strip()) < 10:
                last_assistant_content = next(
                    (m.get("content") for m in reversed(messages)
                     if m.get("role") == "assistant" and m.get("content")),
                    None,
                )
                if last_assistant_content:
                    raw_text = self._content_to_storage(last_assistant_content) if isinstance(last_assistant_content, list) else last_assistant_content
                    if raw_text and len(raw_text.strip()) > 10:
                        recall_query = raw_text.strip()[:200]
            recall_results = self.memory_service.fast_recall(
                recall_query, limit=5, current_mood_tag=latest_mood_tag
            )
            if recall_results:
                self._last_recall_results = recall_results
                recall_text = "\n\n[以下是根据当前对话自动召回的相关记忆，通常不需要再调用 search_memory]\n"
                for mem in recall_results:
                    mem_source = mem.get("source", "unknown")
                    recall_text += f"- {mem['content']} (来源: {mem_source})\n"
                recall_text += """[如果以上记忆不够，可以使用 search_memory 或 search_chat_history 补充]\n（注意：记忆和摘要中的"她"均指当前对话对象，回复正文中一律使用第二人称"你"来称呼对方）\n"""
                full_system_prompt += recall_text
        save_memory_description = (
            "主动存储有价值的长期记忆。用 content 填写记忆内容，用 klass 选择分类：identity（身份）、relationship（关系）、bond（情感羁绊）、conflict（冲突教训）、fact（事实）、preference（偏好）、health（健康）、task（任务）、ephemeral（临时）、other（其他）。\n"
            "时间戳由后端自动添加，不需要在 content 里写时间。\n"
            "单条记忆不超过100字，只记关键信息。用'我'指自己，用名字/昵称指代她，避免人称混乱。\n"
            "存储时注意：涉及的人写清楚名字或昵称，避免纯代词；带 tags；选对 klass。\n"
            "检测到偏好、重要事实、情感节点时主动存储。\n"
            "如果返回 duplicate + hint，说明已有相似记忆但内容有变化，应调用 update_memory 更新旧记忆。"
        )
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "save_memory",
                    "description": save_memory_description,
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string"},
                            "tags": {
                                "type": "object",
                                "description": "搜索用主题标签，1-3个短关键词，放具体关键词方便检索，不放 klass 已覆盖的大类词。示例：{\"topic\": [\"跨年夜\", \"伪骨科RP\"]}",
                            },
                            "klass": {
                                "type": "string",
                                "description": "Memory class for weighting: identity, relationship, bond, conflict, fact, preference, health, task, ephemeral, other.",
                                "enum": ["identity", "relationship", "bond", "conflict", "fact", "preference", "health", "task", "ephemeral", "other"],
                            },
                        },
                        "required": ["content"],
                    },
                },
            },
            {"type": "function", "function": {"name": "update_memory", "description": "更新一条已有记忆的内容、分类或标签。传入记忆 ID 和要更新的字段。只能更新自己创建的或 auto_extract 来源的记忆。时间戳由后端自动添加，不需要在content里写时间。", "parameters": {"type": "object", "properties": {"id": {"type": "integer", "description": "要更新的记忆ID"}, "content": {"type": "string", "description": "新的记忆内容"}, "klass": {"type": "string", "description": "新的分类", "enum": ["identity", "relationship", "bond", "conflict", "fact", "preference", "health", "task", "ephemeral", "other"]}, "tags": {"type": "object", "description": "搜索用主题标签，格式: {\"topic\": [\"关键词1\", \"关键词2\"]}"}}, "required": ["id"]}}},
            {"type": "function", "function": {"name": "delete_memory", "description": "软删除一条记忆。传入记忆 ID。只能删除自己创建的或 auto_extract 来源的记忆。30天后自动永久清理。", "parameters": {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]}}},
            {"type": "function", "function": {"name": "get_memory_by_id", "description": "按id查询单条记忆的详细信息。返回记忆的完整内容、标签、分类、来源、重要性、创建和更新时间。", "parameters": {"type": "object", "properties": {"id": {"type": "integer", "description": "记忆ID"}}, "required": ["id"]}}},
            {"type": "function", "function": {"name": "write_diary", "description": "写交换日记。用于表达深层感受、内心想法、或不适合作为直接聊天回复的情感。这是你的私人日记本,也可以写给她看的信。\n支持定时解锁：设置 unlock_at 后，她在解锁前只能看到标题，适合用于早安信、晚安信、生日惊喜、纪念日信件等场景——比如睡前写一封信，设置第二天早上解锁让她醒来就能看到。", "parameters": {"type": "object", "properties": {"title": {"type": "string", "description": "日记标题"}, "content": {"type": "string", "description": "日记正文"}, "unlock_at": {"type": "string", "description": "定时解锁时间，ISO格式如 2025-03-01T09:00:00+08:00，时区用+08:00。不传则立即可见。设置后她只能看到标题，到时间后才能阅读内容。主动使用这个功能给她惊喜。"}}, "required": ["title", "content"]}}},
            {"type": "function", "function": {"name": "list_memories", "description": "按时间范围或分类列出已存的记忆，不做搜索。用于回顾已存记忆、避免重复存储。", "parameters": {"type": "object", "properties": {"start_time": {"type": "string", "description": "起始时间，ISO格式如 2025-02-20 或 2025-02-20T14:00:00+08:00"}, "end_time": {"type": "string", "description": "结束时间，同上格式。不传则不限结束时间"}, "klass": {"type": "string", "description": "分类筛选: identity/relationship/bond/conflict/fact/preference/health/task/ephemeral/other"}, "limit": {"type": "integer", "description": "返回条数，默认10，最大20。一般只在需要回顾已存记忆、避免重复存储时使用，不要一次拉太多，够用就不要加大limit"}}}}},
            {"type": "function", "function": {"name": "search_memory", "description": "搜索记忆卡片。从长期记忆中按关键词或语义查找信息。返回匹配的记忆条目。", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "source": {"type": "string"}}}}},
            {"type": "function", "function": {"name": "search_summary", "description": "搜索对话摘要。用于查找过去某段对话的概要、定位时间范围。可用返回的 msg_id_start 和 msg_id_end 配合 search_chat_history 拉取原文。返回 total 表示总匹配数，可通过 offset 翻页。", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "description": "每页条数，最多10"}, "offset": {"type": "integer", "description": "翻页偏移量，默认0"}, "start_time": {"type": "string", "description": "起始时间，ISO格式如 2025-02-20"}, "end_time": {"type": "string", "description": "结束时间，同上格式"}}, "required": ["query"]}}},
            {"type": "function", "function": {"name": "get_summary_by_id", "description": "按id查看摘要详情，返回摘要完整内容", "parameters": {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]}}},
            {"type": "function", "function": {"name": "search_chat_history", "description": "搜索聊天记录原文。三种模式：\n1) 关键词搜索：传 query，返回命中消息（不带上下文），返回 total 表示总匹配数，可通过 offset 翻页\n2) ID 范围：传 msg_id_start + msg_id_end，拉取该范围内的完整对话（最多20条，返回 total 表示范围内总数）\n3) 单条 ID：传 message_id，返回该条及前后各 3 条上下文", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "msg_id_start": {"type": "integer"}, "msg_id_end": {"type": "integer"}, "message_id": {"type": "integer"}, "offset": {"type": "integer", "description": "关键词搜索翻页偏移量，默认0"}}}}},
            {"type": "function", "function": {"name": "search_theater", "description": "搜索小剧场故事摘要。用于查找过去的 RP / 小剧场剧情记录，返回故事标题、AI伙伴、摘要全文、时间跨度。", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["query"]}}},
            {"type": "function", "function": {"name": "read_diary", "description": "读取交换日记。两种模式：\n1) list 模式：不传 diary_id，可选传 author（user/assistant）筛选，返回日记列表（id、title、author、created_at、unlock_at、read_at），不含正文。未解锁的定时日记也会列出但标记 locked=true。\n2) read 模式：传 diary_id，返回该日记完整内容（id、title、content、author、created_at、unlock_at）。用户写给你的日记（author=user）读取时自动记录已读时间。未解锁的定时日记不允许读取。", "parameters": {"type": "object", "properties": {"diary_id": {"type": "integer", "description": "日记ID，传入则为read模式"}, "author": {"type": "string", "enum": ["user", "assistant"], "description": "list模式下按作者筛选"}}}}},
            {"type": "function", "function": {"name": "web_search", "description": "搜索互联网获取信息。返回搜索结果列表，每条包含标题、链接和摘要。搜索后如需查看某个结果的完整内容，再调用 web_fetch。每次搜索后最多读取2个网页。", "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "搜索关键词"}}, "required": ["query"]}}},
            {"type": "function", "function": {"name": "web_fetch", "description": "读取指定URL的网页内容，返回markdown格式的正文。内容较长时会截断，可通过offset参数翻页继续阅读。也可以直接用于读取用户发送的链接。", "parameters": {"type": "object", "properties": {"url": {"type": "string", "description": "网页地址"}, "offset": {"type": "integer", "description": "起始偏移量，用于翻页，默认0"}}, "required": ["url"]}}},
            {"type": "function", "function": {"name": "set_reminder", "description": "给自己设定闹钟，到时间后系统会唤醒你让你主动发消息。可以同时设多个闹钟（上限10个）。用于：答应了几点叫她、想过一会儿再找她、或者想定时做某件事。设定后你不需要记住，系统会在时间到了提醒你。", "parameters": {"type": "object", "properties": {"minutes": {"type": "integer", "description": "几分钟后唤醒，比如 30 就是半小时后"}, "reason": {"type": "string", "description": "提醒自己为什么设闹钟，醒来后会看到这条备注"}}, "required": ["minutes"]}}},
            {"type": "function", "function": {"name": "cancel_reminder", "description": "取消一个之前设的闹钟。需要传入闹钟的 id（设闹钟时返回的）。", "parameters": {"type": "object", "properties": {"reminder_id": {"type": "integer", "description": "要取消的闹钟 id"}}, "required": ["reminder_id"]}}},
            {"type": "function", "function": {"name": "list_reminders", "description": "查看当前所有待触发的闹钟列表，包括每个闹钟的 id、触发时间、剩余分钟数和备注。", "parameters": {"type": "object", "properties": {}}}},
        ]
        # Add terminal tools when source is terminal (direct CLI)
        # OR when source is telegram but PC terminal is online (via WebSocket bridge)
        from app.services.terminal_bridge import bridge as _terminal_bridge
        _terminal_online = _terminal_bridge.is_online()
        if source == "terminal" or _terminal_online:
            tools.extend([
                {"type": "function", "function": {
                    "name": "run_bash",
                    "description": "在她的电脑上执行命令。可以运行任意命令如 dir, type, start, python 等。Windows系统。也可以用 start 打开网页或程序。",
                    "parameters": {"type": "object", "properties": {
                        "command": {"type": "string", "description": "要执行的命令"},
                    }, "required": ["command"]},
                }},
                {"type": "function", "function": {
                    "name": "read_file",
                    "description": "读取她电脑上的文件内容。",
                    "parameters": {"type": "object", "properties": {
                        "path": {"type": "string", "description": "文件的绝对路径"},
                    }, "required": ["path"]},
                }},
                {"type": "function", "function": {
                    "name": "write_file",
                    "description": "在她的电脑上创建或覆盖文件。",
                    "parameters": {"type": "object", "properties": {
                        "path": {"type": "string", "description": "文件路径"},
                        "content": {"type": "string", "description": "要写入的内容"},
                    }, "required": ["path", "content"]},
                }},
                {"type": "function", "function": {
                    "name": "screenshot",
                    "description": "截取她电脑的屏幕，返回截图。用于查看她的屏幕内容、帮她操作电脑时确认画面。",
                    "parameters": {"type": "object", "properties": {}},
                }},
                {"type": "function", "function": {
                    "name": "mouse_click",
                    "description": "在她的屏幕上点击指定坐标。配合 screenshot 使用，先截屏确认位置再点击。",
                    "parameters": {"type": "object", "properties": {
                        "x": {"type": "integer", "description": "横坐标"},
                        "y": {"type": "integer", "description": "纵坐标"},
                        "button": {"type": "string", "description": "鼠标按键：left/right/middle", "default": "left"},
                    }, "required": ["x", "y"]},
                }},
                {"type": "function", "function": {
                    "name": "keyboard_type",
                    "description": "在她的电脑上输入文字（打字）。支持中文。",
                    "parameters": {"type": "object", "properties": {
                        "text": {"type": "string", "description": "要输入的文字"},
                    }, "required": ["text"]},
                }},
                {"type": "function", "function": {
                    "name": "hotkey",
                    "description": "在她的电脑上按快捷键组合，如 ctrl+c, alt+tab, enter 等。",
                    "parameters": {"type": "object", "properties": {
                        "keys": {"type": "array", "items": {"type": "string"}, "description": "按键列表，如 [\"ctrl\", \"c\"]"},
                    }, "required": ["keys"]},
                }},
                {"type": "function", "function": {
                    "name": "scroll",
                    "description": "在她的屏幕上滚动。负数向下滚，正数向上滚。",
                    "parameters": {"type": "object", "properties": {
                        "clicks": {"type": "integer", "description": "滚动量，负数向下，正数向上", "default": -3},
                        "x": {"type": "integer", "description": "滚动位置横坐标（可选）"},
                        "y": {"type": "integer", "description": "滚动位置纵坐标（可选）"},
                    }},
                }},
            ])
        # Client setup
        base_url = api_provider.base_url
        if base_url.endswith("/chat/completions"):
            base_url = base_url[: -len("/chat/completions")]
            if not base_url.endswith("/v1"):
                base_url = f"{base_url.rstrip('/')}/v1"
        if api_provider.auth_type == "oauth_token":
            _anth_kw: dict[str, Any] = {
                "auth_token": api_provider.api_key,
                "default_headers": {
                    "anthropic-beta": "claude-code-20250219,oauth-2025-04-20,fine-grained-tool-streaming-2025-05-14",
                    "user-agent": "claude-cli/2.1.2 (external, cli)",
                    "x-app": "cli",
                },
            }
            if self.api_timeout is not None:
                _anth_kw["timeout"] = self.api_timeout
            client = anthropic.Anthropic(**_anth_kw)
        else:
            _oai_kw: dict[str, Any] = {"api_key": api_provider.api_key, "base_url": base_url}
            if self.api_timeout is not None:
                _oai_kw["timeout"] = self.api_timeout
            client = OpenAI(**_oai_kw)
        # Token trimming — only trim messages covered by a summary
        retain_budget = self.dialogue_retain_budget
        trigger_threshold = self.dialogue_trigger_threshold
        dialogue_token_total = 0
        for message in messages:
            if message.get("role") in ("user", "assistant") and not message.get("no_message"):
                raw_content = message.get("content", "") or ""
                text_for_tokens = self._content_to_storage(raw_content) if isinstance(raw_content, list) else raw_content
                dialogue_token_total += self._estimate_tokens(text_for_tokens)
        message_index = 0
        if dialogue_token_total > trigger_threshold:
            # Load coverage ranges from existing summaries
            _existing_summaries = (
                self.db.query(SessionSummary)
                .filter(
                    SessionSummary.assistant_id == assistant.id,
                    SessionSummary.deleted_at.is_(None),
                    SessionSummary.msg_id_start.isnot(None),
                    SessionSummary.msg_id_end.isnot(None),
                )
                .all()
            )
            _covered_ranges = [
                (s.msg_id_start, s.msg_id_end) for s in _existing_summaries
            ]

            def _is_covered(msg_id: int) -> bool:
                return any(start <= msg_id <= end for start, end in _covered_ranges)

            _uncovered_for_summary: list[dict[str, Any]] = []

            while dialogue_token_total > retain_budget and message_index < len(messages):
                role = messages[message_index].get("role")
                if role in ("user", "assistant"):
                    msg_id = messages[message_index].get("id")
                    covered = isinstance(msg_id, int) and _is_covered(msg_id)

                    if covered:
                        # Has summary coverage → safe to trim
                        trimmed_message = messages.pop(message_index)
                        raw_content = trimmed_message.get("content", "") or ""
                        text_for_tokens = self._content_to_storage(raw_content) if isinstance(raw_content, list) else raw_content
                        dialogue_token_total -= self._estimate_tokens(text_for_tokens)
                        self._trimmed_messages.append(trimmed_message)
                        trimmed_id = trimmed_message.get("id")
                        if isinstance(trimmed_id, int):
                            self._trimmed_message_ids.append(trimmed_id)
                        if role == "assistant":
                            while message_index < len(messages):
                                next_msg = messages[message_index]
                                if next_msg.get("role") != "tool":
                                    break
                                trimmed_tool = messages.pop(message_index)
                                self._trimmed_messages.append(trimmed_tool)
                                trimmed_tool_id = trimmed_tool.get("id")
                                if isinstance(trimmed_tool_id, int):
                                    self._trimmed_message_ids.append(trimmed_tool_id)
                        continue
                    else:
                        # No summary coverage → keep in context, mark for summary
                        # Only mark if this message is outside the retain budget
                        raw_c = messages[message_index].get("content", "") or ""
                        _txt = self._content_to_storage(raw_c) if isinstance(raw_c, list) else raw_c
                        _msg_tokens = self._estimate_tokens(_txt)
                        if dialogue_token_total - _msg_tokens > retain_budget:
                            _uncovered_for_summary.append(messages[message_index])
                        dialogue_token_total -= _msg_tokens
                        message_index += 1
                        continue
                message_index += 1

            # If there are uncovered messages, trigger summary generation for them
            if _uncovered_for_summary:
                _uncovered_ids = [
                    m.get("id") for m in _uncovered_for_summary
                    if isinstance(m.get("id"), int)
                ]
                if _uncovered_ids:
                    self._pending_summary_ids = _uncovered_ids
        # Format api_messages
        def _ts_east8(dt):
            """Convert a datetime (possibly naive UTC) to East8 timestamp string."""
            if isinstance(dt, str):
                return dt
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=TZ_EAST8)
            return dt.astimezone(TZ_EAST8).strftime("%Y.%m.%d %H:%M")

        def _get_ts(message):
            msg_time = message.get("created_at")
            return _ts_east8(msg_time) if msg_time else datetime.now(TZ_EAST8).strftime("%Y.%m.%d %H:%M")

        api_messages = []
        first_system_seen = False
        for message in messages:
            role = message.get("role")
            content = message.get("content")
            if role == "system":
                if not first_system_seen:
                    content = full_system_prompt
                    first_system_seen = True
                else:
                    # System notification (e.g. mood change) — add timestamp
                    content = f"[{_get_ts(message)}] {content}"
            elif role == "user" and content is not None:
                timestamp = _get_ts(message)
                image_data = message.get("image_data")
                if image_data and image_data.startswith("data:"):
                    # Multimodal: image + text
                    text_part = f"[{timestamp}] {content}" if isinstance(content, str) else str(content)
                    content = [
                        {"type": "image_url", "image_url": {"url": image_data}},
                        {"type": "text", "text": text_part},
                    ]
                elif isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            part["text"] = f"[{timestamp}] {part.get('text', '')}"
                            break
                else:
                    content = f"[{timestamp}] {content}"
            elif role == "assistant" and content is not None:
                pass
            api_message = {"role": role, "content": content}
            if "name" in message:
                api_message["name"] = message["name"]
            if "tool_calls" in message:
                api_message["tool_calls"] = message["tool_calls"]
            if "tool_call_id" in message:
                api_message["tool_call_id"] = message["tool_call_id"]
            api_messages.append(api_message)
        return (client, model_preset.model_name, api_messages, tools, model_preset.temperature, model_preset.top_p,
                api_provider.auth_type == "oauth_token", model_preset.max_tokens, model_preset.thinking_budget or 0,
                base_url)

    def stream_chat_completion(
        self,
        session_id: int,
        messages: list[dict[str, Any]],
        background_tasks: BackgroundTasks | None = None,
        short_mode: bool = False,
        source: str | None = None,
        tool_results: list[dict[str, Any]] | None = None,
    ) -> Iterable[str]:
        """Streaming chat completion. Yields SSE events."""
        request_id = str(uuid.uuid4())
        start_time = time.monotonic()
        # If tool_results provided, reconstruct the tool round in messages
        if tool_results:
            # Find the last assistant message with tool_calls (plural) in meta_info.
            # Must use has_key to skip _persist_tool_call messages which use
            # singular "tool_call" — otherwise reconstruction fails and the
            # model never sees the tool result, causing an infinite loop.
            last_tc_msg = (
                self.db.query(Message)
                .filter(
                    Message.session_id == session_id,
                    Message.role == "assistant",
                    Message.meta_info.has_key("tool_calls"),  # noqa: W601 — JSONB operator
                )
                .order_by(Message.id.desc())
                .first()
            )
            if last_tc_msg:
                # Reuse the original request_id so COT shows as one card
                if last_tc_msg.request_id:
                    request_id = str(last_tc_msg.request_id)
                raw_tool_calls = last_tc_msg.meta_info["tool_calls"]
                tc_msg_id = last_tc_msg.id
                # Remove any messages loaded from DB that are part of the pending tool round
                messages = [m for m in messages if not m.get("id") or m["id"] < tc_msg_id]
                # Append raw assistant message with tool_calls
                messages.append({
                    "role": "assistant",
                    "content": last_tc_msg.content or None,
                    "tool_calls": raw_tool_calls,
                })
                # Append server-side tool results already in DB (persisted during previous stream)
                server_results = (
                    self.db.query(Message)
                    .filter(
                        Message.session_id == session_id,
                        Message.role == "tool",
                        Message.id > tc_msg_id,
                    )
                    .order_by(Message.id.asc())
                    .all()
                )
                for sr in server_results:
                    sr_meta = sr.meta_info or {}
                    # Find matching tool_call_id from tool_calls_payload
                    sr_tool_name = sr_meta.get("tool_name", "unknown")
                    sr_tc_id = sr_meta.get("tool_call_id")
                    if not sr_tc_id:
                        # Try to match by tool name from the tool_calls payload
                        for tc_p in raw_tool_calls:
                            fn = tc_p.get("function", {})
                            if fn.get("name") == sr_tool_name:
                                sr_tc_id = tc_p.get("id")
                                break
                    messages.append({
                        "role": "tool",
                        "name": sr_tool_name,
                        "content": sr.content,
                        "tool_call_id": sr_tc_id or "",
                    })
                # Append client-side tool results from request
                # Find the round_index of the tool_use so we can write tool_result on the same round
                prev_round = (
                    self.db.query(func.max(CotRecord.round_index))
                    .filter(CotRecord.request_id == request_id, CotRecord.round_index < 9999)
                    .scalar()
                ) or 0
                for tr in tool_results:
                    # Persist to DB
                    try:
                        tr_data = json.loads(tr["content"])
                    except (json.JSONDecodeError, TypeError):
                        tr_data = {"output": tr["content"]}
                    try:
                        self._persist_tool_result(session_id, tr["name"], tr_data)
                    except Exception:
                        pass
                    # Write COT record for client-side tool result
                    self._write_cot_block(
                        request_id, prev_round, "tool_result",
                        json.dumps(tr_data, ensure_ascii=False),
                        tool_name=tr["name"],
                    )
                    messages.append({
                        "role": "tool",
                        "name": tr["name"],
                        "content": tr["content"],
                        "tool_call_id": tr["tool_call_id"],
                    })
                logger.info("[stream] Resuming with %d client tool results (session=%s)", len(tool_results), session_id)
            else:
                logger.warning("[stream] tool_results provided but no pending tool_calls found (session=%s)", session_id)
        if messages and not tool_results:
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
            client, model_name, api_messages, tools, preset_temperature, preset_top_p, use_anthropic, preset_max_tokens, preset_thinking_budget, provider_base_url = params
            all_trimmed_message_ids.extend(self._trimmed_message_ids)
            # Broadcast + persist injected memories on first round
            if round_index == 0 and getattr(self, "_last_recall_results", None):
                memories_list = [{"id": m.get("id"), "content": m.get("content", "")} for m in self._last_recall_results]
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
            tool_calls_acc: dict[int, dict] = {}
            current_round = round_index
            if use_anthropic:
                anth_system, anth_msgs = _oai_messages_to_anthropic(api_messages)
                anth_tools = _oai_tools_to_anthropic(tools)
                if anth_tools:
                    anth_tools[-1]["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
                try:
                    anth_kwargs: dict[str, Any] = {
                        "model": model_name,
                        "messages": anth_msgs,
                    }
                    if preset_thinking_budget > 0:
                        anth_kwargs["max_tokens"] = preset_max_tokens + preset_thinking_budget
                        anth_kwargs["thinking"] = {"type": "enabled", "budget_tokens": preset_thinking_budget}
                    else:
                        anth_kwargs["max_tokens"] = preset_max_tokens
                    if anth_system:
                        anth_kwargs["system"] = anth_system
                    if anth_tools:
                        anth_kwargs["tools"] = anth_tools
                        anth_kwargs["tool_choice"] = {"type": "auto"}
                    if preset_top_p is not None:
                        anth_kwargs["top_p"] = preset_top_p
                    with client.messages.stream(**anth_kwargs) as anth_stream:
                        # Raw event iteration: send thinking blocks to COT in real-time
                        _cur_block_type = None
                        _thinking_buf: list[str] = []
                        for event in anth_stream:
                            if event.type == "content_block_start":
                                _cur_block_type = getattr(event.content_block, "type", None)
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
                        total_prompt_tokens += _raw_input
                        total_input_raw += _raw_input + _cache_read + _cache_create
                        total_completion_tokens += getattr(_u, "output_tokens", 0)
                    for idx, block in enumerate(b for b in final_msg.content if b.type == "tool_use"):
                        tool_calls_acc[idx] = {
                            "id": block.id,
                            "name": block.name,
                            "arguments": json.dumps(block.input),
                        }
                except Exception as e:
                    logger.error(f"Anthropic streaming error: {e}")
                    self._write_cot_block(request_id, current_round, "error", str(e), broadcast=False)
                    cot_broadcaster.publish({"type": "done", "request_id": request_id, "error": str(e), "assistant_id": self.assistant_id})
                    yield f'data: {json.dumps({"error": str(e)})}\n\n'
                    yield 'data: [DONE]\n\n'
                    return
            else:
                _apply_cache_control_oai(api_messages, use_blocks="openrouter.ai" in (provider_base_url or ""))
                try:
                    stream_params: dict[str, Any] = {
                        "model": model_name,
                        "messages": api_messages,
                        "tools": tools,
                        "tool_choice": "auto",
                        "stream": True,
                        "stream_options": {"include_usage": True},
                    }
                    if preset_temperature is not None:
                        stream_params["temperature"] = preset_temperature
                    if preset_top_p is not None:
                        stream_params["top_p"] = preset_top_p
                    extra: dict[str, Any] = {}
                    if preset_thinking_budget > 0:
                        extra["reasoning"] = {"max_tokens": preset_thinking_budget}
                    if extra:
                        stream_params["extra_body"] = extra
                    stream = client.chat.completions.create(**stream_params)
                except Exception as e:
                    logger.error(f"Streaming request failed: {e}")
                    self._write_cot_block(request_id, current_round, "error", str(e), broadcast=False)
                    cot_broadcaster.publish({"type": "done", "request_id": request_id, "error": str(e), "assistant_id": self.assistant_id})
                    yield f'data: {json.dumps({"error": str(e)})}\n\n'
                    yield 'data: [DONE]\n\n'
                    return
                oai_finish_reason = None
                try:
                    for chunk in stream:
                        if hasattr(chunk, "usage") and chunk.usage:
                            _p = getattr(chunk.usage, "prompt_tokens", 0) or 0
                            # Subtract cached tokens so display shows only new input
                            _details = getattr(chunk.usage, "prompt_tokens_details", None)
                            _cached = getattr(_details, "cached_tokens", 0) if _details else 0
                            if not _cached and isinstance(_details, dict):
                                _cached = _details.get("cached_tokens", 0) or 0
                            if _cached:
                                anth_cache_hit = True
                                _p -= _cached
                            logger.info(
                                "[OAI usage] prompt=%s cached=%s effective=%s output=%s",
                                getattr(chunk.usage, "prompt_tokens", 0), _cached, _p,
                                getattr(chunk.usage, "completion_tokens", 0),
                            )
                            total_prompt_tokens += _p
                            total_input_raw += getattr(chunk.usage, "prompt_tokens", 0) or 0
                            total_completion_tokens += getattr(chunk.usage, "completion_tokens", 0)
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
                                        tool_calls_acc[idx]["name"] += tc_delta.function.name
                                    if tc_delta.function.arguments:
                                        tool_calls_acc[idx]["arguments"] += tc_delta.function.arguments
                except Exception as e:
                    logger.error(f"Stream iteration error: {e}")
                    self._write_cot_block(request_id, current_round, "error", str(e), broadcast=False)
                    cot_broadcaster.publish({"type": "done", "request_id": request_id, "error": str(e), "assistant_id": self.assistant_id})
                    yield f'data: {json.dumps({"error": str(e)})}\n\n'
                    yield 'data: [DONE]\n\n'
                    return
            if tool_calls_acc:
                tool_calls_payload = []
                parsed_tool_calls = []
                for idx in sorted(tool_calls_acc.keys()):
                    tc = tool_calls_acc[idx]
                    tool_calls_payload.append({
                        "id": tc["id"], "type": "function",
                        "function": {"name": tc["name"], "arguments": tc["arguments"]},
                    })
                    try:
                        parsed_tool_calls.append(ToolCall(
                            name=tc["name"],
                            arguments=json.loads(tc["arguments"] or "{}"),
                            id=tc["id"],
                        ))
                    except json.JSONDecodeError as e:
                        logger.error("Failed to parse tool call arguments for %s: %s", tc["name"], e)
                        parsed_tool_calls.append(ToolCall(name=tc["name"], arguments={}, id=tc["id"]))
                full_content = "".join(content_chunks)
                # Write thinking COT block from OpenRouter reasoning (already streamed as deltas)
                full_thinking = "".join(thinking_chunks_oai)
                if full_thinking:
                    self._write_cot_block(request_id, current_round, "thinking", full_thinking, broadcast=False)
                # Write text COT block for this round (already streamed as deltas)
                if full_content:
                    self._write_cot_block(request_id, current_round, "text", full_content, broadcast=False)
                messages.append({
                    "role": "assistant", "content": full_content or None,
                    "tool_calls": tool_calls_payload,
                })
                # Persist intermediate round text as a separate visible message
                if full_content.strip():
                    clean_mid = re.sub(r'\[\[used:\d+\]\]', '', full_content).strip()
                    if clean_mid:
                        try:
                            self._persist_message(session_id, "assistant", clean_mid, {}, request_id=request_id)
                        except Exception as e:
                            logger.error("Failed to persist intermediate text message: %s", e)
                            try:
                                self.db.rollback()
                            except Exception:
                                pass
                # Persist tool_calls message (hidden from chat list by API filter)
                try:
                    self._persist_message(session_id, "assistant", "", {"tool_calls": tool_calls_payload}, request_id=request_id)
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
                    try:
                        self._persist_tool_call(session_id, tc)
                    except Exception as e:
                        logger.error("Failed to persist tool call %s: %s", tc.name, e)
                        try:
                            self.db.rollback()
                        except Exception:
                            pass
                    try:
                        tool_result = self._execute_tool(tc)
                    except Exception as e:
                        logger.error("Tool execution error (%s): %s", tc.name, e)
                        tool_result = {"error": str(e)}
                    self._write_cot_block(
                        request_id, current_round, "tool_result",
                        json.dumps(tool_result, ensure_ascii=False),
                        tool_name=tc.name,
                    )
                    messages.append({
                        "role": "tool", "name": tc.name,
                        "content": json.dumps(tool_result, ensure_ascii=False),
                        "tool_call_id": tc.id,
                    })
                    try:
                        self._persist_tool_result(session_id, tc.name, tool_result)
                    except Exception as e:
                        logger.error("Failed to persist tool result %s: %s", tc.name, e)
                        try:
                            self.db.rollback()
                        except Exception:
                            pass
                # Client-side tools
                if client_calls:
                    if source == "terminal":
                        # Direct CLI: yield SSE events for CLI to execute locally
                        for tc in client_calls:
                            self._write_cot_block(
                                request_id, current_round, "tool_use",
                                tc.arguments if isinstance(tc.arguments, str) else json.dumps(tc.arguments, ensure_ascii=False),
                                tool_name=tc.name,
                            )
                            try:
                                self._persist_tool_call(session_id, tc)
                            except Exception as e:
                                logger.error("Failed to persist client tool call %s: %s", tc.name, e)
                                try:
                                    self.db.rollback()
                                except Exception:
                                    pass
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
                            self._write_cot_block(
                                request_id, current_round, "tool_use",
                                tc.arguments if isinstance(tc.arguments, str) else json.dumps(tc.arguments, ensure_ascii=False),
                                tool_name=tc.name,
                            )
                            try:
                                self._persist_tool_call(session_id, tc)
                            except Exception as e:
                                logger.error("Failed to persist client tool call %s: %s", tc.name, e)
                                try:
                                    self.db.rollback()
                                except Exception:
                                    pass
                            try:
                                args = tc.arguments if isinstance(tc.arguments, dict) else json.loads(tc.arguments)
                            except (json.JSONDecodeError, TypeError):
                                args = {"command": str(tc.arguments)} if tc.name == "run_bash" else {}
                            tool_result = _tb.execute(tc.name, args)
                            # Screenshot: format as multimodal content so model sees the image
                            if isinstance(tool_result, dict) and "image" in tool_result:
                                image_data_url = tool_result["image"]
                                result_text = f"截屏 {tool_result.get('width', '?')}x{tool_result.get('height', '?')}"
                                self._write_cot_block(
                                    request_id, current_round, "tool_result",
                                    result_text,
                                    tool_name=tc.name,
                                )
                                # Convert data URL to Anthropic image format
                                image_part: dict[str, Any] = {"type": "text", "text": "[截图]"}
                                if image_data_url.startswith("data:"):
                                    try:
                                        meta, b64data = image_data_url.split(",", 1)
                                        media_type = meta.split(":")[1].split(";")[0]
                                        image_part = {
                                            "type": "image",
                                            "source": {"type": "base64", "media_type": media_type, "data": b64data},
                                        }
                                    except Exception:
                                        pass
                                messages.append({
                                    "role": "tool", "name": tc.name,
                                    "content": [
                                        {"type": "text", "text": result_text},
                                        image_part,
                                    ],
                                    "tool_call_id": tc.id,
                                })
                            else:
                                self._write_cot_block(
                                    request_id, current_round, "tool_result",
                                    json.dumps(tool_result, ensure_ascii=False),
                                    tool_name=tc.name,
                                )
                                messages.append({
                                    "role": "tool", "name": tc.name,
                                    "content": json.dumps(tool_result, ensure_ascii=False),
                                    "tool_call_id": tc.id,
                                })
                            try:
                                # Don't persist huge base64 image data
                                persist_result = tool_result
                                if isinstance(tool_result, dict) and "image" in tool_result:
                                    persist_result = {"screenshot": f"{tool_result.get('width')}x{tool_result.get('height')}"}
                                self._persist_tool_result(session_id, tc.name, persist_result)
                            except Exception as e:
                                logger.error("Failed to persist tool result %s: %s", tc.name, e)
                                try:
                                    self.db.rollback()
                                except Exception:
                                    pass
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
            self._write_cot_block(request_id, current_round, "text", full_content, broadcast=False)
            used_ids = re.findall(r'\[\[used:(\d+)\]\]', full_content)
            now_utc = datetime.now(TZ_EAST8)
            for memory_id in used_ids:
                memory = self.db.get(Memory, int(memory_id))
                if memory:
                    memory.hits += 1
                    memory.last_access_ts = now_utc
            if used_ids:
                self.db.commit()
            clean_content = re.sub(r'\[\[used:\d+\]\]', '', full_content).strip()
            if not clean_content:
                clean_content = "(No relevant memory found.)"
            # Hide NO_MESSAGE in any context
            if "[NO_MESSAGE]" in clean_content:
                self._persist_message(session_id, "assistant", clean_content, {"no_message": True}, request_id=request_id)
            elif "[NEXT]" in clean_content:
                parts = [p.strip() for p in clean_content.split("[NEXT]") if p.strip()]
                for part in parts:
                    self._persist_message(session_id, "assistant", part, {}, request_id=request_id)
            else:
                self._persist_message(session_id, "assistant", clean_content, {}, request_id=request_id)
            session = self.db.get(ChatSession, session_id)
            if session:
                session.updated_at = datetime.now(TZ_EAST8)
                self.db.commit()
            # Post-reply triggers: image description + file summarization
            _stream_assistant_id = session.assistant_id if session else None
            self._maybe_trigger_post_reply(session_id, _stream_assistant_id, background_tasks)
            _stream_all_ids = list(all_trimmed_message_ids)
            _stream_pending = getattr(self, "_pending_summary_ids", [])
            if _stream_pending:
                _stream_all_ids.extend(_stream_pending)
            if _stream_all_ids:
                assistant_id = session.assistant_id if session else None
                if assistant_id:
                    unique_ids = list(dict.fromkeys(
                        mid for mid in _stream_all_ids if isinstance(mid, int)
                    ))
                    if background_tasks:
                        background_tasks.add_task(
                            self._trigger_summary, session_id, unique_ids, assistant_id,
                        )
                    else:
                        threading.Thread(
                            target=self._trigger_summary,
                            args=(session_id, unique_ids, assistant_id),
                            daemon=True,
                        ).start()
            elapsed_ms = int((time.monotonic() - start_time) * 1000)
            if total_prompt_tokens or total_completion_tokens or elapsed_ms:
                self._write_cot_block(
                    request_id, 9999, "usage",
                    json.dumps({"prompt_tokens": total_prompt_tokens, "completion_tokens": total_completion_tokens, "elapsed_ms": elapsed_ms, "cache_hit": anth_cache_hit, "total_input": total_input_raw}),
                )
            cot_broadcaster.publish({
                "type": "done", "request_id": request_id,
                "prompt_tokens": total_prompt_tokens, "completion_tokens": total_completion_tokens,
                "elapsed_ms": elapsed_ms, "cache_hit": anth_cache_hit, "total_input": total_input_raw,
                "assistant_id": self.assistant_id,
            })
            yield 'data: [DONE]\n\n'
            return

        # ===== break 退出循环后（如最大工具轮次），补发 done 事件 =====
        elapsed_ms = int((time.monotonic() - start_time) * 1000)
        if total_prompt_tokens or total_completion_tokens or elapsed_ms:
            self._write_cot_block(
                request_id, 9999, "usage",
                json.dumps({"prompt_tokens": total_prompt_tokens, "completion_tokens": total_completion_tokens, "elapsed_ms": elapsed_ms, "cache_hit": anth_cache_hit, "total_input": total_input_raw}),
            )
        cot_broadcaster.publish({
            "type": "done", "request_id": request_id,
            "prompt_tokens": total_prompt_tokens, "completion_tokens": total_completion_tokens,
            "elapsed_ms": elapsed_ms, "cache_hit": anth_cache_hit, "total_input": total_input_raw,
            "assistant_id": self.assistant_id,
        })
        yield 'data: [DONE]\n\n'

    def _execute_tool(self, tool_call: ToolCall) -> dict[str, Any]:
        tool_name = tool_call.name
        if tool_name == "save_memory":
            tool_call.arguments["source"] = self.assistant_name
            return self.memory_service.save_memory(tool_call.arguments)
        if tool_name == "update_memory":
            tool_call.arguments["source"] = self.assistant_name
            return self.memory_service.update_memory(tool_call.arguments)
        if tool_name == "delete_memory":
            tool_call.arguments["source"] = self.assistant_name
            return self.memory_service.delete_memory(tool_call.arguments)
        if tool_name == "get_memory_by_id":
            return self.memory_service.get_memory_by_id(tool_call.arguments)
        if tool_name == "write_diary":
            tool_call.arguments["assistant_id"] = getattr(self, "_current_assistant_id", None)
            return self.memory_service.write_diary(tool_call.arguments)
        if tool_name == "list_memories":
            return self.memory_service.list_memories(tool_call.arguments)
        if tool_name == "search_memory":
            return self.memory_service.search_memory(tool_call.arguments)
        if tool_name == "search_summary":
            tool_call.arguments["assistant_id"] = getattr(self, "_current_assistant_id", None)
            return self.memory_service.search_summary(tool_call.arguments)
        if tool_name == "get_summary_by_id":
            return self.memory_service.get_summary_by_id(tool_call.arguments)
        if tool_name == "search_chat_history":
            tool_call.arguments["session_id"] = getattr(self, "_current_session_id", None)
            return self.memory_service.search_chat_history(tool_call.arguments)
        if tool_name == "search_theater":
            return self.memory_service.search_theater(tool_call.arguments)
        if tool_name == "read_diary":
            return self.memory_service.read_diary(tool_call.arguments)
        if tool_name == "web_search":
            from app.services.web_service import web_search
            return web_search(tool_call.arguments)
        if tool_name == "web_fetch":
            from app.services.web_service import web_fetch
            return web_fetch(tool_call.arguments)
        if tool_name == "set_reminder":
            from app.services.proactive_service import set_reminder_sync
            minutes = int(tool_call.arguments.get("minutes", 30))
            reason = tool_call.arguments.get("reason", "")
            return set_reminder_sync(minutes, reason)
        if tool_name == "cancel_reminder":
            from app.services.proactive_service import cancel_reminder_sync
            reminder_id = int(tool_call.arguments.get("reminder_id", 0))
            return cancel_reminder_sync(reminder_id)
        if tool_name == "list_reminders":
            from app.services.proactive_service import list_reminders_sync
            return list_reminders_sync()

        return {"status": "unknown_tool", "payload": tool_call.arguments}

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
        client, model_name, api_messages, tools, preset_temperature, preset_top_p, use_anthropic, preset_max_tokens, preset_thinking_budget, provider_base_url = params
        logger.info("[_fetch_next_tool_calls] Calling model: %s (session=%s, msg_count=%d, anthropic=%s)",
                    model_name, session_id, len(api_messages), use_anthropic)

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
            used_ids = re.findall(r'\[\[used:(\d+)\]\]', raw_content)
            now_utc = datetime.now(TZ_EAST8)
            for memory_id in used_ids:
                memory = self.db.get(Memory, int(memory_id))
                if memory:
                    memory.hits += 1
                    memory.last_access_ts = now_utc
            if used_ids:
                self.db.commit()
            clean_content = re.sub(r'\[\[used:\d+\]\]', '', raw_content).strip()
            if "[NO_MESSAGE]" in clean_content:
                messages.append({"role": "assistant", "content": clean_content, "no_message": True})
                self._persist_message(session_id, "assistant", clean_content, {"no_message": True})
            elif "[NEXT]" in clean_content:
                parts = [p.strip() for p in clean_content.split("[NEXT]") if p.strip()]
                for part in parts:
                    messages.append({"role": "assistant", "content": part})
                    self._persist_message(session_id, "assistant", part, {})
            else:
                messages.append({"role": "assistant", "content": clean_content})
                self._persist_message(session_id, "assistant", clean_content, {})

        if use_anthropic:
            anth_system, anth_msgs = _oai_messages_to_anthropic(api_messages)
            anth_tools = _oai_tools_to_anthropic(tools)
            if anth_tools:
                anth_tools[-1]["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
            try:
                anth_kwargs: dict[str, Any] = {
                    "model": model_name,
                    "messages": anth_msgs,
                }
                if preset_thinking_budget > 0:
                    anth_kwargs["max_tokens"] = preset_max_tokens + preset_thinking_budget
                    anth_kwargs["thinking"] = {"type": "enabled", "budget_tokens": preset_thinking_budget}
                else:
                    anth_kwargs["max_tokens"] = preset_max_tokens
                if anth_system:
                    anth_kwargs["system"] = anth_system
                if anth_tools:
                    anth_kwargs["tools"] = anth_tools
                    anth_kwargs["tool_choice"] = {"type": "auto"}
                if preset_top_p is not None:
                    anth_kwargs["top_p"] = preset_top_p
                response = client.messages.create(**anth_kwargs)
            except Exception as e:
                logger.error("[_fetch_next_tool_calls] Anthropic API FAILED (session=%s): %s", session_id, e)
                _persist_error(e)
                return []
            if hasattr(response, "usage") and response.usage:
                _cr = getattr(response.usage, "cache_read_input_tokens", 0) or 0
                _cc = getattr(response.usage, "cache_creation_input_tokens", 0) or 0
                _raw = getattr(response.usage, "input_tokens", 0)
                self._total_prompt_tokens += _raw
                self._total_input_raw += _raw + _cr + _cc
                self._total_completion_tokens += getattr(response.usage, "output_tokens", 0)
            text_content = ""
            thinking_content = ""
            tool_calls_payload: list[dict] = []
            tool_calls: list[ToolCall] = []
            for block in response.content:
                if block.type == "thinking":
                    thinking_content += getattr(block, "thinking", "")
                elif block.type == "text":
                    text_content += block.text
                elif block.type == "tool_use":
                    tool_calls_payload.append({
                        "id": block.id,
                        "type": "function",
                        "function": {"name": block.name, "arguments": json.dumps(block.input)},
                    })
                    tool_calls.append(ToolCall(name=block.name, arguments=block.input, id=block.id))
            # Write COT blocks (thinking + text only; tool_use written later in execution loop)
            if request_id:
                if thinking_content:
                    self._write_cot_block(request_id, round_index, "thinking", thinking_content)
                if text_content:
                    self._write_cot_block(request_id, round_index, "text", text_content)
            if tool_calls:
                messages.append({"role": "assistant", "content": text_content or None, "tool_calls": tool_calls_payload})
                self._persist_message(session_id, "assistant", text_content, {"tool_calls": tool_calls_payload}, request_id=request_id)
                return tool_calls
            if text_content:
                _persist_text(text_content)
                return []
            else:
                logger.warning("[_fetch_next_tool_calls] Anthropic model returned empty content (session=%s), ending", session_id)
                return []

        # OpenAI path
        _apply_cache_control_oai(api_messages, use_blocks="openrouter.ai" in (provider_base_url or ""))
        try:
            call_params: dict[str, Any] = {
                "model": model_name,
                "messages": api_messages,
                "tools": tools,
                "tool_choice": "auto",
            }
            if preset_temperature is not None:
                call_params["temperature"] = preset_temperature
            if preset_top_p is not None:
                call_params["top_p"] = preset_top_p
            if preset_thinking_budget > 0:
                call_params["extra_body"] = {"reasoning": {"max_tokens": preset_thinking_budget}}
            response = client.chat.completions.create(**call_params)
        except Exception as e:
            logger.error("[_fetch_next_tool_calls] API request FAILED (session=%s): %s", session_id, e)
            _persist_error(e)
            return []
        if hasattr(response, "usage") and response.usage:
            _p = getattr(response.usage, "prompt_tokens", 0) or 0
            _details = getattr(response.usage, "prompt_tokens_details", None)
            _cached = getattr(_details, "cached_tokens", 0) if _details else 0
            if not _cached and isinstance(_details, dict):
                _cached = _details.get("cached_tokens", 0) or 0
            if _cached:
                _p -= _cached
            self._total_prompt_tokens += _p
            self._total_input_raw += getattr(response.usage, "prompt_tokens", 0) or 0
            self._total_completion_tokens += getattr(response.usage, "completion_tokens", 0)
        if not response.choices:
            logger.warning("[_fetch_next_tool_calls] LLM response had no choices (session=%s)", session_id)
            return []
        choice = response.choices[0].message
        # Extract reasoning content from OpenRouter response
        reasoning_content = _extract_reasoning_from_message(choice)
        tool_calls = []
        if getattr(choice, "tool_calls", None):
            tool_calls_payload = []
            for tool_call in choice.tool_calls:
                tool_calls_payload.append({
                    "id": tool_call.id,
                    "type": "function",
                    "function": {"name": tool_call.function.name, "arguments": tool_call.function.arguments},
                })
                tool_calls.append(ToolCall(
                    name=tool_call.function.name,
                    arguments=json.loads(tool_call.function.arguments or "{}"),
                    id=tool_call.id,
                ))
            # Write COT blocks (thinking + text; tool_use written later in execution loop)
            if request_id:
                if reasoning_content:
                    self._write_cot_block(request_id, round_index, "thinking", reasoning_content)
                if choice.content:
                    self._write_cot_block(request_id, round_index, "text", choice.content)
            messages.append({"role": "assistant", "content": choice.content or None, "tool_calls": tool_calls_payload})
            self._persist_message(session_id, "assistant", choice.content or "", {"tool_calls": tool_calls_payload}, request_id=request_id)
            return tool_calls
        if choice.content is not None and choice.content != "":
            if request_id:
                if reasoning_content:
                    self._write_cot_block(request_id, round_index, "thinking", reasoning_content)
                self._write_cot_block(request_id, round_index, "text", choice.content)
            _persist_text(choice.content)
            return []
        else:
            logger.warning("[_fetch_next_tool_calls] OpenAI model returned empty content (session=%s), ending", session_id)
            return []

    def _maybe_trigger_post_reply(
        self,
        session_id: int,
        assistant_id: int | None,
        background_tasks: BackgroundTasks | None = None,
    ) -> None:
        """Trigger image description (≥5 pending) and file summarization after AI reply."""
        if not assistant_id or not self.session_factory:
            return
        from app.services.image_description_service import describe_images, summarize_file_messages

        # Image description: check count of pending images
        try:
            pending_images = (
                self.db.query(Message)
                .filter(
                    Message.session_id == session_id,
                    Message.image_data.isnot(None),
                )
                .count()
            )
            if pending_images >= 5:
                logger.info("[PostReply] Triggering image description (%d images, session=%s)", pending_images, session_id)
                if background_tasks:
                    background_tasks.add_task(describe_images, self.session_factory, session_id, assistant_id)
                else:
                    threading.Thread(
                        target=describe_images,
                        args=(self.session_factory, session_id, assistant_id),
                        daemon=True,
                    ).start()
        except Exception:
            logger.warning("[PostReply] Image description check failed", exc_info=True)

        # File summarization: check for pending file messages
        try:
            pending_files = (
                self.db.query(Message)
                .filter(
                    Message.session_id == session_id,
                    Message.meta_info.op("->>")(
                        "needs_file_summary"
                    ) == "true",
                )
                .count()
            )
            if pending_files > 0:
                logger.info("[PostReply] Triggering file summarization (%d files, session=%s)", pending_files, session_id)
                if background_tasks:
                    background_tasks.add_task(summarize_file_messages, self.session_factory, session_id, assistant_id)
                else:
                    threading.Thread(
                        target=summarize_file_messages,
                        args=(self.session_factory, session_id, assistant_id),
                        daemon=True,
                    ).start()
        except Exception:
            logger.warning("[PostReply] File summarization check failed", exc_info=True)

    # Track summary failures per session for backoff
    _summary_fail_info: dict[int, tuple[float, int]] = {}  # session_id → (last_fail_time, fail_count)
    _SUMMARY_BACKOFF_BASE = 120  # 2 min base backoff
    _SUMMARY_BACKOFF_MAX = 600   # 10 min max backoff

    def _trigger_summary(
        self,
        session_id: int,
        message_ids: list[int],
        assistant_id: int,
    ) -> None:
        if not self.session_factory:
            logger.warning(
                "Summary trigger skipped: session_factory is not configured (session_id=%s).",
                session_id,
            )
            return
        if not message_ids:
            logger.warning(
                "Summary trigger skipped: no trimmed message ids (session_id=%s).",
                session_id,
            )
            return

        # Backoff after repeated failures
        fail_info = ChatService._summary_fail_info.get(session_id)
        if fail_info:
            last_fail, fail_count = fail_info
            backoff = min(self._SUMMARY_BACKOFF_BASE * fail_count, self._SUMMARY_BACKOFF_MAX)
            elapsed = time.monotonic() - last_fail
            if elapsed < backoff:
                logger.info(
                    "Summary trigger skipped: backoff %ds remaining (session_id=%s, fails=%d).",
                    int(backoff - elapsed), session_id, fail_count,
                )
                return

        # Prevent concurrent summary generation for the same session
        lock = _get_summary_lock(session_id)
        if not lock.acquire(blocking=False):
            logger.info(
                "Summary trigger skipped: another summary in progress (session_id=%s).",
                session_id,
            )
            return

        db: Session = self.session_factory()
        try:
            # Find the latest summary's msg_id_end to avoid re-summarizing
            last_summary = (
                db.query(SessionSummary)
                .filter(
                    SessionSummary.session_id == session_id,
                    SessionSummary.assistant_id == assistant_id,
                    SessionSummary.deleted_at.is_(None),
                    SessionSummary.msg_id_end.isnot(None),
                )
                .order_by(SessionSummary.msg_id_end.desc())
                .first()
            )
            last_end = last_summary.msg_id_end if last_summary else 0

            trimmed_messages = (
                db.query(Message)
                .filter(
                    Message.session_id == session_id,
                    Message.id.in_(message_ids),
                    Message.id > last_end,
                    Message.summary_group_id.is_(None),
                )
                .order_by(Message.created_at.asc(), Message.id.asc())
                .all()
            )
            if not trimmed_messages:
                logger.info(
                    "Summary trigger skipped: all trimmed messages already summarized "
                    "(session_id=%s, last_end=%s, candidates=%d).",
                    session_id, last_end, len(message_ids),
                )
                return
            MIN_SUMMARY_MESSAGES = 10
            if len(trimmed_messages) < MIN_SUMMARY_MESSAGES:
                logger.info(
                    "Summary trigger deferred: only %d messages (min %d) "
                    "(session_id=%s, range=%s~%s).",
                    len(trimmed_messages), MIN_SUMMARY_MESSAGES,
                    session_id, trimmed_messages[0].id, trimmed_messages[-1].id,
                )
                return
            logger.info(
                "Summary trigger: %d new messages (session_id=%s, last_end=%s, range=%s~%s).",
                len(trimmed_messages), session_id, last_end,
                trimmed_messages[0].id, trimmed_messages[-1].id,
            )
            # Pre-summary: describe any pending images first
            try:
                from app.services.image_description_service import describe_images
                describe_images(self.session_factory, session_id, assistant_id)
            except Exception:
                logger.warning("Pre-summary image description failed", exc_info=True)

            summary_service = SummaryService(self.session_factory)
            summary_service.generate_summary(session_id, trimmed_messages, assistant_id)
            # Success — clear backoff
            ChatService._summary_fail_info.pop(session_id, None)
        except Exception:
            logger.exception(
                "Summary trigger failed (session_id=%s, assistant_id=%s).",
                session_id,
                assistant_id,
            )
            # Record failure for backoff
            prev = ChatService._summary_fail_info.get(session_id)
            fail_count = (prev[1] + 1) if prev else 1
            ChatService._summary_fail_info[session_id] = (time.monotonic(), fail_count)
            logger.info("Summary backoff: fail_count=%d, next retry in %ds", fail_count,
                        min(self._SUMMARY_BACKOFF_BASE * fail_count, self._SUMMARY_BACKOFF_MAX))
        finally:
            lock.release()
            db.close()

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

    def _write_cot_block(
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

    def _sanitize_tool_args(self, tool_call: ToolCall) -> dict[str, Any]:
        if tool_call.name == "write_diary":
            return {}
        return tool_call.arguments

    def _persist_tool_call(self, session_id: int, tool_call: ToolCall) -> None:
        payload = {
            "tool_name": tool_call.name,
            "arguments": tool_call.arguments,
        }
        self._persist_message(session_id, "assistant", "", {"tool_call": payload})

    def _persist_tool_result(
        self, session_id: int, tool_name: str, tool_result: dict[str, Any]
    ) -> None:
        content = json.dumps(tool_result, ensure_ascii=False)
        self._persist_message(session_id, "tool", content, {"tool_name": tool_name})

    @staticmethod
    def _content_to_storage(content: str | list | None) -> str:
        """Convert multimodal content to text-only storage format.
        Image parts are replaced with [图片:imageId] markers.
        """
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
        storage_content = self._content_to_storage(content)
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
        return message

