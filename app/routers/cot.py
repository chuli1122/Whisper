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

COT_MAX_KEEP = 500


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
    recall_source: str | None = None


class CotModelInfo(BaseModel):
    model_name: str = ""
    preset_name: str = ""
    source: str = ""


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
    model_info: CotModelInfo | None = None
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
    """Keep only the most recent COT_MAX_KEEP request_ids.
    Preserves round_usage and model_info blocks (needed for monthly cost stats)."""
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
            " AND block_type NOT IN ('round_usage', 'model_info')"
        ))
        if result.rowcount:
            db.commit()
            logger.info("COT cleanup: deleted %d rows (kept usage/model_info)", result.rowcount)
        else:
            db.rollback()
    except Exception as exc:
        logger.warning("COT cleanup failed: %s", exc, exc_info=True)
        try:
            db.rollback()
        except Exception:
            pass


async def cot_cleanup_loop() -> None:
    """Background task: run _cleanup_old_cot_records every 10 minutes so
    the /cot list endpoint doesn't pay the GROUP BY + DELETE cost on each request."""
    import asyncio as _asyncio
    from app.database import SessionLocal
    while True:
        try:
            db = SessionLocal()
            try:
                _cleanup_old_cot_records(db)
            finally:
                db.close()
        except Exception as exc:
            logger.warning("cot_cleanup_loop iteration failed: %s", exc, exc_info=True)
        await _asyncio.sleep(600)


@router.get("/cot", response_model=list[CotItem])
def list_cot(
    limit: int = Query(500, ge=1, le=500),
    assistant_id: int | None = Query(None),
    db: Session = Depends(get_db),
) -> Any:
    try:
        if not _ensure_table(db):
            return []

        # Cleanup runs via background cot_cleanup_loop, not per request
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
            model_info: CotModelInfo | None = None

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
                    if rec.block_type == "model_info":
                        try:
                            import json as _json
                            mi = _json.loads(rec.content or "{}")
                            model_info = CotModelInfo(
                                model_name=mi.get("model_name", ""),
                                preset_name=mi.get("preset_name", ""),
                                source=mi.get("source", ""),
                            )
                        except Exception:
                            pass
                        continue
                    if rec.block_type == "injected_memories":
                        try:
                            import json as _json
                            mems = _json.loads(rec.content or "[]")
                            injected_memories = [CotMemory(id=m.get("id", 0), content=m.get("content", ""), recall_source=m.get("recall_source")) for m in mems]
                        except Exception:
                            pass
                        continue
                    if rec.block_type == "error":
                        error_text = rec.content or "Unknown error"
                    # Strip the heavy payload field from request_payload blocks;
                    # the frontend fetches it on demand via /cot/{request_id}/request-payload/{round_index}
                    if rec.block_type == "request_payload":
                        try:
                            import json as _json
                            snapshot = _json.loads(rec.content or "{}")
                            snapshot.pop("payload", None)
                            lean_content = _json.dumps(snapshot, ensure_ascii=False)
                        except Exception:
                            lean_content = rec.content or ""
                        blocks.append(
                            CotBlock(
                                block_type="request_payload",
                                content=lean_content,
                                tool_name=rec.tool_name,
                            )
                        )
                        continue
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
                    model_info=model_info,
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
    assistant_id: int | None = None


class TranslateResponse(BaseModel):
    translated: str


@router.post("/cot/translate", response_model=TranslateResponse)
def translate_cot_block(
    payload: TranslateRequest,
    db: Session = Depends(get_db),
) -> TranslateResponse:
    from app.services.summary_service import translate_text

    try:
        translated = translate_text(db, payload.text, assistant_id=payload.assistant_id)
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


