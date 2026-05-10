from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import Memory
from app.utils import format_datetime, TZ_EAST8

logger = logging.getLogger(__name__)
router = APIRouter()


class MemoryItem(BaseModel):
    id: int
    content: str
    tags: dict[str, Any]
    source: str
    klass: str
    importance: float
    manual_boost: float
    hits: int
    disclosure: str | None = None
    last_access_ts: str | None
    updated_at: str | None = None
    created_at: str | None
    decayed_score: float


class MemoriesResponse(BaseModel):
    memories: list[MemoryItem]
    total: int


class MemoryUpdateRequest(BaseModel):
    content: str | None = None
    manual_boost: float | None = None
    klass: str | None = None
    tags: dict | None = None
    disclosure: str | None = None


class MemoryDeleteResponse(BaseModel):
    status: str
    id: int


class TrashMemoryItem(BaseModel):
    id: int
    content: str
    source: str
    klass: str
    deleted_at: str | None
    created_at: str | None


class TrashMemoriesResponse(BaseModel):
    memories: list[TrashMemoryItem]
    total: int


def _to_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=TZ_EAST8)
    return value.astimezone(timezone.utc)


def _compute_decayed_score(memory: Memory) -> float:
    now_utc = datetime.now(TZ_EAST8)
    created_at = _to_utc(memory.created_at)
    last_access_ts = _to_utc(memory.last_access_ts)
    if created_at is None:
        age_days = 0.0
    else:
        age_base = last_access_ts or created_at
        age_days = max(0.0, (now_utc - age_base).total_seconds() / 86400.0)
    base = min(max((memory.importance or 0.5) + (memory.manual_boost or 0.0), 0.0), 1.0)
    halflife = memory.halflife_days or 60.0
    boost = 1 + 0.35 * math.log(1 + (memory.hits or 0))
    return base * math.exp(-math.log(2) / halflife * age_days) * boost


