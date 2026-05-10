from __future__ import annotations

import logging
import random

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.meme import Meme
from app.utils import format_datetime

logger = logging.getLogger(__name__)
router = APIRouter()

_table_ensured = False


def _ensure_table(db: Session) -> None:
    global _table_ensured
    if _table_ensured:
        return
    try:
        db.execute(text(
            "CREATE TABLE IF NOT EXISTS memes ("
            "  id SERIAL PRIMARY KEY,"
            "  term VARCHAR(100) NOT NULL,"
            "  category VARCHAR(50),"
            "  type VARCHAR(50),"
            "  content JSONB NOT NULL DEFAULT '{}',"
            "  keywords TEXT[],"
            "  created_at TIMESTAMPTZ DEFAULT now(),"
            "  updated_at TIMESTAMPTZ DEFAULT now()"
            ")"
        ))
        db.commit()
        cols = {c["name"]: c for c in inspect(db.get_bind()).get_columns("memes")}
        if "content" in cols and "JSON" not in str(cols["content"]["type"]).upper():
            db.execute(text("ALTER TABLE memes ALTER COLUMN content DROP DEFAULT"))
            db.execute(text(
                "ALTER TABLE memes ALTER COLUMN content TYPE JSONB "
                "USING CASE "
                "WHEN content IS NULL OR btrim(content::text) = '' THEN '{}'::jsonb "
                "ELSE jsonb_build_object('usage', content::text) "
                "END"
            ))
            db.execute(text("ALTER TABLE memes ALTER COLUMN content SET DEFAULT '{}'::jsonb"))
            db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning("[memes] failed to ensure memes table", exc_info=True)
        return
    _table_ensured = True


# ── Schemas ──

class MemeItem(BaseModel):
    id: int
    term: str
    category: str | None
    type: str | None
    content: dict | None
    keywords: list[str] | None
    created_at: str | None
    updated_at: str | None


class MemesResponse(BaseModel):
    memes: list[MemeItem]
    total: int


class MemeCreateRequest(BaseModel):
    term: str
    category: str | None = None
    type: str | None = None
    content: dict | None = None
    keywords: list[str] | None = None


class MemeUpdateRequest(BaseModel):
    term: str | None = None
    category: str | None = None
    type: str | None = None
    content: dict | None = None
    keywords: list[str] | None = None


class MemeDeleteResponse(BaseModel):
    status: str
    id: int


def _to_item(row: Meme) -> MemeItem:
    content = row.content
    if isinstance(content, str):
        content = {"usage": content} if content else {}
    if not isinstance(content, dict):
        content = {}
    return MemeItem(
        id=row.id,
        term=row.term,
        category=row.category,
        type=row.type,
        content=content,
        keywords=row.keywords or [],
        created_at=format_datetime(row.created_at),
        updated_at=format_datetime(row.updated_at) if row.updated_at else None,
    )


@router.get("/memes", response_model=MemesResponse)
def list_memes(
    category: str | None = Query(None),
    db: Session = Depends(get_db),
):
    _ensure_table(db)
    q = db.query(Meme)
    if category:
        q = q.filter(Meme.category == category)
    q = q.order_by(Meme.category.nulls_last(), Meme.id)
    rows = q.all()
    return MemesResponse(memes=[_to_item(r) for r in rows], total=len(rows))


@router.get("/memes/{meme_id}", response_model=MemeItem)
def get_meme(meme_id: int, db: Session = Depends(get_db)):
    _ensure_table(db)
    row = db.query(Meme).filter(Meme.id == meme_id).first()
    if not row:
        raise HTTPException(404, "Meme not found")
    return _to_item(row)


@router.post("/memes", response_model=MemeItem)
def create_meme(req: MemeCreateRequest, db: Session = Depends(get_db)):
    _ensure_table(db)
    row = Meme(
        term=req.term,
        category=req.category,
        type=req.type,
        content=req.content or {},
        keywords=req.keywords,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _to_item(row)


@router.put("/memes/{meme_id}", response_model=MemeItem)
def update_meme(meme_id: int, req: MemeUpdateRequest, db: Session = Depends(get_db)):
    _ensure_table(db)
    row = db.query(Meme).filter(Meme.id == meme_id).first()
    if not row:
        raise HTTPException(404, "Meme not found")
    fields_set = getattr(req, "model_fields_set", getattr(req, "__fields_set__", set()))
    if req.term is not None:
        row.term = req.term
    if "category" in fields_set:
        row.category = req.category
    if "type" in fields_set:
        row.type = req.type
    if "content" in fields_set:
        row.content = req.content or {}
    if "keywords" in fields_set:
        row.keywords = req.keywords
    db.commit()
    db.refresh(row)
    return _to_item(row)


@router.delete("/memes/{meme_id}", response_model=MemeDeleteResponse)
def delete_meme(meme_id: int, db: Session = Depends(get_db)):
    _ensure_table(db)
    row = db.query(Meme).filter(Meme.id == meme_id).first()
    if not row:
        raise HTTPException(404, "Meme not found")
    db.delete(row)
    db.commit()
    return MemeDeleteResponse(status="deleted", id=meme_id)


def get_memes_for_injection(db: Session, recent_texts: list[str], max_total: int = 5, random_count: int = 3) -> tuple[list[Meme], list[int]]:
    """Return memes for context injection: keyword-matched + random fill."""
    try:
        _ensure_table(db)
        all_memes = db.query(Meme).all()
    except Exception:
        logger.warning("[memes] failed to load memes for injection", exc_info=True)
        try:
            db.rollback()
        except Exception:
            pass
        return [], []
    if not all_memes:
        return [], []

    user_texts_lower = [t.lower() for t in recent_texts if t.strip()]
    matched = []
    matched_ids = set()
    for m in all_memes:
        if m.keywords:
            for kw in m.keywords:
                kw_l = kw.lower()
                for ut in user_texts_lower:
                    if kw_l in ut or (len(ut) >= 2 and ut in kw_l):
                        matched.append(m)
                        matched_ids.add(m.id)
                        break
                if m.id in matched_ids:
                    break

    if len(matched) >= max_total:
        return matched[:max_total], [m.id for m in matched[:max_total]]

    remaining = [m for m in all_memes if m.id not in matched_ids]
    fill_count = random_count if not matched else max_total - len(matched)
    fill = random.sample(remaining, min(fill_count, len(remaining)))

    result = matched + fill
    return result, [m.id for m in matched]
