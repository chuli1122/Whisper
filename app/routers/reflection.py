from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import CoreBlockCandidate, Memory, MemoryVersion, PendingReflectionChange, ReflectionLog, Settings
from app.utils import TZ_EAST8, format_datetime

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Reflection trigger ──────────────────────────────────────────────────────

class ReflectionTasks(BaseModel):
    disclosure: bool = True
    merge: bool = True
    outdated: bool = True
    classify: bool = True

class ReflectionTriggerRequest(BaseModel):
    assistant_id: int = 2
    count: int | None = None  # reflect on last N memories; None = since last reflection
    start: int | None = None  # 1-based position (oldest=1)
    end: int | None = None    # 1-based position (inclusive)
    tasks: ReflectionTasks | None = None


class ReflectionTriggerResponse(BaseModel):
    log_id: int | None = None
    changes: dict[str, Any] = {}
    message: str = ""


@router.post("/reflection/trigger")
async def trigger_reflection(
    payload: ReflectionTriggerRequest | None = None,
    db: Session = Depends(get_db),
):
    """Trigger reflection via main model (ChatService), shows in COT.

    Runs in background — returns immediately so the UI stays responsive
    and the COT page can load while reflection is in progress.
    """
    import asyncio
    from sqlalchemy import func, desc
    from app.services.reflection_service import _send_reflection_trigger, _reflection_running
    import app.services.reflection_service as reflection_mod
    from app.models.models import Memory

    if reflection_mod._reflection_running:
        return {"message": "reflection already running", "status": "skipped"}

    start = (payload.start if payload else None)
    end = (payload.end if payload else None)

    # Build trigger_info (same logic as _check_should_trigger but without threshold check)
    total = db.query(func.count(Memory.id)).filter(Memory.deleted_at.is_(None), Memory.is_pending == False).scalar() or 0
    if start is not None and end is not None:
        trigger_info = {"count": end - start + 1, "start": start, "end": end}
    else:
        # Default: last 30 memories
        batch = min(30, total)
        trigger_info = {"count": batch, "start": max(1, total - batch + 1), "end": total}

    tasks = payload.tasks if payload and payload.tasks else ReflectionTasks()
    trigger_info["tasks"] = tasks.model_dump()

    async def _run_reflection():
        reflection_mod._reflection_running = True
        try:
            await _send_reflection_trigger(trigger_info)
        except Exception:
            logger.exception("Background reflection failed")
        finally:
            reflection_mod._reflection_running = False

    asyncio.create_task(_run_reflection())
    return {"message": "ok", "status": "started"}


# ── Revert a reflection change ──────────────────────────────────────────────

class RevertChangeRequest(BaseModel):
    log_id: int
    change_index: int  # index in the changes array


@router.post("/reflection/revert")
def revert_reflection_change(
    payload: RevertChangeRequest,
    db: Session = Depends(get_db),
) -> dict:
    from app.models.models import MemoryVersion
    log = db.get(ReflectionLog, payload.log_id)
    if not log:
        raise HTTPException(status_code=404, detail="Log not found")
    changes_data = log.changes or {}
    changes_list = changes_data.get("changes", [])
    if payload.change_index < 0 or payload.change_index >= len(changes_list):
        raise HTTPException(status_code=400, detail="Invalid change index")
    change = changes_list[payload.change_index]
    if change.get("reverted"):
        raise HTTPException(status_code=400, detail="Already reverted")

    memory_id = change.get("memory_id")
    action = change.get("action")
    memory = db.get(Memory, memory_id)

    if action == "update" and memory:
        # Restore from old values stored in the change
        db.add(MemoryVersion(
            memory_id=memory.id, content=memory.content,
            klass=memory.klass, tags=memory.tags,
            disclosure=memory.disclosure, changed_by="revert",
        ))
        if "old_content" in change:
            memory.content = change["old_content"]
        if "old_klass" in change:
            memory.klass = change["old_klass"]
        if "old_disclosure" in change:
            memory.disclosure = change["old_disclosure"]
    elif action == "delete" and memory:
        memory.deleted_at = None
    elif action == "merge":
        # Un-delete source
        if memory:
            memory.deleted_at = None
        # Restore target content
        merge_into_id = change.get("merge_into")
        if merge_into_id:
            target = db.get(Memory, merge_into_id)
            if target and "merge_target_old_content" in change:
                db.add(MemoryVersion(
                    memory_id=target.id, content=target.content,
                    klass=target.klass, tags=target.tags,
                    disclosure=target.disclosure, changed_by="revert",
                ))
                target.content = change["merge_target_old_content"]
    else:
        raise HTTPException(status_code=400, detail="Cannot revert: memory not found")

    # Mark as reverted in the log
    change["reverted"] = True
    changes_list[payload.change_index] = change
    changes_data["changes"] = changes_list
    from sqlalchemy.orm.attributes import flag_modified
    log.changes = changes_data
    flag_modified(log, "changes")
    db.commit()
    return {"status": "ok"}


