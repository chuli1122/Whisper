from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import RinChat, RinMemory
from app.services.memory_service import MemoryService
from app.utils import format_datetime

router = APIRouter()
RIN_API_KEY = os.getenv("RIN_API_KEY", "")


async def _rin_auth(request: Request):
    """Allow localhost/private-network callers or requests with the Rin API key."""
    client = request.client.host if request.client else ""
    if client in ("127.0.0.1", "::1", "localhost"):
        return
    if client.startswith("172.") or client.startswith("192.168."):
        return
    if RIN_API_KEY and request.headers.get("X-Rin-Key") == RIN_API_KEY:
        return
    raise HTTPException(status_code=403, detail="Forbidden")


router.dependencies = [Depends(_rin_auth)]


class RinMemoryWrite(BaseModel):
    key: str
    content: str


class RinChatWrite(BaseModel):
    role: str
    content: str


@router.get("/rin/memory")
async def read_memory(
    key: str | None = None,
    prefix: str | None = None,
    history: bool = False,
    after: str | None = None,
    before: str | None = None,
    type: str | None = None,
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=50),
    summary_only: bool = False,
    db: Session = Depends(get_db),
):
    """Read Rin memories by exact key, prefix, type, or time range."""

    def _maybe_trim(content: str) -> str:
        if not summary_only or key:
            return content
        if content is None or len(content) <= 120:
            return content
        return content[:120] + "..."

    type_prefix_map = {
        "diary": "diary:",
        "weekly": "weekly:",
        "memo": "memo:",
        "feedback": "feedback:",
        "project": "project:",
        "ref": "ref:",
        "bootstrap": "bootstrap",
    }

    if type and type != "all":
        prefix = type_prefix_map.get(type, f"{type}:")

    if key:
        query = db.query(RinMemory).filter(RinMemory.key == key)
        if history:
            items = query.order_by(RinMemory.created_at.asc()).all()
        else:
            items = query.order_by(RinMemory.created_at.desc()).limit(1).all()
        return {
            "items": [
                {"id": m.id, "key": m.key, "content": _maybe_trim(m.content), "created_at": format_datetime(m.created_at)}
                for m in items
            ],
        }

    where_parts: list[str] = []
    params: dict = {"limit": limit, "offset": (page - 1) * limit}

    if prefix:
        if prefix == "bootstrap":
            where_parts.append("key = 'bootstrap'")
        else:
            where_parts.append("key LIKE :prefix")
            params["prefix"] = f"{prefix}%"

    if after:
        where_parts.append("created_at >= CAST(:after AS timestamptz)")
        params["after"] = after
    if before:
        where_parts.append("created_at < CAST(:before AS timestamptz)")
        params["before"] = before

    where_clause = " AND ".join(where_parts) if where_parts else "TRUE"

    if history:
        sql = text(f"""
            SELECT id, key, content, created_at
            FROM rin_memory
            WHERE {where_clause}
            ORDER BY created_at DESC
            LIMIT :limit OFFSET :offset
        """)
    else:
        sql = text(f"""
            SELECT * FROM (
                SELECT DISTINCT ON (key) id, key, content, created_at
                FROM rin_memory
                WHERE {where_clause}
                ORDER BY key, created_at DESC
            ) sub
            ORDER BY created_at DESC
            LIMIT :limit OFFSET :offset
        """)

    rows = db.execute(sql, params).all()
    return {
        "items": [
            {"id": r.id, "key": r.key, "content": _maybe_trim(r.content), "created_at": format_datetime(r.created_at)}
            for r in rows
        ],
        "page": page,
        "limit": limit,
    }


@router.post("/rin/memory")
async def write_memory(body: RinMemoryWrite, db: Session = Depends(get_db)):
    """Write a Rin memory. Same key appends a new version."""
    mem = RinMemory(key=body.key, content=body.content)
    db.add(mem)
    db.commit()
    db.refresh(mem)
    return {"id": mem.id, "key": mem.key, "created_at": format_datetime(mem.created_at)}


@router.delete("/rin/memory/{memory_id}")
async def delete_memory(memory_id: int, db: Session = Depends(get_db)):
    """Delete a single Rin memory entry."""
    mem = db.query(RinMemory).filter(RinMemory.id == memory_id).first()
    if not mem:
        raise HTTPException(status_code=404, detail="Memory not found")
    db.delete(mem)
    db.commit()
    return {"status": "deleted", "id": memory_id}


@router.post("/rin/chat")
async def save_chat(body: RinChatWrite, db: Session = Depends(get_db)):
    """Save a Rin chat message."""
    msg = RinChat(role=body.role, content=body.content)
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return {"id": msg.id, "role": msg.role, "created_at": format_datetime(msg.created_at)}


@router.delete("/rin/chat/{chat_id}")
async def delete_chat(chat_id: int, db: Session = Depends(get_db)):
    """Delete a single Rin chat message."""
    msg = db.query(RinChat).filter(RinChat.id == chat_id).first()
    if not msg:
        raise HTTPException(status_code=404, detail="Chat message not found")
    db.delete(msg)
    db.commit()
    return {"status": "deleted", "id": chat_id}


@router.delete("/rin/chat")
async def delete_chat_batch(ids: list[int], db: Session = Depends(get_db)):
    """Batch delete Rin chat messages."""
    deleted = db.query(RinChat).filter(RinChat.id.in_(ids)).delete(synchronize_session=False)
    db.commit()
    return {"status": "deleted", "count": deleted}


@router.get("/rin/chat")
async def read_chat(
    limit: int = Query(30, ge=1, le=100),
    before_id: int | None = None,
    db: Session = Depends(get_db),
):
    """Read recent Rin chat messages."""
    query = db.query(RinChat)
    if before_id:
        query = query.filter(RinChat.id < before_id)
    items = query.order_by(RinChat.created_at.desc()).limit(limit).all()
    items.reverse()
    return {
        "items": [
            {"id": m.id, "role": m.role, "content": m.content, "created_at": format_datetime(m.created_at)}
            for m in items
        ],
    }


class AichengSearchRequest(BaseModel):
    action: str
    arguments: dict


@router.post("/rin/aicheng-search")
async def aicheng_search(body: AichengSearchRequest, db: Session = Depends(get_db)):
    """Proxy for Rin to search 助手A's memories/summaries/chat read-only."""
    ms = MemoryService(db)
    action = body.action
    args = body.arguments

    if action == "search_memory":
        payload = {"query": args.get("query", "")}
        if args.get("action") == "related":
            payload["action"] = "related"
            if args.get("memory_id"):
                payload["memory_id"] = args["memory_id"]
            return ms.related_memory(payload)
        if args.get("source"):
            payload["source"] = args["source"]
        return ms.search_memory(payload)
    if action == "search_summary":
        payload = {"query": args.get("query", ""), "limit": args.get("limit", 5)}
        if args.get("offset"):
            payload["offset"] = args["offset"]
        if args.get("start_time"):
            payload["start_time"] = args["start_time"]
        if args.get("end_time"):
            payload["end_time"] = args["end_time"]
        return ms.search_summary(payload)
    if action == "search_chat_history":
        payload = {}
        for k in ("query", "msg_id_start", "msg_id_end", "message_id", "offset"):
            if args.get(k) is not None:
                payload[k] = args[k]
        return ms.search_chat_history(payload)
    return {"error": f"unknown action: {action}"}
