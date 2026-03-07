from __future__ import annotations

import logging
import traceback
from collections import defaultdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import CotRecord
from app.utils import format_datetime_short

logger = logging.getLogger(__name__)
router = APIRouter()

COT_MAX_KEEP = 100


class CotBlock(BaseModel):
    block_type: str
    content: str
    tool_name: str | None = None


class CotRound(BaseModel):
    round_index: int
    blocks: list[CotBlock]


class CotMemory(BaseModel):
    id: int | str
    content: str


class CotItem(BaseModel):
    request_id: str
    created_at: str | None
    preview: str
    has_tool_calls: bool
    prompt_tokens: int = 0
    completion_tokens: int = 0
    elapsed_ms: int = 0
    cache_hit: bool = False
    total_input: int = 0
    assistant_id: int | None = None
    rounds: list[CotRound]
    injectedMemories: list[CotMemory] = []
    error: str | None = None


def _ensure_table(db: Session) -> bool:
    """Check if cot_records table exists, create it if not."""
    try:
        result = db.execute(
            text("SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'cot_records')")
        )
        exists = result.scalar()
        if not exists:
            db.execute(text("""
                CREATE TABLE IF NOT EXISTS cot_records (
                    id SERIAL PRIMARY KEY,
                    request_id VARCHAR(36) NOT NULL,
                    round_index INTEGER NOT NULL,
                    block_type VARCHAR(32) NOT NULL,
                    content TEXT NOT NULL,
                    tool_name VARCHAR(255),
                    assistant_id INTEGER,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """))
            db.execute(text("CREATE INDEX IF NOT EXISTS ix_cot_records_request_id ON cot_records(request_id)"))
            db.commit()
            logger.info("Created cot_records table")
        return True
    except Exception as exc:
        logger.error("Failed to ensure cot_records table: %s", exc)
        try:
            db.rollback()
        except Exception:
            pass
        return False


def _cleanup_old_cot_records(db: Session) -> None:
    """Keep only the most recent COT_MAX_KEEP request_ids, using raw SQL."""
    try:
        result = db.execute(text(
            "WITH old AS ("
            "  SELECT request_id FROM ("
            "    SELECT request_id, MIN(created_at) AS first_ts"
            "    FROM cot_records GROUP BY request_id"
            "    ORDER BY first_ts DESC"
            f"   OFFSET {COT_MAX_KEEP}"
            "  ) sub"
            ") "
            "DELETE FROM cot_records WHERE request_id IN (SELECT request_id FROM old)"
        ))
        if result.rowcount:
            db.commit()
            logger.info("COT cleanup: deleted %d rows", result.rowcount)
        else:
            db.rollback()
    except Exception as exc:
        logger.warning("COT cleanup failed: %s", exc, exc_info=True)
        try:
            db.rollback()
        except Exception:
            pass


