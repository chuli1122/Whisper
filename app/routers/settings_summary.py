from __future__ import annotations

import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import SessionSummary, Settings, SummaryLayer, SummaryLayerHistory
from app.utils import TZ_EAST8

logger = logging.getLogger(__name__)
router = APIRouter()

DEFAULT_SUMMARY_BUDGET_RECENT = 2000

# ── Summary layers (longterm / daily) ────────────────────────────────────────


class PendingDailyGroup(BaseModel):
    version: int
    ids: list[int]


class SummaryLayerItem(BaseModel):
    content: str
    updated_at: str | None
    version: int = 1
    pending_ids: list[int] = []
    pending_daily: list[PendingDailyGroup] = []
    needs_merge: bool = False


class SummaryLayersResponse(BaseModel):
    longterm: SummaryLayerItem
    daily: SummaryLayerItem


class SummaryLayerUpdateRequest(BaseModel):
    content: str


@router.get("/settings/summary-layers", response_model=SummaryLayersResponse)
def get_summary_layers(db: Session = Depends(get_db)) -> SummaryLayersResponse:
    rows = (
        db.query(SummaryLayer)
        .filter(SummaryLayer.layer_type.in_(["longterm", "daily"]))
        .all()
    )
    # Pick the row with the most content per layer type
    by_type: dict[str, SummaryLayer] = {}
    for row in rows:
        prev = by_type.get(row.layer_type)
        if not prev or len(row.content or "") > len(prev.content or ""):
            by_type[row.layer_type] = row

    def _item(layer_type: str) -> SummaryLayerItem:
        import json as _json
        row = by_type.get(layer_type)
        assistant_id = row.assistant_id if row else None
        # Query pending (unconsumed) summaries for this layer
        pending_q = db.query(SessionSummary.id).filter(
            SessionSummary.merged_into == layer_type,
            SessionSummary.merged_at_version.is_(None),
            SessionSummary.deleted_at.is_(None),
        )
        if assistant_id:
            pending_q = pending_q.filter(SessionSummary.assistant_id == assistant_id)
        pending_all = pending_q.all()
        all_pending_ids = {p.id for p in pending_all}

        # For longterm: find which pending summaries came from daily (have daily history)
        pending_daily: list[PendingDailyGroup] = []
        raw_pending_ids: list[int] = list(all_pending_ids)
        if layer_type == "longterm" and all_pending_ids:
            dh_q = db.query(SummaryLayerHistory).filter(
                SummaryLayerHistory.layer_type == "daily",
                SummaryLayerHistory.merged_summary_ids.isnot(None),
            )
            if assistant_id:
                dh_q = dh_q.filter(SummaryLayerHistory.assistant_id == assistant_id)
            daily_histories = dh_q.order_by(SummaryLayerHistory.version.desc()).all()
            claimed_by_daily = set()
            for dh in daily_histories:
                try:
                    merged_ids = set(_json.loads(dh.merged_summary_ids))
                except Exception:
                    continue
                overlap = all_pending_ids & merged_ids
                if overlap:
                    pending_daily.append(PendingDailyGroup(
                        version=dh.version,
                        ids=sorted(overlap),
                    ))
                    claimed_by_daily |= overlap
            raw_pending_ids = sorted(all_pending_ids - claimed_by_daily)

        if row:
            return SummaryLayerItem(
                content=row.content or "",
                updated_at=row.updated_at.isoformat() if row.updated_at else None,
                version=row.version,
                pending_ids=raw_pending_ids,
                pending_daily=pending_daily,
                needs_merge=row.needs_merge,
            )
        return SummaryLayerItem(
            content="", updated_at=None,
            pending_ids=raw_pending_ids,
            pending_daily=pending_daily,
        )

    return SummaryLayersResponse(longterm=_item("longterm"), daily=_item("daily"))


@router.put("/settings/summary-layers/{layer_type}", response_model=SummaryLayerItem)
def update_summary_layer(
    layer_type: str,
    payload: SummaryLayerUpdateRequest,
    db: Session = Depends(get_db),
) -> SummaryLayerItem:
    if layer_type not in ("longterm", "daily"):
        raise HTTPException(status_code=400, detail="Invalid layer type")

    # Find existing layer row (any assistant)
    row = (
        db.query(SummaryLayer)
        .filter(SummaryLayer.layer_type == layer_type)
        .order_by(SummaryLayer.updated_at.desc())
        .first()
    )
    now = datetime.now(TZ_EAST8)
    if row:
        if payload.content != row.content:
            db.add(SummaryLayerHistory(
                summary_layer_id=row.id,
                layer_type=row.layer_type,
                assistant_id=row.assistant_id,
                content=row.content,
                version=row.version,
            ))
            row.version += 1
        row.content = payload.content
        row.needs_merge = False
        row.updated_at = now
    else:
        from app.models.models import Assistant
        assistant = db.query(Assistant).filter(Assistant.deleted_at.is_(None)).first()
        if not assistant:
            raise HTTPException(status_code=404, detail="No assistant found")
        row = SummaryLayer(
            assistant_id=assistant.id,
            layer_type=layer_type,
            content=payload.content,
            needs_merge=False,
            updated_at=now,
        )
        db.add(row)

    db.commit()
    return SummaryLayerItem(
        content=row.content,
        updated_at=row.updated_at.isoformat() if row.updated_at else None,
        version=row.version,
    )