# ── Revert ALL changes in a reflection log ─────────────────────────────────

class RevertAllRequest(BaseModel):
    log_id: int


@router.post("/reflection/revert-all")
def revert_all_reflection_changes(
    payload: RevertAllRequest,
    db: Session = Depends(get_db),
) -> dict:
    from app.models.models import MemoryVersion
    from sqlalchemy.orm.attributes import flag_modified

    log = db.get(ReflectionLog, payload.log_id)
    if not log:
        raise HTTPException(status_code=404, detail="Log not found")
    changes_data = log.changes or {}
    changes_list = changes_data.get("changes", [])

    reverted_count = 0
    for idx, change in enumerate(changes_list):
        if change.get("reverted"):
            continue

        memory_id = change.get("memory_id")
        action = change.get("action")
        memory = db.get(Memory, memory_id)

        if action == "update" and memory:
            db.add(MemoryVersion(
                memory_id=memory.id, content=memory.content,
                klass=memory.klass, tags=memory.tags,
                disclosure=memory.disclosure, changed_by="revert",
            ))
            if "old_content" in change:
                memory.content = change["old_content"]
            if "old_klass" in change:
                memory.klass = change["old_klass"]
            if "old_disclosure" in change:
                memory.disclosure = change["old_disclosure"]
        elif action == "delete" and memory:
            memory.deleted_at = None
        elif action == "merge":
            if memory:
                memory.deleted_at = None
            merge_into_id = change.get("merge_into")
            if merge_into_id:
                target = db.get(Memory, merge_into_id)
                if target and "merge_target_old_content" in change:
                    db.add(MemoryVersion(
                        memory_id=target.id, content=target.content,
                        klass=target.klass, tags=target.tags,
                        disclosure=target.disclosure, changed_by="revert",
                    ))
                    target.content = change["merge_target_old_content"]

        change["reverted"] = True
        changes_list[idx] = change
        reverted_count += 1

    changes_data["changes"] = changes_list
    log.changes = changes_data
    flag_modified(log, "changes")
    db.commit()
    return {"status": "ok", "reverted_count": reverted_count}


# ── Restore (re-apply) a reverted change ───────────────────────────────────