@router.get("/cot", response_model=list[CotItem])
def list_cot(
    limit: int = Query(20, ge=1, le=100),
    assistant_id: int | None = Query(None),
    db: Session = Depends(get_db),
) -> Any:
    try:
        if not _ensure_table(db):
            return []

        _cleanup_old_cot_records(db)

        # Latest N distinct request_ids by first created_at
        base_query = db.query(
            CotRecord.request_id,
            func.min(CotRecord.created_at).label("first_ts"),
        )
        if assistant_id is not None:
            base_query = base_query.filter(CotRecord.assistant_id == assistant_id)
        subq = base_query.group_by(CotRecord.request_id).subquery()
        latest = (
            db.query(subq.c.request_id, subq.c.first_ts)
            .order_by(subq.c.first_ts.desc())
            .limit(limit)
            .all()
        )
        if not latest:
            return []

        request_ids = [row[0] for row in latest]
        first_ts_map: dict[str, Any] = {str(row[0]): row[1] for row in latest}

        # All blocks for these request_ids
        records = (
            db.query(CotRecord)
            .filter(CotRecord.request_id.in_(request_ids))
            .order_by(CotRecord.request_id, CotRecord.round_index.asc(), CotRecord.id.asc())
            .all()
        )

        # Group: request_id → round_index → [records]
        grouped: dict[str, dict[int, list[CotRecord]]] = defaultdict(lambda: defaultdict(list))
        for rec in records:
            grouped[str(rec.request_id)][rec.round_index].append(rec)

        result: list[CotItem] = []
        for req_id in request_ids:
            req_id_str = str(req_id)
            rounds_map = grouped.get(req_id_str, {})
            rounds: list[CotRound] = []
            preview = ""
            has_tool_calls = False
            prompt_tokens = 0
            completion_tokens = 0
            elapsed_ms = 0
            cache_hit = False
            total_input = 0
            rec_assistant_id: int | None = None
            injected_memories: list[CotMemory] = []
            error_text: str | None = None

            for round_idx in sorted(rounds_map.keys()):
                blocks: list[CotBlock] = []
                for rec in rounds_map[round_idx]:
                    if rec_assistant_id is None and getattr(rec, "assistant_id", None) is not None:
                        rec_assistant_id = rec.assistant_id
                    if rec.block_type == "usage":
                        try:
                            import json as _json
                            usage = _json.loads(rec.content or "{}")
                            prompt_tokens = usage.get("prompt_tokens", 0)
                            completion_tokens = usage.get("completion_tokens", 0)
                            elapsed_ms = usage.get("elapsed_ms", 0)
                            cache_hit = usage.get("cache_hit", False)
                            total_input = usage.get("total_input", 0)
                        except Exception:
                            pass
                        continue
                    if rec.block_type == "injected_memories":
                        try:
                            import json as _json
                            mems = _json.loads(rec.content or "[]")
                            injected_memories = [CotMemory(id=m.get("id", 0), content=m.get("content", "")) for m in mems]
                        except Exception:
                            pass
                        continue
                    if rec.block_type == "error":
                        error_text = rec.content or "Unknown error"
                    blocks.append(
                        CotBlock(
                            block_type=rec.block_type or "text",
                            content=rec.content or "",
                            tool_name=rec.tool_name,
                        )
                    )
                    if rec.block_type == "tool_use":
                        has_tool_calls = True
                    if rec.block_type == "text" and not preview:
                        preview = (rec.content or "")[:80]
                if blocks:
                    rounds.append(CotRound(round_index=round_idx, blocks=blocks))

            result.append(
                CotItem(
                    request_id=req_id_str,
                    created_at=format_datetime_short(first_ts_map.get(req_id_str)),
                    preview=preview,
                    has_tool_calls=has_tool_calls,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    elapsed_ms=elapsed_ms,
                    cache_hit=cache_hit,
                    total_input=total_input,
                    assistant_id=rec_assistant_id,
                    rounds=rounds,
                    injectedMemories=injected_memories,
                    error=error_text,
                )
            )

        return result

    except Exception as exc:
        logger.error("COT list_cot failed: %s\n%s", exc, traceback.format_exc())
        return JSONResponse(
            status_code=500,
            content={"detail": f"COT加载失败: {exc}"},
        )


class TranslateRequest(BaseModel):
    text: str


class TranslateResponse(BaseModel):
    translated: str


@router.post("/cot/translate", response_model=TranslateResponse)
def translate_cot_block(
    payload: TranslateRequest,
    db: Session = Depends(get_db),
) -> TranslateResponse:
    from app.services.summary_service import translate_text

    try:
        translated = translate_text(db, payload.text)
        return TranslateResponse(translated=translated)
    except Exception as exc:
        logger.error("Translation failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"翻译失败: {exc}")


@router.delete("/cot/{request_id}")
def delete_cot(
    request_id: str,
    db: Session = Depends(get_db),
) -> dict[str, str]:
    count = (
        db.query(CotRecord)
        .filter(CotRecord.request_id == request_id)
        .delete(synchronize_session=False)
    )
    db.commit()
    if count == 0:
        raise HTTPException(status_code=404, detail="Not found")
    return {"status": "deleted", "request_id": request_id}