# ── Summary layer history ───────────────────────────────────────────────────


@router.get("/settings/summary-layers/{layer_type}/history")
def get_summary_layer_history(
    layer_type: str,
    db: Session = Depends(get_db),
):
    if layer_type not in ("longterm", "daily"):
        raise HTTPException(status_code=400, detail="Invalid layer type")
    rows = (
        db.query(SummaryLayerHistory)
        .filter(SummaryLayerHistory.layer_type == layer_type)
        .order_by(SummaryLayerHistory.version.desc(), SummaryLayerHistory.id.desc())
        .all()
    )
    import json as _json
    return {
        "history": [
            {
                "id": r.id,
                "version": r.version,
                "content": r.content,
                "merged_summary_ids": _json.loads(r.merged_summary_ids) if r.merged_summary_ids else None,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    }


@router.delete("/settings/summary-layers/history/{history_id}")
def delete_summary_layer_history(
    history_id: int,
    db: Session = Depends(get_db),
):
    row = db.query(SummaryLayerHistory).filter(SummaryLayerHistory.id == history_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="History entry not found")
    db.delete(row)
    db.commit()
    return {"success": True}


class RollbackRequest(BaseModel):
    history_id: int


@router.post("/settings/summary-layers/{layer_type}/rollback")
def rollback_summary_layer(
    layer_type: str,
    payload: RollbackRequest,
    db: Session = Depends(get_db),
):
    if layer_type not in ("longterm", "daily"):
        raise HTTPException(status_code=400, detail="Invalid layer type")

    history = db.query(SummaryLayerHistory).filter(SummaryLayerHistory.id == payload.history_id).first()
    if not history or history.layer_type != layer_type:
        raise HTTPException(status_code=404, detail="History entry not found")

    # Find the layer row
    row = db.query(SummaryLayer).filter(SummaryLayer.id == history.summary_layer_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Layer not found")

    # Cache target version before any mutation
    target_version = history.version
    target_content = history.content

    # 1. Save current content to history (for future forward-rollback)
    if row.content != target_content:
        db.add(SummaryLayerHistory(
            summary_layer_id=row.id,
            layer_type=row.layer_type,
            assistant_id=row.assistant_id,
            content=row.content,
            version=row.version,
            merged_summary_ids=None,
        ))

    # 2. Delete the target history entry (it becomes the current version)
    db.delete(history)

    # 3. Restore content AND version (history.version has no unique constraint,
    # so reverting the version number doesn't conflict with the just-saved snapshot)
    row.content = target_content
    row.version = target_version
    row.needs_merge = False
    row.updated_at = datetime.now(TZ_EAST8)

    # 4. Release summaries merged after the target version — clear both
    # merged_at_version AND merged_into so they return to the unmerged pool
    # (front-end stops showing "已归档" tag).
    released = (
        db.query(SessionSummary)
        .filter(
            SessionSummary.merged_into == layer_type,
            SessionSummary.assistant_id == row.assistant_id,
            SessionSummary.merged_at_version > target_version,
        )
        .all()
    )
    released_ids = []
    for s in released:
        s.merged_at_version = None
        s.merged_into = None
        released_ids.append(s.id)

    db.commit()
    logger.info(
        "[rollback] %s rolled back to v%d, released %d summaries: %s",
        layer_type, target_version, len(released_ids), released_ids,
    )
    return {
        "content": row.content,
        "version": row.version,
        "released_summary_ids": released_ids,
    }


@router.get("/settings/summary-layers/flush-status")
def flush_status(db: Session = Depends(get_db)):
    """Preview what flush would do."""
    from app.models.models import Assistant

    assistants = db.query(Assistant).filter(Assistant.deleted_at.is_(None)).all()
    if not assistants:
        return {"pending_flush": 0, "pending_merge": [], "already_merged": []}

    budget_key = "summary_budget_recent"
    budget_row = db.query(Settings).filter(Settings.key == budget_key).first()
    budget_recent = int(budget_row.value) if budget_row else DEFAULT_SUMMARY_BUDGET_RECENT

    def _est(text: str) -> int:
        return max(1, len(text) * 2 // 3)

    pending_flush = 0
    for assistant in assistants:
        all_summaries = (
            db.query(SessionSummary)
            .filter(
                SessionSummary.assistant_id == assistant.id,
                SessionSummary.deleted_at.is_(None),
                SessionSummary.msg_id_start.isnot(None),
            )
            .order_by(SessionSummary.created_at.desc())
            .all()
        )
        used = 0
        for s in all_summaries:
            content = (s.summary_content or "").strip()
            if not content:
                continue
            tokens = _est(content)
            if used + tokens <= budget_recent:
                used += tokens
            elif s.merged_into is None:
                pending_flush += 1

    pending_merge = []
    already_merged = []
    layer_rows = (
        db.query(SummaryLayer)
        .filter(SummaryLayer.layer_type.in_(["daily", "longterm"]))
        .all()
    )
    seen_types = set()
    for row in layer_rows:
        lt = row.layer_type
        if lt in seen_types:
            continue
        has_content = bool(row.content and row.content.strip())
        if has_content or row.needs_merge:
            seen_types.add(lt)
            if row.needs_merge:
                pending_merge.append(lt)
            elif has_content:
                already_merged.append(lt)

    return {"pending_flush": pending_flush, "pending_merge": pending_merge, "already_merged": already_merged}


@router.post("/settings/summary-layers/flush")
def flush_summaries_to_layers(force: bool = False, db: Session = Depends(get_db)):
    """Flush overflow summaries into layers + merge pending. force=true re-merges all."""
    import threading
    from app.database import SessionLocal
    from app.models.models import Assistant
    from app.services.summary_service import SummaryService

    assistants = db.query(Assistant).filter(Assistant.deleted_at.is_(None)).all()
    if not assistants:
        raise HTTPException(status_code=404, detail="No assistant found")

    budget_key = "summary_budget_recent"
    budget_row = db.query(Settings).filter(Settings.key == budget_key).first()
    budget_recent = int(budget_row.value) if budget_row else DEFAULT_SUMMARY_BUDGET_RECENT

    def _estimate_tokens(text: str) -> int:
        return max(1, len(text) * 2 // 3)

    # Step 1: flush un-merged overflow summaries into daily (per assistant)
    flushed = 0
    svc = SummaryService(SessionLocal)

    for assistant in assistants:
        all_summaries = (
            db.query(SessionSummary)
            .filter(
                SessionSummary.assistant_id == assistant.id,
                SessionSummary.deleted_at.is_(None),
                SessionSummary.msg_id_start.isnot(None),
            )
            .order_by(SessionSummary.created_at.desc())
            .all()
        )
        used = 0
        overflow: list[SessionSummary] = []
        for s in all_summaries:
            content = (s.summary_content or "").strip()
            if not content:
                continue
            tokens = _estimate_tokens(content)
            if used + tokens <= budget_recent:
                used += tokens
            elif s.merged_into is None:
                overflow.append(s)

        for s in overflow:
            s.merged_into = "daily"
            flushed += 1

        if overflow:
            svc.ensure_layer_needs_merge(db, assistant.id, "daily")
    if flushed:
        db.commit()

    # Step 2: trigger merge only on layers that already need it
    merged_layers = []
    merge_assistant_ids = set()
    layer_rows = (
        db.query(SummaryLayer)
        .filter(SummaryLayer.layer_type.in_(["daily", "longterm"]))
        .all()
    )
    for row in layer_rows:
        if force and (row.content and row.content.strip()):
            row.needs_merge = True
        if row.needs_merge:
            if row.layer_type not in merged_layers:
                merged_layers.append(row.layer_type)
            merge_assistant_ids.add(row.assistant_id)
    if force:
        db.commit()

    if merged_layers:
        merge_types = tuple(merged_layers)
        for aid in merge_assistant_ids:
            threading.Thread(
                target=svc.merge_layers_async, args=(aid, merge_types), daemon=True,
            ).start()

    return {
        "flushed": flushed, "to_daily": flushed,
        "merge_triggered": merged_layers,
    }


# ── Weekly merge info ────────────────────────────────────────────────────────

MERGE_INTERVAL_DAYS = 7


@router.get("/settings/merge-info")
def get_merge_info(db: Session = Depends(get_db)):
    """Return next weekly merge date, days/hours/minutes remaining, and total remaining_seconds."""
    row = db.query(Settings).filter(Settings.key == "last_weekly_merge").first()
    if row:
        last_merge = datetime.fromisoformat(row.value).date()
    else:
        last_merge = datetime.now(TZ_EAST8).date()
    next_merge = last_merge + timedelta(days=MERGE_INTERVAL_DAYS)
    now_bj = datetime.now(TZ_EAST8)
    next_merge_dt = datetime.combine(next_merge, datetime.min.time()).replace(tzinfo=TZ_EAST8)
    remaining = next_merge_dt - now_bj
    remaining_seconds = max(0, int(remaining.total_seconds()))
    days_left = remaining_seconds // 86400
    hours_left = (remaining_seconds % 86400) // 3600
    minutes_left = (remaining_seconds % 3600) // 60
    return {
        "last_merge": last_merge.isoformat(),
        "next_merge": next_merge.isoformat(),
        "days_left": days_left,
        "hours_left": hours_left,
        "minutes_left": minutes_left,
        "remaining_seconds": remaining_seconds,
        "interval_days": MERGE_INTERVAL_DAYS,
    }


# ── Archive only (no merge) ─────────────────────────────────────────────────

@router.post("/settings/summary-layers/archive")
def archive_overflow(db: Session = Depends(get_db)):
    """Archive overflow summaries into daily layer without triggering merge."""
    from app.database import SessionLocal
    from app.models.models import Assistant
    from app.services.summary_service import SummaryService

    assistants = db.query(Assistant).filter(Assistant.deleted_at.is_(None)).all()
    if not assistants:
        raise HTTPException(status_code=404, detail="No assistant found")

    budget_key = "summary_budget_recent"
    budget_row = db.query(Settings).filter(Settings.key == budget_key).first()
    budget_recent = int(budget_row.value) if budget_row else DEFAULT_SUMMARY_BUDGET_RECENT

    def _estimate_tokens(text: str) -> int:
        return max(1, len(text) * 2 // 3)

    flushed = 0
    svc = SummaryService(SessionLocal)

    for assistant in assistants:
        all_summaries = (
            db.query(SessionSummary)
            .filter(
                SessionSummary.assistant_id == assistant.id,
                SessionSummary.deleted_at.is_(None),
                SessionSummary.msg_id_start.isnot(None),
            )
            .order_by(SessionSummary.created_at.desc())
            .all()
        )
        used = 0
        overflow: list[SessionSummary] = []
        for s in all_summaries:
            content = (s.summary_content or "").strip()
            if not content:
                continue
            tokens = _estimate_tokens(content)
            if used + tokens <= budget_recent:
                used += tokens
            elif s.merged_into is None:
                overflow.append(s)

        for s in overflow:
            s.merged_into = "daily"
            flushed += 1

        if overflow:
            svc.ensure_layer_needs_merge(db, assistant.id, "daily")
    if flushed:
        db.commit()

    return {"flushed": flushed}


# ── Merge daily layer ────────────────────────────────────────────────────────

@router.post("/settings/summary-layers/merge-daily")
def merge_daily(db: Session = Depends(get_db)):
    """Trigger merge of daily layer (only if it needs merge)."""
    import threading
    from app.database import SessionLocal
    from app.models.models import Assistant
    from app.services.summary_service import SummaryService

    assistants = db.query(Assistant).filter(Assistant.deleted_at.is_(None)).all()
    svc = SummaryService(SessionLocal)
    triggered = False

    for assistant in assistants:
        layer = (
            db.query(SummaryLayer)
            .filter(
                SummaryLayer.assistant_id == assistant.id,
                SummaryLayer.layer_type == "daily",
            )
            .first()
        )
        if layer and layer.needs_merge:
            triggered = True
            threading.Thread(
                target=svc.merge_layers_async, args=(assistant.id, ("daily",)), daemon=True,
            ).start()

    return {"triggered": triggered}


# ── Merge daily → longterm (manual early merge) ─────────────────────────────

@router.post("/settings/summary-layers/merge-to-longterm")
def merge_to_longterm(db: Session = Depends(get_db)):
    """Manually trigger daily→longterm merge and reset the weekly countdown."""
    import threading
    from app.database import SessionLocal
    from app.models.models import Assistant
    from app.services.summary_service import SummaryService

    assistants = db.query(Assistant).filter(Assistant.deleted_at.is_(None)).all()
    if not assistants:
        raise HTTPException(status_code=404, detail="No assistant found")

    svc = SummaryService(SessionLocal)

    def _worker(aid: int):
        svc.daily_merge_to_longterm(aid)

    for assistant in assistants:
        threading.Thread(target=_worker, args=(assistant.id,), daemon=True).start()

    # Update last_weekly_merge to now (optimistic — merge runs in background)
    now_str = datetime.now(TZ_EAST8).date().isoformat()
    row = db.query(Settings).filter(Settings.key == "last_weekly_merge").first()
    if row:
        row.value = now_str
    else:
        db.add(Settings(key="last_weekly_merge", value=now_str))
    db.commit()

    return get_merge_info(db)
