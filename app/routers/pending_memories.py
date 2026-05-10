from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import Memory, PendingMemory
from app.utils import TZ_EAST8

logger = logging.getLogger(__name__)
router = APIRouter()


class RelatedMemoryInfo(BaseModel):
    id: int
    content: str
    klass: str | None
    tags: dict | None
    disclosure: str | None
    similarity: float | None


class PendingMemoryItem(BaseModel):
    id: int  # real Memory id
    pending_id: int  # PendingMemory id
    content: str
    klass: str
    importance: float
    tags: dict | None
    disclosure: str | None
    related_memory_id: int | None
    related_memory_content: str | None
    related_memory_klass: str | None
    related_memory_tags: dict | None
    similarity: float | None
    related_memories: list[RelatedMemoryInfo] = []
    status: str
    summary_id: int | None
    created_at: str | None


class PendingMemoriesResponse(BaseModel):
    items: list[PendingMemoryItem]
    total: int


class ConfirmRequest(BaseModel):
    ids: list[int]  # Memory ids


class DismissRequest(BaseModel):
    ids: list[int]  # Memory ids


class EditPendingRequest(BaseModel):
    content: str | None = None
    klass: str | None = None
    tags: dict | None = None
    disclosure: str | None = None


class UpdateExistingRequest(BaseModel):
    pending_id: int  # Memory id of pending entry
    target_memory_id: int  # Memory id to overwrite


def _get_memory_for_pending(pm: PendingMemory, db: Session) -> Memory | None:
    """Get the real Memory entry linked to a PendingMemory.
    For legacy entries (no memory_id), migrate by creating a real Memory."""
    if pm.memory_id:
        return db.get(Memory, pm.memory_id)

    # Legacy migration: create a real Memory from old PendingMemory data
    from app.constants import KLASS_DEFAULTS
    klass_config = KLASS_DEFAULTS.get(pm.klass, KLASS_DEFAULTS["other"])
    memory = Memory(
        content=pm.content,
        klass=pm.klass,
        tags=pm.tags or {},
        embedding=pm.embedding,
        source="auto_extract",
        importance=klass_config["importance"],
        halflife_days=klass_config["halflife_days"],
        is_pending=True,
        created_at=pm.created_at,
    )
    db.add(memory)
    db.flush()  # get memory.id
    pm.memory_id = memory.id
    return memory


@router.get("/pending-memories", response_model=PendingMemoriesResponse)
def list_pending_memories(db: Session = Depends(get_db)):
    """List all pending memories with re-checked related memories."""
    rows = (
        db.query(PendingMemory)
        .filter(PendingMemory.status == "pending")
        .order_by(PendingMemory.created_at.desc())
        .all()
    )

    items: list[PendingMemoryItem] = []
    for row in rows:
        memory = _get_memory_for_pending(row, db)
        if not memory or memory.deleted_at:
            # Memory was deleted externally — auto-resolve
            row.status = "auto_resolved"
            row.resolved_at = datetime.now(TZ_EAST8)
            continue

        # Re-check: find top 2 similar confirmed memories
        related_memories: list[RelatedMemoryInfo] = []
        if memory.embedding is not None:
            emb_str = str([float(x) for x in memory.embedding])
            dup_sql = text("""
                SELECT id, content, klass, tags, disclosure,
                       1 - (embedding <=> :query_embedding) AS similarity
                FROM memories
                WHERE embedding IS NOT NULL AND deleted_at IS NULL
                  AND is_pending = FALSE
                ORDER BY embedding <=> :query_embedding
                LIMIT 2
            """)
            dups = db.execute(dup_sql, {"query_embedding": emb_str}).fetchall()
            top = dups[0] if dups else None
            if top and top.similarity > 0.88:
                # Auto-resolve: an equivalent confirmed memory now exists
                row.status = "auto_resolved"
                row.related_memory_id = top.id
                row.similarity = round(top.similarity, 3)
                row.resolved_at = datetime.now(TZ_EAST8)
                memory.deleted_at = datetime.now(TZ_EAST8)
                memory.is_pending = False
                db.commit()
                continue
            for d in dups:
                if d.similarity > 0.4:
                    related_memories.append(RelatedMemoryInfo(
                        id=d.id,
                        content=d.content,
                        klass=d.klass,
                        tags=d.tags if isinstance(d.tags, dict) else None,
                        disclosure=d.disclosure,
                        similarity=round(d.similarity, 3),
                    ))
            # Keep backward compat fields (top match)
            if related_memories:
                row.related_memory_id = related_memories[0].id
                row.similarity = related_memories[0].similarity
            elif row.related_memory_id:
                existing = db.get(Memory, row.related_memory_id)
                if not existing or existing.deleted_at:
                    row.related_memory_id = None
                    row.similarity = None

        # Legacy: populate related_memory fields from top match
        related_content = None
        related_klass = None
        related_tags = None
        if related_memories:
            related_content = related_memories[0].content
            related_klass = related_memories[0].klass
            related_tags = related_memories[0].tags
        elif row.related_memory_id:
            related = db.get(Memory, row.related_memory_id)
            if related:
                related_content = related.content
                related_klass = related.klass
                related_tags = related.tags

        items.append(PendingMemoryItem(
            id=memory.id,
            pending_id=row.id,
            content=memory.content,
            klass=memory.klass,
            importance=memory.importance,
            tags=memory.tags,
            disclosure=memory.disclosure,
            related_memory_id=row.related_memory_id,
            related_memory_content=related_content,
            related_memory_klass=related_klass,
            related_memory_tags=related_tags,
            similarity=row.similarity,
            related_memories=related_memories,
            status=row.status,
            summary_id=row.summary_id,
            created_at=memory.created_at.isoformat() if memory.created_at else None,
        ))

    db.commit()  # persist any updated related_memory_id/similarity
    return PendingMemoriesResponse(items=items, total=len(items))