def _restore_single_change(change: dict, db: Session) -> bool:
    """Re-apply a single reverted change. Returns True if successful."""
    from app.models.models import MemoryVersion
    memory_id = change.get("memory_id")
    action = change.get("action")
    memory = db.get(Memory, memory_id)

    if action == "update" and memory:
        db.add(MemoryVersion(
            memory_id=memory.id, content=memory.content,
            klass=memory.klass, tags=memory.tags,
            disclosure=memory.disclosure, changed_by="restore",
        ))
        if "new_content" in change:
            memory.content = change["new_content"]
        if "new_klass" in change:
            memory.klass = change["new_klass"]
        if "new_disclosure" in change:
            memory.disclosure = change["new_disclosure"]
        return True
    elif action == "delete" and memory:
        from datetime import datetime, timezone
        memory.deleted_at = datetime.now(timezone.utc)
        return True
    elif action == "merge" and memory:
        from datetime import datetime, timezone
        # Re-delete source
        memory.deleted_at = datetime.now(timezone.utc)
        # Re-apply merged content to target
        merge_into_id = change.get("merge_into")
        if merge_into_id:
            target = db.get(Memory, merge_into_id)
            if target and "merge_target_new_content" in change:
                db.add(MemoryVersion(
                    memory_id=target.id, content=target.content,
                    klass=target.klass, tags=target.tags,
                    disclosure=target.disclosure, changed_by="restore",
                ))
                target.content = change["merge_target_new_content"]
        return True
    return False


@router.post("/reflection/restore")
def restore_reflection_change(
    payload: RevertChangeRequest,
    db: Session = Depends(get_db),
) -> dict:
    from sqlalchemy.orm.attributes import flag_modified

    log = db.get(ReflectionLog, payload.log_id)
    if not log:
        raise HTTPException(status_code=404, detail="Log not found")
    changes_data = log.changes or {}
    changes_list = changes_data.get("changes", [])
    if payload.change_index < 0 or payload.change_index >= len(changes_list):
        raise HTTPException(status_code=400, detail="Invalid change index")
    change = changes_list[payload.change_index]
    if not change.get("reverted"):
        raise HTTPException(status_code=400, detail="Not reverted, nothing to restore")

    if not _restore_single_change(change, db):
        raise HTTPException(status_code=400, detail="Cannot restore: memory not found")

    change["reverted"] = False
    changes_list[payload.change_index] = change
    changes_data["changes"] = changes_list
    log.changes = changes_data
    flag_modified(log, "changes")
    db.commit()
    return {"status": "ok"}


@router.post("/reflection/restore-all")
def restore_all_reflection_changes(
    payload: RevertAllRequest,
    db: Session = Depends(get_db),
) -> dict:
    from sqlalchemy.orm.attributes import flag_modified

    log = db.get(ReflectionLog, payload.log_id)
    if not log:
        raise HTTPException(status_code=404, detail="Log not found")
    changes_data = log.changes or {}
    changes_list = changes_data.get("changes", [])

    restored_count = 0
    for idx, change in enumerate(changes_list):
        if not change.get("reverted"):
            continue
        if _restore_single_change(change, db):
            change["reverted"] = False
            changes_list[idx] = change
            restored_count += 1

    changes_data["changes"] = changes_list
    log.changes = changes_data
    flag_modified(log, "changes")
    db.commit()
    return {"status": "ok", "restored_count": restored_count}


# ── Pending reflection changes ─────────────────────────────────────────────

@router.get("/reflection/pending")
def list_pending_changes(db: Session = Depends(get_db)) -> dict:
    rows = (
        db.query(PendingReflectionChange)
        .filter(PendingReflectionChange.status == "pending")
        .order_by(PendingReflectionChange.created_at.desc(), PendingReflectionChange.id.desc())
        .all()
    )
    items = []
    for r in rows:
        items.append({
            "id": r.id,
            "action": r.action,
            "memory_id": r.memory_id,
            "merge_into_id": r.merge_into_id,
            "old_content": r.old_content,
            "old_klass": r.old_klass,
            "old_disclosure": r.old_disclosure,
            "proposed_content": r.proposed_content,
            "proposed_klass": r.proposed_klass,
            "proposed_disclosure": r.proposed_disclosure,
            "proposed_tags": r.proposed_tags,
            "merge_target_old_content": r.merge_target_old_content,
            "reflection_log_id": r.reflection_log_id,
            "created_at": format_datetime(r.created_at),
        })
    return {"items": items, "total": len(items)}


