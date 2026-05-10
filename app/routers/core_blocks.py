from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import SessionLocal, get_db
from app.models.models import CoreBlock, CoreBlockCandidate, CoreBlockHistory
from app.services.core_blocks_updater import CoreBlocksUpdater
from app.utils import format_datetime, TZ_EAST8

logger = logging.getLogger(__name__)
router = APIRouter()


class CoreBlockItem(BaseModel):
    id: int
    block_type: str
    assistant_id: int | None
    content: str
    version: int
    updated_at: str | None
    created_at: str | None


class CoreBlocksResponse(BaseModel):
    blocks: list[CoreBlockItem]


class CoreBlockUpdateRequest(BaseModel):
    content: str


class CoreBlockHistoryItem(BaseModel):
    id: int
    core_block_id: int
    block_type: str
    assistant_id: int | None
    content: str
    version: int
    created_at: str | None


class CoreBlockHistoryResponse(BaseModel):
    history: list[CoreBlockHistoryItem]


class CoreBlockCandidateItem(BaseModel):
    id: int
    block_type: str
    assistant_id: int | None
    content: str
    source_summary_id: int | None
    status: str
    occurrence_count: int
    created_at: str | None


class CoreBlockCandidatesResponse(BaseModel):
    candidates: list[CoreBlockCandidateItem]
    total: int


class CoreBlockRewriteRequest(BaseModel):
    assistant_id: int | None = None


class CoreBlockRewriteResponse(BaseModel):
    rewritten_blocks: int
    processed_candidates: int