@router.get("/pending-memories/count")
def pending_memory_count(db: Session = Depends(get_db)):
    """Quick count for badge display."""
    count = db.query(PendingMemory).filter(PendingMemory.status == "pending").count()
    return {"count": count}


@router.post("/pending-memories/confirm")
def confirm_pending_memories(req: ConfirmRequest, db: Session = Depends(get_db)):
    """Confirm pending memories — just flip is_pending to False."""
    saved = 0
    skipped = 0
    for memory_id in req.ids:
        # Find the PendingMemory by memory_id
        pm = (
            db.query(PendingMemory)
            .filter(PendingMemory.memory_id == memory_id, PendingMemory.status == "pending")
            .first()
        )
        if not pm:
            skipped += 1
            continue

        memory = db.get(Memory, memory_id)
        if not memory or memory.deleted_at:
            skipped += 1
            continue

        # Final dedup check against confirmed memories
        if memory.embedding is not None:
            emb_str = str([float(x) for x in memory.embedding])
            dup_sql = text("""
                SELECT id FROM memories
                WHERE embedding IS NOT NULL AND deleted_at IS NULL
                  AND is_pending = FALSE
                  AND 1 - (embedding <=> :query_embedding) > 0.88
                LIMIT 1
            """)
            dup = db.execute(dup_sql, {"query_embedding": emb_str}).first()
            if dup:
                pm.status = "auto_resolved"
                pm.resolved_at = datetime.now(TZ_EAST8)
                memory.deleted_at = datetime.now(TZ_EAST8)
                memory.is_pending = False
                skipped += 1
                continue

        # Confirm: just flip the flag
        memory.is_pending = False
        pm.status = "confirmed"
        pm.resolved_at = datetime.now(TZ_EAST8)
        saved += 1

    db.commit()
    return {"saved": saved, "skipped": skipped}


@router.post("/pending-memories/dismiss")
def dismiss_pending_memories(req: DismissRequest, db: Session = Depends(get_db)):
    """Dismiss pending memories — soft-delete the Memory entry."""
    dismissed = 0
    for memory_id in req.ids:
        pm = (
            db.query(PendingMemory)
            .filter(PendingMemory.memory_id == memory_id, PendingMemory.status == "pending")
            .first()
        )
        if not pm:
            continue

        memory = db.get(Memory, memory_id)
        if memory:
            memory.deleted_at = datetime.now(TZ_EAST8)
            memory.is_pending = False

        pm.status = "dismissed"
        pm.resolved_at = datetime.now(TZ_EAST8)
        dismissed += 1

    db.commit()
    return {"dismissed": dismissed}


@router.patch("/pending-memories/{memory_id}")
def edit_pending_memory(memory_id: int, req: EditPendingRequest, db: Session = Depends(get_db)):
    """Edit a pending memory's content, klass, or tags (edits the real Memory)."""
    from app.services.embedding_service import EmbeddingService

    memory = db.get(Memory, memory_id)
    if not memory or not memory.is_pending or memory.deleted_at:
        raise HTTPException(status_code=404, detail="Pending memory not found")

    if req.content is not None and req.content.strip():
        memory.content = req.content.strip()
        embedding_service = EmbeddingService()
        new_emb = embedding_service.get_embedding(memory.content)
        if new_emb is not None:
            memory.embedding = new_emb
    if req.klass is not None:
        memory.klass = req.klass
    if req.tags is not None:
        memory.tags = req.tags
    if req.disclosure is not None:
        memory.disclosure = req.disclosure.strip() or None

    memory.updated_at = datetime.now(TZ_EAST8)
    db.commit()
    return {"id": memory.id, "content": memory.content, "klass": memory.klass, "tags": memory.tags, "disclosure": memory.disclosure}


@router.post("/pending-memories/update-existing")
def update_existing_memory(req: UpdateExistingRequest, db: Session = Depends(get_db)):
    """Overwrite an existing memory with a pending memory's content."""
    # pending_id here is the Memory id of the pending entry
    pending_memory = db.get(Memory, req.pending_id)
    if not pending_memory or not pending_memory.is_pending or pending_memory.deleted_at:
        raise HTTPException(status_code=404, detail="Pending memory not found")

    pm = (
        db.query(PendingMemory)
        .filter(PendingMemory.memory_id == req.pending_id, PendingMemory.status == "pending")
        .first()
    )

    target = db.get(Memory, req.target_memory_id)
    if not target or target.deleted_at:
        raise HTTPException(status_code=404, detail="Target memory not found")

    # Overwrite target with pending memory's content
    target.content = pending_memory.content
    target.embedding = pending_memory.embedding
    target.klass = pending_memory.klass
    target.tags = pending_memory.tags or target.tags
    target.updated_at = datetime.now(TZ_EAST8)

    # Soft-delete the pending Memory entry (content now lives in target)
    pending_memory.deleted_at = datetime.now(TZ_EAST8)
    pending_memory.is_pending = False

    if pm:
        pm.status = "confirmed"
        pm.resolved_at = datetime.now(TZ_EAST8)

    db.commit()
    return {"updated_memory_id": target.id, "content": target.content}