@router.get("/reflection/pending/count")
def pending_change_count(db: Session = Depends(get_db)) -> dict:
    count = db.query(PendingReflectionChange).filter(PendingReflectionChange.status == "pending").count()
    return {"count": count}


def _apply_pending_change(db: Session, pc: PendingReflectionChange) -> bool:
    """Apply a single pending change to the actual memory. Returns True on success."""
    import re
    from datetime import datetime
    from app.services.embedding_service import EmbeddingService

    memory = db.get(Memory, pc.memory_id)
    if not memory or memory.deleted_at is not None:
        return False

    now = datetime.now(TZ_EAST8)

    if pc.action == "update":
        db.add(MemoryVersion(
            memory_id=memory.id, content=memory.content,
            klass=memory.klass, tags=memory.tags,
            disclosure=memory.disclosure, changed_by="reflection",
        ))
        if pc.proposed_content:
            ts_match = re.match(r"^\[\d{4}\.\d{2}\.\d{2} \d{2}:\d{2}\] ", memory.content)
            new_content = pc.proposed_content
            if ts_match and not re.match(r"^\[\d{4}\.\d{2}\.\d{2} \d{2}:\d{2}\] ", new_content):
                new_content = ts_match.group() + new_content
            memory.content = new_content
        if pc.proposed_klass:
            memory.klass = pc.proposed_klass
        if pc.proposed_disclosure is not None:
            memory.disclosure = pc.proposed_disclosure or None
        if pc.proposed_tags is not None:
            memory.tags = pc.proposed_tags
        memory.updated_at = now
        # Regenerate embedding
        embed_text = memory.content
        if memory.disclosure:
            embed_text = f"{embed_text} {memory.disclosure}"
        emb = EmbeddingService()
        new_vec = emb.get_embedding(embed_text)
        if new_vec is not None:
            memory.embedding = new_vec

    elif pc.action == "delete":
        memory.deleted_at = now

    elif pc.action == "merge":
        target = db.get(Memory, pc.merge_into_id) if pc.merge_into_id else None
        if not target or target.deleted_at is not None:
            return False
        db.add(MemoryVersion(
            memory_id=target.id, content=target.content,
            klass=target.klass, tags=target.tags,
            disclosure=target.disclosure, changed_by="reflection",
        ))
        if pc.proposed_content:
            ts_match = re.match(r"^\[\d{4}\.\d{2}\.\d{2} \d{2}:\d{2}\] ", target.content)
            new_content = pc.proposed_content
            if ts_match and not re.match(r"^\[\d{4}\.\d{2}\.\d{2} \d{2}:\d{2}\] ", new_content):
                new_content = ts_match.group() + new_content
            target.content = new_content
        target.updated_at = now
        memory.deleted_at = now
        # Regenerate target embedding
        embed_text = target.content
        if target.disclosure:
            embed_text = f"{embed_text} {target.disclosure}"
        emb = EmbeddingService()
        new_vec = emb.get_embedding(embed_text)
        if new_vec is not None:
            target.embedding = new_vec

    return True


class PendingChangeIdsRequest(BaseModel):
    ids: list[int]


@router.post("/reflection/pending/confirm")
def confirm_pending_changes(
    payload: PendingChangeIdsRequest,
    db: Session = Depends(get_db),
) -> dict:
    from datetime import datetime
    confirmed = 0
    for pc_id in payload.ids:
        pc = db.get(PendingReflectionChange, pc_id)
        if not pc or pc.status != "pending":
            continue
        if _apply_pending_change(db, pc):
            pc.status = "confirmed"
            pc.resolved_at = datetime.now(TZ_EAST8)
            confirmed += 1
        else:
            pc.status = "rejected"
            pc.resolved_at = datetime.now(TZ_EAST8)
    db.commit()
    return {"status": "ok", "confirmed": confirmed}