@router.get("/memories", response_model=MemoriesResponse)
def list_memories(
    klass: str | None = Query(None),
    source: str | None = Query(None),
    search: str | None = Query(None, min_length=1),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> MemoriesResponse:
    query = db.query(Memory).filter(Memory.deleted_at.is_(None), Memory.is_pending.is_(False))
    if klass:
        query = query.filter(Memory.klass == klass)
    if source:
        query = query.filter(Memory.source == source)
    if search:
        from sqlalchemy import or_
        conditions = [Memory.content.ilike(f"%{search}%")]
        if search.isdigit():
            conditions.append(Memory.id == int(search))
        query = query.filter(or_(*conditions))

    total = query.count()
    rows = (
        query.order_by(Memory.created_at.desc(), Memory.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    items = [
        MemoryItem(
            id=row.id,
            content=row.content,
            tags=row.tags or {},
            source=row.source,
            klass=row.klass,
            importance=row.importance,
            manual_boost=row.manual_boost,
            hits=row.hits,
            disclosure=row.disclosure,
            last_access_ts=format_datetime(row.last_access_ts),
            updated_at=format_datetime(row.updated_at),
            created_at=format_datetime(row.created_at),
            decayed_score=_compute_decayed_score(row),
        )
        for row in rows
    ]
    return MemoriesResponse(memories=items, total=total)


@router.put("/memories/{memory_id}", response_model=MemoryItem)
def update_memory(
    memory_id: int,
    payload: MemoryUpdateRequest,
    db: Session = Depends(get_db),
) -> MemoryItem:
    memory = (
        db.query(Memory)
        .filter(Memory.id == memory_id, Memory.deleted_at.is_(None), Memory.is_pending.is_(False))
        .first()
    )
    if not memory:
        raise HTTPException(status_code=404, detail="Memory not found")

    if hasattr(payload, "model_dump"):
        update_data = payload.model_dump(exclude_unset=True)
    else:
        update_data = payload.dict(exclude_unset=True)

    # Snapshot before overwrite
    from app.models.models import MemoryVersion
    db.add(MemoryVersion(
        memory_id=memory.id,
        content=memory.content,
        klass=memory.klass,
        tags=memory.tags,
        disclosure=memory.disclosure,
        changed_by="admin",
    ))

    content_changed = False
    if "content" in update_data:
        memory.content = update_data["content"] or ""
        content_changed = True
    if "manual_boost" in update_data:
        memory.manual_boost = update_data["manual_boost"] or 0.0
    if "klass" in update_data:
        memory.klass = update_data["klass"] or "other"
    if "tags" in update_data:
        memory.tags = update_data["tags"] or {}
    if "disclosure" in update_data:
        memory.disclosure = update_data.get("disclosure") or None
        content_changed = True
    memory.updated_at = datetime.now(TZ_EAST8)

    # Re-generate embedding if content or disclosure changed
    if content_changed:
        try:
            from app.services.embedding_service import EmbeddingService
            embed_text = memory.content
            if memory.disclosure:
                embed_text = f"{memory.disclosure}\n{embed_text}"
            emb = EmbeddingService().get_embedding(embed_text)
            if emb:
                memory.embedding = emb
        except Exception:
            logger.warning("Failed to re-embed memory %d", memory_id, exc_info=True)

    db.commit()
    db.refresh(memory)
    return MemoryItem(
        id=memory.id,
        content=memory.content,
        tags=memory.tags or {},
        source=memory.source,
        klass=memory.klass,
        importance=memory.importance,
        manual_boost=memory.manual_boost,
        hits=memory.hits,
        disclosure=memory.disclosure,
        last_access_ts=format_datetime(memory.last_access_ts),
        created_at=format_datetime(memory.created_at),
        decayed_score=_compute_decayed_score(memory),
    )


@router.get("/memories/trash", response_model=TrashMemoriesResponse)
def list_memory_trash(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> TrashMemoriesResponse:
    query = db.query(Memory).filter(Memory.deleted_at.is_not(None))
    total = query.count()
    rows = (
        query.order_by(Memory.deleted_at.desc(), Memory.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    items = [
        TrashMemoryItem(
            id=row.id,
            content=row.content,
            source=row.source,
            klass=row.klass,
            deleted_at=format_datetime(row.deleted_at),
            created_at=format_datetime(row.created_at),
        )
        for row in rows
    ]
    return TrashMemoriesResponse(memories=items, total=total)


@router.get("/memories/{memory_id}/position")
def get_memory_position(memory_id: int, db: Session = Depends(get_db)):
    row = db.get(Memory, memory_id)
    if not row or row.deleted_at:
        raise HTTPException(status_code=404, detail="Memory not found")
    # Count how many memories appear before this one in default sort (created_at desc)
    position = db.query(Memory).filter(
        Memory.deleted_at.is_(None),
        Memory.is_pending.is_(False),
        (Memory.created_at > row.created_at) | (
            (Memory.created_at == row.created_at) & (Memory.id > row.id)
        ),
    ).count()
    return {"position": position, "id": memory_id}


@router.get("/memories/{memory_id}", response_model=MemoryItem)
def get_memory(memory_id: int, db: Session = Depends(get_db)) -> MemoryItem:
    row = db.get(Memory, memory_id)
    if not row or row.deleted_at:
        raise HTTPException(status_code=404, detail="Memory not found")
    return MemoryItem(
        id=row.id,
        content=row.content,
        tags=row.tags or {},
        source=row.source,
        klass=row.klass,
        importance=row.importance,
        manual_boost=row.manual_boost,
        hits=row.hits,
        disclosure=row.disclosure,
        last_access_ts=format_datetime(row.last_access_ts),
        updated_at=format_datetime(row.updated_at),
        created_at=format_datetime(row.created_at),
        decayed_score=_compute_decayed_score(row),
    )


@router.post("/memories/{memory_id}/restore", response_model=MemoryDeleteResponse)
def restore_memory(memory_id: int, db: Session = Depends(get_db)) -> MemoryDeleteResponse:
    memory = db.query(Memory).filter(Memory.id == memory_id).first()
    if not memory:
        raise HTTPException(status_code=404, detail="Memory not found")
    if memory.deleted_at is None:
        raise HTTPException(status_code=400, detail="Memory is not deleted")
    memory.deleted_at = None
    db.commit()
    return MemoryDeleteResponse(status="restored", id=memory_id)


class MemoryVersionItem(BaseModel):
    id: int
    content: str
    klass: str | None
    tags: dict | None
    disclosure: str | None
    changed_by: str
    created_at: str | None


@router.get("/memories/{memory_id}/versions")
def get_memory_versions(
    memory_id: int,
    db: Session = Depends(get_db),
) -> list[MemoryVersionItem]:
    from app.models.models import MemoryVersion
    versions = (
        db.query(MemoryVersion)
        .filter(MemoryVersion.memory_id == memory_id)
        .order_by(MemoryVersion.created_at.desc())
        .limit(50)
        .all()
    )
    return [
        MemoryVersionItem(
            id=v.id,
            content=v.content,
            klass=v.klass,
            tags=v.tags,
            disclosure=v.disclosure,
            changed_by=v.changed_by,
            created_at=format_datetime(v.created_at),
        )
        for v in versions
    ]


@router.post("/memories/{memory_id}/rollback/{version_id}")
def rollback_memory(
    memory_id: int,
    version_id: int,
    db: Session = Depends(get_db),
):
    from app.models.models import MemoryVersion
    memory = db.query(Memory).filter(Memory.id == memory_id, Memory.deleted_at.is_(None)).first()
    if not memory:
        raise HTTPException(status_code=404, detail="Memory not found")
    version = db.query(MemoryVersion).filter(
        MemoryVersion.id == version_id,
        MemoryVersion.memory_id == memory_id,
    ).first()
    if not version:
        raise HTTPException(status_code=404, detail="Version not found")
    # Snapshot current state before rollback
    db.add(MemoryVersion(
        memory_id=memory.id,
        content=memory.content,
        klass=memory.klass,
        tags=memory.tags,
        disclosure=memory.disclosure,
        changed_by="rollback",
    ))
    memory.content = version.content
    memory.klass = version.klass or memory.klass
    memory.tags = version.tags if version.tags is not None else memory.tags
    memory.disclosure = version.disclosure
    memory.updated_at = None

    # Re-generate embedding for rolled-back content
    try:
        from app.services.embedding_service import EmbeddingService
        embed_text = memory.content
        if memory.disclosure:
            embed_text = f"{memory.disclosure}\n{embed_text}"
        emb = EmbeddingService().get_embedding(embed_text)
        if emb:
            memory.embedding = emb
    except Exception:
        logger.warning("Failed to re-embed memory %d on rollback", memory_id, exc_info=True)

    db.commit()
    return {"status": "rolled_back", "memory_id": memory_id, "to_version_id": version_id}


class BatchDeleteRequest(BaseModel):
    ids: list[int]


class BatchDeleteResponse(BaseModel):
    deleted: int


@router.delete("/memories/batch", response_model=BatchDeleteResponse)
def batch_delete_memories(
    payload: BatchDeleteRequest,
    db: Session = Depends(get_db),
) -> BatchDeleteResponse:
    if not payload.ids:
        return BatchDeleteResponse(deleted=0)
    now = datetime.now(TZ_EAST8)
    deleted = db.query(Memory).filter(
        Memory.id.in_(payload.ids),
        Memory.deleted_at.is_(None),
        Memory.is_pending.is_(False),
    ).update({Memory.deleted_at: now}, synchronize_session=False)
    db.commit()
    return BatchDeleteResponse(deleted=deleted)


@router.delete("/memories/{memory_id}/permanent", response_model=MemoryDeleteResponse)
def delete_memory_permanent(
    memory_id: int, db: Session = Depends(get_db)
) -> MemoryDeleteResponse:
    memory = db.query(Memory).filter(Memory.id == memory_id).first()
    if not memory:
        raise HTTPException(status_code=404, detail="Memory not found")
    db.delete(memory)
    db.commit()
    return MemoryDeleteResponse(status="deleted_permanently", id=memory_id)


@router.delete("/memories/{memory_id}", response_model=MemoryDeleteResponse)
def delete_memory(memory_id: int, db: Session = Depends(get_db)) -> MemoryDeleteResponse:
    memory = (
        db.query(Memory)
        .filter(Memory.id == memory_id, Memory.deleted_at.is_(None), Memory.is_pending.is_(False))
        .first()
    )
    if not memory:
        raise HTTPException(status_code=404, detail="Memory not found")
    memory.deleted_at = datetime.now(TZ_EAST8)
    db.commit()
    return MemoryDeleteResponse(status="deleted", id=memory_id)