@router.get("/cot/{request_id}/request-payload/{round_index}")
def get_request_payload(
    request_id: str,
    round_index: int,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Fetch the heavy `payload` field for a request_payload block on demand.
    list_cot strips it; the frontend loads it only when the block is expanded."""
    rec = (
        db.query(CotRecord)
        .filter(
            CotRecord.request_id == request_id,
            CotRecord.round_index == round_index,
            CotRecord.block_type == "request_payload",
        )
        .first()
    )
    if not rec:
        raise HTTPException(status_code=404, detail="request payload not found")
    try:
        import json as _json
        snapshot = _json.loads(rec.content or "{}")
        return {"payload": snapshot.get("payload")}
    except Exception:
        return {"payload": None}


# ── Monthly cost summary ──

MODEL_PRICING = {
    "opus":   {"input": 5, "cache_create": 10, "cache_read": 0.50, "output": 25, "currency": "USD"},
    "sonnet": {"input": 3, "cache_create": 3.75, "cache_read": 0.30, "output": 15, "currency": "USD"},
    "haiku":  {"input": 0.80, "cache_create": 1.00, "cache_read": 0.08, "output": 4, "currency": "USD"},
    # GLM-5.1 tiered by input length, 元/M tokens. cache_create limited-time free.
    "glm_short": {"input": 6, "cache_create": 0, "cache_read": 1.3, "output": 24, "currency": "CNY"},
    "glm_long":  {"input": 8, "cache_create": 0, "cache_read": 2,   "output": 28, "currency": "CNY"},
}


_ZERO_PRICING = {"input": 0, "cache_create": 0, "cache_read": 0, "output": 0, "currency": "USD"}

def _get_pricing(model_name: str, total_input_tokens: int = 0) -> dict[str, Any]:
    if not model_name:
        return MODEL_PRICING["opus"]
    m = model_name.lower()
    if "opus" in m:
        return MODEL_PRICING["opus"]
    if "haiku" in m:
        return MODEL_PRICING["haiku"]
    if "sonnet" in m or "claude" in m:
        return MODEL_PRICING["sonnet"]
    if "glm" in m:
        return MODEL_PRICING["glm_long" if total_input_tokens >= 32000 else "glm_short"]
    return _ZERO_PRICING


# CLD 按 16 号起算是用户 2026-04-15 要求的（对齐 Anthropic 的结算日感觉），
# GLM 保持自然月。改这里记得同步 docstring 里的说明。
_BILLING_CYCLE_ANCHOR: dict[str, int] = {
    "USD": 16,
    "CNY": 1,
}


def _cycle_start_for(anchor: int, now: "datetime") -> "datetime":
    from datetime import timedelta
    if now.day >= anchor:
        return now.replace(day=anchor, hour=0, minute=0, second=0, microsecond=0)
    prev_end = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0) - timedelta(seconds=1)
    return prev_end.replace(day=anchor, hour=0, minute=0, second=0, microsecond=0)


@router.get("/cot/monthly-cost")
def monthly_cost(
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Return current billing-cycle cost breakdown.

    CLD (USD): 每月 16 号起一个周期 → 下月 15 号止
    GLM (CNY): 自然月 1 号起
    """
    import json as _json
    from datetime import datetime, timezone, timedelta

    tz = timezone(timedelta(hours=8))
    now = datetime.now(tz)

    cycle_starts: dict[str, datetime] = {
        cur: _cycle_start_for(anchor, now)
        for cur, anchor in _BILLING_CYCLE_ANCHOR.items()
    }
    query_start = min(cycle_starts.values())

    round_usages = (
        db.query(CotRecord)
        .filter(
            CotRecord.block_type == "round_usage",
            CotRecord.created_at >= query_start,
        )
        .all()
    )

    model_infos = (
        db.query(CotRecord.request_id, CotRecord.content)
        .filter(
            CotRecord.block_type == "model_info",
            CotRecord.created_at >= query_start,
        )
        .all()
    )
    model_map: dict[str, str] = {}
    for req_id, content in model_infos:
        try:
            model_map[req_id] = _json.loads(content or "{}").get("model_name", "")
        except Exception:
            pass

    # Aggregate separately per currency so mixed-provider months (e.g. Claude $
    # for summary + GLM ¥ for chat) don't get added together.
    totals_by_currency: dict[str, dict[str, Any]] = {}

    for rec in round_usages:
        try:
            usage = _json.loads(rec.content or "{}")
        except Exception:
            continue
        model_name = model_map.get(rec.request_id, "")
        inp = usage.get("input", 0)
        cc = usage.get("cache_create", 0)
        cr = usage.get("cache_read", 0)
        out = usage.get("output", 0)
        # GLM tiered pricing decided by this round's full input size
        pricing = _get_pricing(model_name, inp + cc + cr)
        currency = pricing.get("currency", "USD")
        cycle_start = cycle_starts.get(currency) or query_start
        rec_ts = rec.created_at
        if rec_ts.tzinfo is None:
            rec_ts = rec_ts.replace(tzinfo=tz)
        if rec_ts < cycle_start:
            continue
        cost = (
            inp * pricing["input"] / 1e6
            + cc * pricing["cache_create"] / 1e6
            + cr * pricing["cache_read"] / 1e6
            + out * pricing["output"] / 1e6
        )
        t = totals_by_currency.setdefault(currency, {
            "cost": 0.0, "input": 0, "cache_create": 0, "cache_read": 0, "output": 0,
            "requests": set(),
        })
        t["cost"] += cost
        t["input"] += inp
        t["cache_create"] += cc
        t["cache_read"] += cr
        t["output"] += out
        t["requests"].add(rec.request_id)

    # Top-level `since` 用所有 cycle_start 里最早的那个（语义：这次窗口最早包含的一天）
    overall_start = min(cycle_starts.values())
    since = overall_start.strftime("%m/%d")

    # 按 _BILLING_CYCLE_ANCHOR 声明顺序输出，前端胶囊顺序依赖这个（CLD 在前 GLM 在后）。
    # 之前按 totals_by_currency 迭代输出会被"谁先出现数据"打乱。
    by_currency = []
    for cur in _BILLING_CYCLE_ANCHOR:
        t = totals_by_currency.get(cur)
        if t is None:
            continue
        by_currency.append({
            "currency": cur,
            "total_cost": round(t["cost"], 4),
            "request_count": len(t["requests"]),
            # since 直接用该 currency 的周期起点，不依赖数据最早日期
            "since": cycle_starts[cur].strftime("%m/%d"),
            "tokens": {
                "input": t["input"],
                "cache_create": t["cache_create"],
                "cache_read": t["cache_read"],
                "output": t["output"],
            },
        })
    # Backward-compat top-level fields: use USD bucket if present, else zeros.
    usd = next((x for x in by_currency if x["currency"] == "USD"), None)
    all_requests: set[str] = set()
    for t in totals_by_currency.values():
        all_requests |= t["requests"]
    return {
        "month": now.strftime("%Y-%m"),
        "since": since,
        "by_currency": by_currency,
        # Kept for old clients — only reflects USD spend
        "total_cost": usd["total_cost"] if usd else 0,
        "request_count": len(all_requests),
        "tokens": usd["tokens"] if usd else {
            "input": 0, "cache_create": 0, "cache_read": 0, "output": 0,
        },
    }


@router.get("/cot/trim-status")
def trim_status(db: Session = Depends(get_db)) -> dict[str, Any]:
    """Return last calculated dialogue_token_total and trigger_threshold."""
    import app.services.chat.chat_service as _cs
    from app.models.models import Settings
    status = getattr(_cs, "_last_trim_status", None)
    if status:
        return status
    # Fallback: read thresholds from DB, tokens unknown until first request
    kv = {}
    for r in db.query(Settings).filter(Settings.key.in_(["dialogue_trigger_threshold", "dialogue_retain_budget"])).all():
        kv[r.key] = r.value
    return {
        "dialogue_tokens": 0,
        "trigger": int(kv.get("dialogue_trigger_threshold", 16000)),
        "retain": int(kv.get("dialogue_retain_budget", 8000)),
    }