@router.post("/reflection/pending/reject")
def reject_pending_changes(
    payload: PendingChangeIdsRequest,
    db: Session = Depends(get_db),
) -> dict:
    from datetime import datetime
    rejected = 0
    for pc_id in payload.ids:
        pc = db.get(PendingReflectionChange, pc_id)
        if not pc or pc.status != "pending":
            continue
        pc.status = "rejected"
        pc.resolved_at = datetime.now(TZ_EAST8)
        rejected += 1
    db.commit()
    return {"status": "ok", "rejected": rejected}


@router.post("/reflection/pending/confirm-all")
def confirm_all_pending(db: Session = Depends(get_db)) -> dict:
    from datetime import datetime
    rows = db.query(PendingReflectionChange).filter(PendingReflectionChange.status == "pending").all()
    confirmed = 0
    for pc in rows:
        if _apply_pending_change(db, pc):
            pc.status = "confirmed"
            pc.resolved_at = datetime.now(TZ_EAST8)
            confirmed += 1
        else:
            pc.status = "rejected"
            pc.resolved_at = datetime.now(TZ_EAST8)
    db.commit()
    return {"status": "ok", "confirmed": confirmed}


@router.post("/reflection/pending/reject-all")
def reject_all_pending(db: Session = Depends(get_db)) -> dict:
    from datetime import datetime
    rows = db.query(PendingReflectionChange).filter(PendingReflectionChange.status == "pending").all()
    for pc in rows:
        pc.status = "rejected"
        pc.resolved_at = datetime.now(TZ_EAST8)
    db.commit()
    return {"status": "ok", "rejected": len(rows)}


@router.patch("/reflection/pending/{change_id}")
def update_pending_change(
    change_id: int,
    payload: dict,
    db: Session = Depends(get_db),
) -> dict:
    pc = db.get(PendingReflectionChange, change_id)
    if not pc or pc.status != "pending":
        raise HTTPException(status_code=404, detail="Not found or already resolved")
    if "proposed_content" in payload:
        pc.proposed_content = payload["proposed_content"]
    if "proposed_klass" in payload:
        pc.proposed_klass = payload["proposed_klass"]
    if "proposed_disclosure" in payload:
        pc.proposed_disclosure = payload["proposed_disclosure"]
    if "proposed_tags" in payload:
        pc.proposed_tags = payload["proposed_tags"]
    db.commit()
    return {"status": "ok"}


# ── Reflection history (confirmed/rejected) ──────────────────────────────