@router.get("/core-blocks/candidates", response_model=CoreBlockCandidatesResponse)
def list_core_block_candidates(
    status: str | None = Query(None),
    block_type: str | None = Query(None),
    assistant_id: int | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> CoreBlockCandidatesResponse:
    query = db.query(CoreBlockCandidate)
    if status:
        query = query.filter(CoreBlockCandidate.status == status)
    if block_type:
        query = query.filter(CoreBlockCandidate.block_type == block_type)
    if assistant_id is not None:
        query = query.filter(CoreBlockCandidate.assistant_id == assistant_id)

    total = query.count()
    rows = (
        query.order_by(CoreBlockCandidate.created_at.desc(), CoreBlockCandidate.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    items = [
        CoreBlockCandidateItem(
            id=row.id,
            block_type=row.block_type,
            assistant_id=row.assistant_id,
            content=row.content,
            source_summary_id=row.source_summary_id,
            status=row.status,
            occurrence_count=row.occurrence_count,
            created_at=format_datetime(row.created_at),
        )
        for row in rows
    ]
    return CoreBlockCandidatesResponse(candidates=items, total=total)


@router.post("/core-blocks/rewrite", response_model=CoreBlockRewriteResponse)
def rewrite_core_blocks(
    payload: CoreBlockRewriteRequest | None = None,
) -> CoreBlockRewriteResponse:
    updater = CoreBlocksUpdater(SessionLocal)
    assistant_id = payload.assistant_id if payload else None
    result = updater.rewrite_adopted_candidates(assistant_id)
    return CoreBlockRewriteResponse(**result)


@router.get("/core-blocks", response_model=CoreBlocksResponse)
def list_core_blocks(
    block_type: str | None = Query(None),
    assistant_id: int | None = Query(None),
    db: Session = Depends(get_db),
) -> CoreBlocksResponse:
    query = db.query(CoreBlock)
    if block_type:
        query = query.filter(CoreBlock.block_type == block_type)
    if assistant_id is not None:
        query = query.filter(CoreBlock.assistant_id == assistant_id)

    rows = query.order_by(CoreBlock.block_type.asc(), CoreBlock.id.asc()).all()
    items = [
        CoreBlockItem(
            id=row.id,
            block_type=row.block_type,
            assistant_id=row.assistant_id,
            content=row.content,
            version=row.version,
            updated_at=format_datetime(row.updated_at),
            created_at=format_datetime(row.created_at),
        )
        for row in rows
    ]
    return CoreBlocksResponse(blocks=items)


class CoreBlockCreateRequest(BaseModel):
    block_type: str
    assistant_id: int | None = None
    content: str = ""


@router.post("/core-blocks", response_model=CoreBlockItem)
def create_core_block(
    payload: CoreBlockCreateRequest,
    db: Session = Depends(get_db),
) -> CoreBlockItem:
    now = datetime.now(TZ_EAST8)
    row = CoreBlock(
        block_type=payload.block_type,
        assistant_id=payload.assistant_id,
        content=payload.content,
        version=1,
        updated_at=now,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return CoreBlockItem(
        id=row.id,
        block_type=row.block_type,
        assistant_id=row.assistant_id,
        content=row.content,
        version=row.version,
        updated_at=format_datetime(row.updated_at),
        created_at=format_datetime(row.created_at),
    )


@router.put("/core-blocks/{block_id}", response_model=CoreBlockItem)
def update_core_block(
    block_id: int,
    payload: CoreBlockUpdateRequest,
    db: Session = Depends(get_db),
) -> CoreBlockItem:
    row = db.query(CoreBlock).filter(CoreBlock.id == block_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Core block not found")

    # Only create history + bump version when content actually changed
    if payload.content != row.content:
        history = CoreBlockHistory(
            core_block_id=row.id,
            block_type=row.block_type,
            assistant_id=row.assistant_id,
            content=row.content,
            version=row.version,
        )
        db.add(history)
        row.content = payload.content
        row.version += 1
    row.updated_at = datetime.now(TZ_EAST8)
    db.commit()
    db.refresh(row)
    return CoreBlockItem(
        id=row.id,
        block_type=row.block_type,
        assistant_id=row.assistant_id,
        content=row.content,
        version=row.version,
        updated_at=format_datetime(row.updated_at),
        created_at=format_datetime(row.created_at),
    )


@router.get("/core-blocks/{block_id}/history", response_model=CoreBlockHistoryResponse)
def get_core_block_history(
    block_id: int,
    db: Session = Depends(get_db),
) -> CoreBlockHistoryResponse:
    exists = db.query(CoreBlock).filter(CoreBlock.id == block_id).first()
    if not exists:
        raise HTTPException(status_code=404, detail="Core block not found")

    rows = (
        db.query(CoreBlockHistory)
        .filter(CoreBlockHistory.core_block_id == block_id)
        .order_by(CoreBlockHistory.version.desc(), CoreBlockHistory.id.desc())
        .all()
    )
    history = [
        CoreBlockHistoryItem(
            id=row.id,
            core_block_id=row.core_block_id,
            block_type=row.block_type,
            assistant_id=row.assistant_id,
            content=row.content,
            version=row.version,
            created_at=format_datetime(row.created_at),
        )
        for row in rows
    ]
    return CoreBlockHistoryResponse(history=history)


@router.delete("/core-blocks/history/{history_id}")
def delete_core_block_history(
    history_id: int,
    db: Session = Depends(get_db),
):
    row = db.query(CoreBlockHistory).filter(CoreBlockHistory.id == history_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="History entry not found")
    db.delete(row)
    db.commit()
    return {"success": True}


# ── Candidate update / delete ──────────────────────────────────────────────

class CandidateUpdateRequest(BaseModel):
    content: str | None = None
    status: str | None = None


@router.put("/core-blocks/candidates/{candidate_id}")
def update_candidate(
    candidate_id: int,
    payload: CandidateUpdateRequest,
    db: Session = Depends(get_db),
):
    row = db.query(CoreBlockCandidate).filter(CoreBlockCandidate.id == candidate_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Candidate not found")
    if payload.content is not None:
        row.content = payload.content
    if payload.status is not None:
        row.status = payload.status
    db.commit()
    return {"success": True}


@router.delete("/core-blocks/candidates/{candidate_id}")
def delete_candidate(
    candidate_id: int,
    db: Session = Depends(get_db),
):
    row = db.query(CoreBlockCandidate).filter(CoreBlockCandidate.id == candidate_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Candidate not found")
    db.delete(row)
    db.commit()
    return {"success": True}
