from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import WorldBook
from app.utils import format_datetime

logger = logging.getLogger(__name__)
router = APIRouter()

_sort_order_ensured = False


def _ensure_sort_order_column(db: Session) -> None:
    global _sort_order_ensured
    if _sort_order_ensured:
        return
    try:
        db.execute(
            text("ALTER TABLE world_books ADD COLUMN IF NOT EXISTS sort_order INTEGER NOT NULL DEFAULT 0")
        )
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
    _sort_order_ensured = True


class WorldBookItem(BaseModel):
    id: int
    name: str
    content: str
    activation: str
    keywords: list[Any] | None
    message_mode: str | None = None
    folder: str | None
    sort_order: int = 0
    created_at: str | None


class WorldBooksResponse(BaseModel):
    world_books: list[WorldBookItem]
    total: int


class WorldBookCreateRequest(BaseModel):
    name: str
    content: str = ""
    activation: str = "always"
    keywords: list[Any] | None = None
    message_mode: str | None = None
    folder: str | None = None


class WorldBookUpdateRequest(BaseModel):
    name: str | None = None
    content: str | None = None
    activation: str | None = None
    keywords: list[Any] | None = None
    message_mode: str | None = None
    folder: str | None = None


class WorldBookDeleteResponse(BaseModel):
    status: str
    id: int


class ReorderRequest(BaseModel):
    ordered_ids: list[int]


def _to_item(row: WorldBook) -> WorldBookItem:
    return WorldBookItem(
        id=row.id,
        name=row.name,
        content=row.content,
        activation=row.activation,
        keywords=row.keywords or [],
        message_mode=row.message_mode,
        folder=row.folder,
        sort_order=row.sort_order if hasattr(row, "sort_order") else 0,
        created_at=format_datetime(row.created_at),
    )


@router.get("/world-books", response_model=WorldBooksResponse)
def list_world_books(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> WorldBooksResponse:
    _ensure_sort_order_column(db)
    query = db.query(WorldBook)
    total = query.count()
    rows = (
        query.order_by(WorldBook.sort_order.asc(), WorldBook.id.asc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return WorldBooksResponse(world_books=[_to_item(r) for r in rows], total=total)


@router.post("/world-books", response_model=WorldBookItem)
def create_world_book(
    payload: WorldBookCreateRequest,
    db: Session = Depends(get_db),
) -> WorldBookItem:
    row = WorldBook(
        name=payload.name,
        content=payload.content,
        activation=payload.activation,
        keywords=payload.keywords or [],
        message_mode=payload.message_mode,
        folder=payload.folder,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _to_item(row)


@router.put("/world-books/reorder")
def reorder_world_books(
    payload: ReorderRequest,
    db: Session = Depends(get_db),
) -> dict[str, str]:
    _ensure_sort_order_column(db)
    for idx, book_id in enumerate(payload.ordered_ids):
        row = db.query(WorldBook).filter(WorldBook.id == book_id).first()
        if row:
            row.sort_order = idx
    db.commit()
    return {"status": "ok"}


@router.put("/world-books/{book_id}", response_model=WorldBookItem)
def update_world_book(
    book_id: int,
    payload: WorldBookUpdateRequest,
    db: Session = Depends(get_db),
) -> WorldBookItem:
    row = db.query(WorldBook).filter(WorldBook.id == book_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="World book not found")

    if hasattr(payload, "model_dump"):
        update_data = payload.model_dump(exclude_unset=True)
    else:
        update_data = payload.dict(exclude_unset=True)
    for key, value in update_data.items():
        setattr(row, key, value)

    db.commit()
    db.refresh(row)
    return _to_item(row)


@router.delete("/world-books/{book_id}", response_model=WorldBookDeleteResponse)
def delete_world_book(book_id: int, db: Session = Depends(get_db)) -> WorldBookDeleteResponse:
    row = db.query(WorldBook).filter(WorldBook.id == book_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="World book not found")
    db.delete(row)
    db.commit()
    return WorldBookDeleteResponse(status="deleted", id=book_id)