@router.get("/reflection/history")
def list_reflection_history(
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> dict:
    items: list[dict] = []

    # Source 1: pending_reflection_changes (confirmed/rejected)
    rows = (
        db.query(PendingReflectionChange)
        .filter(PendingReflectionChange.status.in_(["confirmed", "rejected"]))
        .order_by(PendingReflectionChange.resolved_at.desc(), PendingReflectionChange.id.desc())
        .all()
    )
    for r in rows:
        items.append({
            "id": f"p{r.id}",
            "action": r.action,
            "status": r.status,
            "memory_id": r.memory_id,
            "merge_into_id": r.merge_into_id,
            "old_content": r.old_content,
            "old_klass": r.old_klass,
            "old_disclosure": r.old_disclosure,
            "proposed_content": r.proposed_content,
            "proposed_klass": r.proposed_klass,
            "proposed_disclosure": r.proposed_disclosure,
            "merge_target_old_content": r.merge_target_old_content,
            "resolved_at": format_datetime(r.resolved_at),
            "created_at": format_datetime(r.created_at),
            "_sort_ts": r.resolved_at or r.created_at,
        })

    # Source 2: old reflection_logs (directly applied, before pending system)
    logs = (
        db.query(ReflectionLog)
        .order_by(ReflectionLog.created_at.desc())
        .all()
    )
    for log in logs:
        changes = (log.changes or {}).get("changes", [])
        for i, c in enumerate(changes):
            reverted = c.get("reverted", False)
            items.append({
                "id": f"l{log.id}_{i}",
                "action": c.get("action", "update"),
                "status": "reverted" if reverted else "confirmed",
                "memory_id": c.get("memory_id"),
                "merge_into_id": c.get("merge_into"),
                "old_content": c.get("old_content"),
                "old_klass": c.get("old_klass"),
                "old_disclosure": c.get("old_disclosure"),
                "proposed_content": c.get("content") or c.get("new_content"),
                "proposed_klass": c.get("new_klass"),
                "proposed_disclosure": c.get("new_disclosure"),
                "merge_target_old_content": c.get("merge_target_old_content"),
                "resolved_at": format_datetime(log.created_at),
                "created_at": format_datetime(log.created_at),
                "_sort_ts": log.created_at,
            })

    # Sort all items by time descending, then limit
    items.sort(key=lambda x: x.get("_sort_ts") or datetime.min, reverse=True)
    for item in items:
        item.pop("_sort_ts", None)

    return {"items": items[:limit]}


# ── Memory count (for reflection modal) ────────────────────────────────────

@router.get("/reflection/memory-count")
def get_memory_count(db: Session = Depends(get_db)) -> dict:
    total = db.query(Memory).filter(Memory.deleted_at.is_(None)).count()
    return {"total": total}


# ── Reflection logs ─────────────────────────────────────────────────────────

class ReflectionLogItem(BaseModel):
    id: int
    memory_count: int
    changes: dict[str, Any]
    model_used: str | None
    created_at: str | None


class ReflectionLogsResponse(BaseModel):
    logs: list[ReflectionLogItem]
    total: int


@router.get("/reflection/logs", response_model=ReflectionLogsResponse)
def list_reflection_logs(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> ReflectionLogsResponse:
    query = db.query(ReflectionLog)
    total = query.count()
    rows = (
        query.order_by(ReflectionLog.created_at.desc(), ReflectionLog.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    items = [
        ReflectionLogItem(
            id=row.id,
            memory_count=row.memory_count,
            changes=row.changes or {},
            model_used=row.model_used,
            created_at=format_datetime(row.created_at),
        )
        for row in rows
    ]
    return ReflectionLogsResponse(logs=items, total=total)


@router.get("/reflection/logs/{log_id}", response_model=ReflectionLogItem)
def get_reflection_log(
    log_id: int,
    db: Session = Depends(get_db),
) -> ReflectionLogItem:
    row = db.get(ReflectionLog, log_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Reflection log not found")
    return ReflectionLogItem(
        id=row.id,
        memory_count=row.memory_count,
        changes=row.changes or {},
        model_used=row.model_used,
        created_at=format_datetime(row.created_at),
    )


# ── Emotion keywords & mood klass weights ───────────────────────────────────

class EmotionConfigResponse(BaseModel):
    keywords: dict[str, list[str]]
    weights: dict[str, dict[str, float]]


class EmotionConfigUpdateRequest(BaseModel):
    keywords: dict[str, list[str]] | None = None
    weights: dict[str, dict[str, float]] | None = None


def _get_default_emotion_keywords() -> dict[str, list[str]]:
    from app.services.mood_detection import EMOTION_KEYWORDS
    return dict(EMOTION_KEYWORDS)


def _get_default_mood_klass_weights() -> dict[str, dict[str, float]]:
    from app.services.mood_detection import MOOD_KLASS_WEIGHTS
    return dict(MOOD_KLASS_WEIGHTS)


@router.get("/emotion-keywords", response_model=EmotionConfigResponse)
def get_emotion_keywords(
    db: Session = Depends(get_db),
) -> EmotionConfigResponse:
    keywords = _get_default_emotion_keywords()
    weights = _get_default_mood_klass_weights()

    kw_row = db.query(Settings).filter(Settings.key == "emotion_keywords").first()
    if kw_row and kw_row.value:
        try:
            keywords = json.loads(kw_row.value)
        except (json.JSONDecodeError, TypeError):
            pass

    wt_row = db.query(Settings).filter(Settings.key == "mood_klass_weights").first()
    if wt_row and wt_row.value:
        try:
            weights = json.loads(wt_row.value)
        except (json.JSONDecodeError, TypeError):
            pass

    return EmotionConfigResponse(keywords=keywords, weights=weights)


@router.put("/emotion-keywords")
def update_emotion_keywords(
    payload: EmotionConfigUpdateRequest,
    db: Session = Depends(get_db),
) -> dict[str, str]:
    if payload.keywords is not None:
        kw_row = db.query(Settings).filter(Settings.key == "emotion_keywords").first()
        if kw_row:
            kw_row.value = json.dumps(payload.keywords, ensure_ascii=False)
        else:
            db.add(Settings(key="emotion_keywords", value=json.dumps(payload.keywords, ensure_ascii=False)))

    if payload.weights is not None:
        wt_row = db.query(Settings).filter(Settings.key == "mood_klass_weights").first()
        if wt_row:
            wt_row.value = json.dumps(payload.weights, ensure_ascii=False)
        else:
            db.add(Settings(key="mood_klass_weights", value=json.dumps(payload.weights, ensure_ascii=False)))

    db.commit()
    return {"status": "ok"}


# ── Core block candidates ───────────────────────────────────────────────────

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


@router.get("/core-blocks/candidates", response_model=CoreBlockCandidatesResponse)
def list_core_block_candidates(
    assistant_id: int | None = Query(None),
    status: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> CoreBlockCandidatesResponse:
    query = db.query(CoreBlockCandidate)
    if assistant_id is not None:
        query = query.filter(CoreBlockCandidate.assistant_id == assistant_id)
    if status is not None:
        query = query.filter(CoreBlockCandidate.status == status)

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


# ── Threshold settings ─────────────────────────────────────────────────────

class ThresholdSettings(BaseModel):
    reflection_threshold: int
    reflection_enabled: bool
    core_blocks_auto_rewrite_threshold: int
    core_blocks_enabled: bool


def _get_bool_setting(db: Session, key: str, default: bool = True) -> bool:
    row = db.query(Settings).filter(Settings.key == key).first()
    if row and row.value is not None:
        return row.value.lower() in ("true", "1", "yes")
    return default


@router.get("/threshold-settings", response_model=ThresholdSettings)
def get_threshold_settings(db: Session = Depends(get_db)) -> ThresholdSettings:
    rt = db.query(Settings).filter(Settings.key == "reflection_threshold").first()
    cb = db.query(Settings).filter(Settings.key == "core_blocks_auto_rewrite_threshold").first()
    return ThresholdSettings(
        reflection_threshold=int(rt.value) if rt and rt.value else 30,
        reflection_enabled=_get_bool_setting(db, "reflection_enabled", True),
        core_blocks_auto_rewrite_threshold=int(cb.value) if cb and cb.value else 10,
        core_blocks_enabled=_get_bool_setting(db, "core_blocks_enabled", True),
    )


@router.put("/threshold-settings")
def update_threshold_settings(
    payload: ThresholdSettings,
    db: Session = Depends(get_db),
) -> dict[str, str]:
    for key, val in [
        ("reflection_threshold", str(payload.reflection_threshold)),
        ("reflection_enabled", str(payload.reflection_enabled).lower()),
        ("core_blocks_auto_rewrite_threshold", str(payload.core_blocks_auto_rewrite_threshold)),
        ("core_blocks_enabled", str(payload.core_blocks_enabled).lower()),
    ]:
        row = db.query(Settings).filter(Settings.key == key).first()
        if row:
            row.value = val
        else:
            db.add(Settings(key=key, value=val))
    db.commit()
    return {"status": "ok"}
