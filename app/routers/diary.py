from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import Diary
from app.utils import format_datetime, TZ_EAST8

logger = logging.getLogger(__name__)
router = APIRouter()


class DiaryItem(BaseModel):
    id: int
    assistant_id: int | None
    author: str
    title: str
    content: str
    is_read: bool
    unlock_at: str | None
    deleted_at: str | None
    created_at: str | None


class DiaryListResponse(BaseModel):
    diary: list[DiaryItem]
    total: int


class DiaryCreateRequest(BaseModel):
    assistant_id: int | None = None
    author: str = "user"
    title: str = ""
    content: str
    unlock_at: str | None = None  # ISO datetime


class DiaryDeleteResponse(BaseModel):
    status: str
    id: int


class DiaryReadResponse(BaseModel):
    status: str
    id: int


class UnreadCountResponse(BaseModel):
    count: int


def _row_to_item(row: Diary) -> DiaryItem:
    return DiaryItem(
        id=row.id,
        assistant_id=row.assistant_id,
        author=row.author or "assistant",
        title=row.title,
        content=row.content,
        is_read=row.is_read,
        unlock_at=format_datetime(row.unlock_at) if hasattr(row, "unlock_at") and row.unlock_at else None,
        deleted_at=format_datetime(row.deleted_at) if hasattr(row, "deleted_at") and row.deleted_at else None,
        created_at=format_datetime(row.created_at),
    )


@router.get("/diary", response_model=DiaryListResponse)
def list_diary(
    assistant_id: int | None = Query(None),
    author: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> DiaryListResponse:
    query = db.query(Diary).filter(Diary.deleted_at.is_(None))
    if assistant_id is not None:
        query = query.filter(Diary.assistant_id == assistant_id)
    if author is not None:
        query = query.filter(Diary.author == author)
    total = query.count()
    rows = query.order_by(Diary.created_at.desc(), Diary.id.desc()).offset(offset).limit(limit).all()
    return DiaryListResponse(diary=[_row_to_item(r) for r in rows], total=total)


@router.get("/diary/unread-count", response_model=UnreadCountResponse)
def unread_count(db: Session = Depends(get_db)) -> UnreadCountResponse:
    now = datetime.now(TZ_EAST8)
    count = (
        db.query(Diary)
        .filter(
            Diary.deleted_at.is_(None),
            Diary.author == "assistant",
            Diary.is_read == False,
        )
        .filter(
            (Diary.unlock_at.is_(None)) | (Diary.unlock_at <= now)
        )
        .count()
    )
    return UnreadCountResponse(count=count)


@router.get("/diary/{diary_id}", response_model=DiaryItem)
def get_diary(diary_id: int, db: Session = Depends(get_db)) -> DiaryItem:
    row = db.query(Diary).filter(Diary.id == diary_id, Diary.deleted_at.is_(None)).first()
    if not row:
        raise HTTPException(status_code=404, detail="Diary not found")
    return _row_to_item(row)


@router.post("/diary", response_model=DiaryItem)
def create_diary(
    payload: DiaryCreateRequest,
    db: Session = Depends(get_db),
) -> DiaryItem:
    unlock = None
    if payload.unlock_at:
        try:
            unlock = datetime.fromisoformat(payload.unlock_at.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid unlock_at format")

    row = Diary(
        assistant_id=payload.assistant_id,
        author=payload.author or "user",
        title=payload.title or "",
        content=payload.content,
        is_read=False,
        unlock_at=unlock,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _row_to_item(row)


@router.post("/diary/{diary_id}/read", response_model=DiaryReadResponse)
def mark_diary_read(diary_id: int, db: Session = Depends(get_db)) -> DiaryReadResponse:
    row = db.query(Diary).filter(Diary.id == diary_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Diary not found")
    row.is_read = True
    db.commit()
    return DiaryReadResponse(status="ok", id=diary_id)


@router.delete("/diary/{diary_id}", response_model=DiaryDeleteResponse)
def delete_diary(diary_id: int, db: Session = Depends(get_db)) -> DiaryDeleteResponse:
    row = db.query(Diary).filter(Diary.id == diary_id, Diary.deleted_at.is_(None)).first()
    if not row:
        raise HTTPException(status_code=404, detail="Diary not found")
    row.deleted_at = datetime.now(TZ_EAST8)
    db.commit()
    return DiaryDeleteResponse(status="deleted", id=diary_id)
