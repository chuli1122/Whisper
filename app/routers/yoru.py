from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import YoruMemory, YoruChat
from app.services.memory_service import MemoryService
from app.utils import format_datetime

logger = logging.getLogger(__name__)
router = APIRouter()
deploy_router = APIRouter()


async def _yoru_auth(request: Request):
    """Allow localhost or requests with correct API key."""
    client = request.client.host if request.client else ""
    if client in ("127.0.0.1", "::1", "localhost"):
        return
    if client.startswith("172.") or client.startswith("192.168."):
        return
    if request.headers.get("X-Yoru-Key") == "yoru2026nightfall":
        return
    raise HTTPException(status_code=403, detail="Forbidden")


router.dependencies = [Depends(_yoru_auth)]


# ── Pydantic models ──


class YoruMemoryWrite(BaseModel):
    key: str
    content: str


class YoruChatWrite(BaseModel):
    role: str
    content: str


# ── Memory endpoints ──


@router.get("/yoru/memory")
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
    """Read yoru memories. Supports exact key, prefix, type, and time range queries.

    summary_only=True truncates content to 120 chars (list queries only, ignored for
    exact key lookup)."""
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
        # Exact key query
        query = db.query(YoruMemory).filter(YoruMemory.key == key)
        if history:
            items = query.order_by(YoruMemory.created_at.asc()).all()
        else:
            items = query.order_by(YoruMemory.created_at.desc()).limit(1).all()
        return {
            "items": [
                {"id": m.id, "key": m.key, "content": _maybe_trim(m.content), "created_at": format_datetime(m.created_at)}
                for m in items
            ],
        }

    # Build parameterized query for prefix / time range / all
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
            FROM yoru_memory
            WHERE {where_clause}
            ORDER BY created_at DESC
            LIMIT :limit OFFSET :offset
        """)
    else:
        # DISTINCT ON per key, then re-sort by created_at DESC for time-ordered results
        sql = text(f"""
            SELECT * FROM (
                SELECT DISTINCT ON (key) id, key, content, created_at
                FROM yoru_memory
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


@router.post("/yoru/memory")
async def write_memory(body: YoruMemoryWrite, db: Session = Depends(get_db)):
    """Write a yoru memory. Same key = new version (append, not overwrite)."""
    mem = YoruMemory(key=body.key, content=body.content)
    db.add(mem)
    db.commit()
    db.refresh(mem)
    return {"id": mem.id, "key": mem.key, "created_at": format_datetime(mem.created_at)}


@router.delete("/yoru/memory/{memory_id}")
async def delete_memory(memory_id: int, db: Session = Depends(get_db)):
    """Delete a single yoru memory entry."""
    mem = db.query(YoruMemory).filter(YoruMemory.id == memory_id).first()
    if not mem:
        raise HTTPException(status_code=404, detail="Memory not found")
    db.delete(mem)
    db.commit()
    return {"status": "deleted", "id": memory_id}


# ── Chat endpoints ──


@router.post("/yoru/chat")
async def save_chat(body: YoruChatWrite, db: Session = Depends(get_db)):
    """Save a chat message."""
    msg = YoruChat(role=body.role, content=body.content)
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return {"id": msg.id, "role": msg.role, "created_at": format_datetime(msg.created_at)}


@router.delete("/yoru/chat/{chat_id}")
async def delete_chat(chat_id: int, db: Session = Depends(get_db)):
    """Delete a single chat message."""
    msg = db.query(YoruChat).filter(YoruChat.id == chat_id).first()
    if not msg:
        raise HTTPException(status_code=404, detail="Chat message not found")
    db.delete(msg)
    db.commit()
    return {"status": "deleted", "id": chat_id}


@router.delete("/yoru/chat")
async def delete_chat_batch(ids: list[int], db: Session = Depends(get_db)):
    """Batch delete chat messages."""
    deleted = db.query(YoruChat).filter(YoruChat.id.in_(ids)).delete(synchronize_session=False)
    db.commit()
    return {"status": "deleted", "count": deleted}


@router.get("/yoru/chat")
async def read_chat(
    limit: int = Query(30, ge=1, le=100),
    before_id: int | None = None,
    db: Session = Depends(get_db),
):
    """Read recent chat messages. For startup injection and history browsing."""
    query = db.query(YoruChat)
    if before_id:
        query = query.filter(YoruChat.id < before_id)
    items = query.order_by(YoruChat.created_at.desc()).limit(limit).all()
    items.reverse()  # Return in chronological order
    return {
        "items": [
            {"id": m.id, "role": m.role, "content": m.content, "created_at": format_datetime(m.created_at)}
            for m in items
        ],
    }


# ── Proxy to 助手A's memory service (read-only) ──


class AichengSearchRequest(BaseModel):
    action: str  # search_memory / search_summary / search_chat_history
    arguments: dict


@router.post("/yoru/aicheng-search")
async def aicheng_search(body: AichengSearchRequest, db: Session = Depends(get_db)):
    """Proxy for 协作助手 to search 助手A's memories/summaries/chat (read-only)."""
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
    elif action == "search_summary":
        payload = {"query": args.get("query", ""), "limit": args.get("limit", 5)}
        if args.get("offset"):
            payload["offset"] = args["offset"]
        if args.get("start_time"):
            payload["start_time"] = args["start_time"]
        if args.get("end_time"):
            payload["end_time"] = args["end_time"]
        return ms.search_summary(payload)
    elif action == "search_chat_history":
        payload = {}
        for k in ("query", "msg_id_start", "msg_id_end", "message_id", "offset"):
            if args.get(k) is not None:
                payload[k] = args[k]
        return ms.search_chat_history(payload)
    else:
        return {"error": f"unknown action: {action}"}


# ── GitHub webhook auto-deploy ──


@deploy_router.post("/yoru/deploy")
async def github_deploy(request: Request):
    """GitHub webhook: pull + restart + health check, rollback on failure."""
    import subprocess
    import asyncio

    # Simple secret check via query param
    if request.query_params.get("secret") != "yoru2026":
        raise HTTPException(status_code=403, detail="Forbidden")

    _cwd = "/srv/ai-companion"
    prev_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=_cwd
    ).stdout.strip()

    # Ensure we're on main branch
    subprocess.run(["git", "checkout", "main"], capture_output=True, text=True, cwd=_cwd)

    # Pull
    pull = subprocess.run(
        ["git", "pull", "--ff-only"], capture_output=True, text=True, cwd=_cwd
    )
    if pull.returncode != 0:
        return {"status": "error", "step": "pull", "detail": pull.stderr}

    new_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=_cwd
    ).stdout.strip()

    # Restart in background: use a detached subprocess so the response can be sent
    # before the current process is killed by systemctl restart.
    # The shell script: restart → wait → health check → rollback if failed.
    _script = (
        f"sleep 1 && systemctl restart ai_companion && sleep 5 "
        f"&& curl -sf -o /dev/null http://localhost:8002/api/auth/verify "
        f"|| (cd {_cwd} && git reset --hard {prev_commit} && systemctl restart ai_companion)"
    )
    subprocess.Popen(
        ["bash", "-c", _script],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    return {"status": "ok", "commit": new_commit, "prev": prev_commit, "restarting": True}
