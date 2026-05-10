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
    read_at: str | None
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


class DiaryUpdateRequest(BaseModel):
    title: str | None = None
    content: str | None = None
    unlock_at: str | None = None  # ISO datetime, or "" to clear


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
        read_at=format_datetime(row.read_at) if hasattr(row, "read_at") and row.read_at else None,
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

    # Wake up proactive loop for immediate user diaries
    if (payload.author or "user") == "user" and not unlock:
        try:
            from app.services.proactive_service import _notify_wakeup
            _notify_wakeup()
        except Exception:
            pass

    return _row_to_item(row)


@router.put("/diary/{diary_id}", response_model=DiaryItem)
def update_diary(
    diary_id: int,
    payload: DiaryUpdateRequest,
    db: Session = Depends(get_db),
) -> DiaryItem:
    row = db.query(Diary).filter(Diary.id == diary_id, Diary.deleted_at.is_(None)).first()
    if not row:
        raise HTTPException(status_code=404, detail="Diary not found")
    if row.author != "user":
        raise HTTPException(status_code=403, detail="只能编辑自己写的日记")
    now = datetime.now(TZ_EAST8)
    if row.unlock_at and row.unlock_at <= now:
        raise HTTPException(status_code=403, detail="已解锁的日记不能编辑")
    if not row.unlock_at and row.notified_at:
        raise HTTPException(status_code=403, detail="已发出的日记不能编辑")

    if payload.title is not None:
        row.title = payload.title
    if payload.content is not None:
        row.content = payload.content
    if payload.unlock_at is not None:
        if payload.unlock_at == "":
            row.unlock_at = None
        else:
            try:
                row.unlock_at = datetime.fromisoformat(payload.unlock_at.replace("Z", "+00:00"))
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid unlock_at format")
    db.commit()
    db.refresh(row)
    return _row_to_item(row)


@router.post("/diary/{diary_id}/read", response_model=DiaryReadResponse)
def mark_diary_read(diary_id: int, db: Session = Depends(get_db)) -> DiaryReadResponse:
    row = db.query(Diary).filter(Diary.id == diary_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Diary not found")
    from datetime import datetime, timezone, timedelta
    row.is_read = True
    row.read_at = datetime.now(timezone(timedelta(hours=8)))
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
