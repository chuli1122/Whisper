from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import ChatSession, Message, SessionSummary, Settings, SummaryLayer, SummaryLayerHistory, UserProfile
from app.utils import TZ_EAST8

logger = logging.getLogger(__name__)
router = APIRouter()

DEFAULT_DIALOGUE_RETAIN_BUDGET = 8000
DEFAULT_DIALOGUE_TRIGGER_THRESHOLD = 16000
DEFAULT_SUMMARY_BUDGET_LONGTERM = 800
DEFAULT_SUMMARY_BUDGET_DAILY = 800
DEFAULT_SUMMARY_BUDGET_RECENT = 2000
DEFAULT_GROUP_CHAT_WAIT_SECONDS = 5
DEFAULT_GROUP_CHAT_MAX_TOKENS = 600


class ContextBudgetResponse(BaseModel):
    retain_budget: int
    trigger_threshold: int
    summary_budget_longterm: int
    summary_budget_daily: int
    summary_budget_recent: int


class ContextBudgetUpdateRequest(BaseModel):
    retain_budget: int = Field(..., ge=1)
    trigger_threshold: int = Field(..., ge=1)
    summary_budget_longterm: int = Field(..., ge=200, le=5000)
    summary_budget_daily: int = Field(..., ge=200, le=5000)
    summary_budget_recent: int = Field(..., ge=500, le=20000)


class GroupChatSettingsResponse(BaseModel):
    wait_seconds: int
    max_tokens: int


class GroupChatSettingsUpdateRequest(BaseModel):
    wait_seconds: int = Field(..., ge=1)
    max_tokens: int = Field(..., ge=1)


def _safe_int(raw_value: str | None, default: int) -> int:
    try:
        return int(str(raw_value).strip())
    except Exception:
        return default


def _read_context_budget(db: Session) -> tuple[int, int, int, int, int]:
    rows = (
        db.query(Settings)
        .filter(Settings.key.in_([
            "dialogue_retain_budget", "dialogue_trigger_threshold",
            "summary_budget_longterm", "summary_budget_daily", "summary_budget_recent",
        ]))
        .all()
    )
    kv = {row.key: row.value for row in rows}
    retain_budget = _safe_int(
        kv.get("dialogue_retain_budget"), DEFAULT_DIALOGUE_RETAIN_BUDGET
    )
    trigger_threshold = _safe_int(
        kv.get("dialogue_trigger_threshold"), DEFAULT_DIALOGUE_TRIGGER_THRESHOLD
    )
    sb_longterm = _safe_int(
        kv.get("summary_budget_longterm"), DEFAULT_SUMMARY_BUDGET_LONGTERM
    )
    sb_daily = _safe_int(
        kv.get("summary_budget_daily"), DEFAULT_SUMMARY_BUDGET_DAILY
    )
    sb_recent = _safe_int(
        kv.get("summary_budget_recent"), DEFAULT_SUMMARY_BUDGET_RECENT
    )
    retain_budget = max(1, retain_budget)
    trigger_threshold = max(retain_budget, trigger_threshold)
    sb_longterm = max(200, sb_longterm)
    sb_daily = max(200, sb_daily)
    sb_recent = max(500, sb_recent)
    return retain_budget, trigger_threshold, sb_longterm, sb_daily, sb_recent


def _upsert_setting(db: Session, key: str, value: str | int) -> None:
    row = db.query(Settings).filter(Settings.key == key).first()
    if row:
        row.value = str(value)
    else:
        db.add(Settings(key=key, value=str(value)))


def _read_group_chat_settings(db: Session) -> tuple[int, int]:
    rows = (
        db.query(Settings)
        .filter(Settings.key.in_(["group_chat_wait_seconds", "group_chat_max_tokens"]))
        .all()
    )
    kv = {row.key: row.value for row in rows}
    wait_seconds = _safe_int(
        kv.get("group_chat_wait_seconds"), DEFAULT_GROUP_CHAT_WAIT_SECONDS
    )
    max_tokens = _safe_int(
        kv.get("group_chat_max_tokens"), DEFAULT_GROUP_CHAT_MAX_TOKENS
    )
    wait_seconds = max(1, wait_seconds)
    max_tokens = max(1, max_tokens)
    return wait_seconds, max_tokens


@router.get("/settings/context-budget", response_model=ContextBudgetResponse)
def get_context_budget(db: Session = Depends(get_db)) -> ContextBudgetResponse:
    retain, trigger, sb_lt, sb_d, sb_r = _read_context_budget(db)
    return ContextBudgetResponse(
        retain_budget=retain,
        trigger_threshold=trigger,
        summary_budget_longterm=sb_lt,
        summary_budget_daily=sb_d,
        summary_budget_recent=sb_r,
    )


@router.put("/settings/context-budget", response_model=ContextBudgetResponse)
def update_context_budget(
    payload: ContextBudgetUpdateRequest,
    db: Session = Depends(get_db),
) -> ContextBudgetResponse:
    retain_budget = max(1, int(payload.retain_budget))
    trigger_threshold = max(retain_budget, int(payload.trigger_threshold))
    sb_longterm = max(200, int(payload.summary_budget_longterm))
    sb_daily = max(200, int(payload.summary_budget_daily))
    sb_recent = max(500, int(payload.summary_budget_recent))
    _upsert_setting(db, "dialogue_retain_budget", retain_budget)
    _upsert_setting(db, "dialogue_trigger_threshold", trigger_threshold)
    _upsert_setting(db, "summary_budget_longterm", sb_longterm)
    _upsert_setting(db, "summary_budget_daily", sb_daily)
    _upsert_setting(db, "summary_budget_recent", sb_recent)
    db.commit()
    return ContextBudgetResponse(
        retain_budget=retain_budget,
        trigger_threshold=trigger_threshold,
        summary_budget_longterm=sb_longterm,
        summary_budget_daily=sb_daily,
        summary_budget_recent=sb_recent,
    )


@router.get("/settings/group-chat", response_model=GroupChatSettingsResponse)
def get_group_chat_settings(
    db: Session = Depends(get_db),
) -> GroupChatSettingsResponse:
    wait_seconds, max_tokens = _read_group_chat_settings(db)
    return GroupChatSettingsResponse(
        wait_seconds=wait_seconds,
        max_tokens=max_tokens,
    )


@router.put("/settings/group-chat", response_model=GroupChatSettingsResponse)
def update_group_chat_settings(
    payload: GroupChatSettingsUpdateRequest,
    db: Session = Depends(get_db),
) -> GroupChatSettingsResponse:
    wait_seconds = max(1, int(payload.wait_seconds))
    max_tokens = max(1, int(payload.max_tokens))
    _upsert_setting(db, "group_chat_wait_seconds", wait_seconds)
    _upsert_setting(db, "group_chat_max_tokens", max_tokens)
    db.commit()
    return GroupChatSettingsResponse(
        wait_seconds=wait_seconds,
        max_tokens=max_tokens,
    )


# ── Chat mode ────────────────────────────────────────────────────────────────

VALID_CHAT_MODES = ("short", "long")


class ChatModeResponse(BaseModel):
    mode: str


class ChatModeUpdateRequest(BaseModel):
    mode: str


@router.get("/settings/chat-mode", response_model=ChatModeResponse)
def get_chat_mode(db: Session = Depends(get_db)) -> ChatModeResponse:
    row = db.query(Settings).filter(Settings.key == "chat_mode").first()
    mode = row.value if row and row.value in VALID_CHAT_MODES else "long"
    return ChatModeResponse(mode=mode)


MODE_LABELS = {"short": "短消息", "long": "长消息"}


@router.put("/settings/chat-mode", response_model=ChatModeResponse)
def update_chat_mode(
    payload: ChatModeUpdateRequest,
    db: Session = Depends(get_db),
) -> ChatModeResponse:
    mode = payload.mode if payload.mode in VALID_CHAT_MODES else "long"
    _upsert_setting(db, "chat_mode", mode)

    # Insert system message, similar to mood switch
    MODE_SWITCH_MESSAGES = {
        "long": "[环境切换] 当前平台：Telegram（长消息模式）。\n注意：从这条消息开始按以下要求输出，不要模仿上文的风格。\n要求：完整段落输出，不拆条，不使用[NEXT]，每次回复至少3段，说话用双引号包裹（如\"我想你了。\"），动作描写和语言自然穿插交织在同一段内，段落要有体量。回复正文中一律使用第二人称\"你\"称呼对方，不许用\"她\"。",
        "short": "[环境切换] 当前平台：QQ（短消息模式）。\n注意：从这条消息开始按以下要求输出，不要模仿上文的风格。\n要求：像真人发微信一样自然回复，用[NEXT]拆条，不使用空行分段，不使用动作描写。",
    }
    switch_content = MODE_SWITCH_MESSAGES.get(mode, f"已切换到{mode}模式")
    latest_session = (
        db.query(ChatSession)
        .order_by(ChatSession.updated_at.desc(), ChatSession.id.desc())
        .first()
    )
    if latest_session:
        msg = Message(
            session_id=latest_session.id,
            role="system",
            content=switch_content,
            meta_info={"mode_switch": True},
            created_at=datetime.now(TZ_EAST8),
        )
        db.add(msg)

    db.commit()
    return ChatModeResponse(mode=mode)


# ── Buffer seconds ────────────────────────────────────────────────────────────

class BufferSecondsResponse(BaseModel):
    seconds: float


class BufferSecondsUpdateRequest(BaseModel):
    seconds: float = Field(..., ge=1, le=120)


@router.put("/settings/buffer-seconds", response_model=BufferSecondsResponse)
def update_buffer_seconds(
    payload: BufferSecondsUpdateRequest,
    db: Session = Depends(get_db),
) -> BufferSecondsResponse:
    seconds = max(1.0, float(payload.seconds))
    _upsert_setting(db, "telegram_buffer_seconds", int(seconds))
    db.commit()
    return BufferSecondsResponse(seconds=seconds)


# ── Short message max count ───────────────────────────────────────────────────

class ShortMsgMaxResponse(BaseModel):
    max_count: int


class ShortMsgMaxUpdateRequest(BaseModel):
    max_count: int = Field(..., ge=2, le=20)


@router.get("/settings/short-msg-max", response_model=ShortMsgMaxResponse)
def get_short_msg_max(db: Session = Depends(get_db)) -> ShortMsgMaxResponse:
    row = db.query(Settings).filter(Settings.key == "short_msg_max").first()
    return ShortMsgMaxResponse(max_count=int(row.value) if row else 8)


@router.put("/settings/short-msg-max", response_model=ShortMsgMaxResponse)
def update_short_msg_max(
    payload: ShortMsgMaxUpdateRequest,
    db: Session = Depends(get_db),
) -> ShortMsgMaxResponse:
    count = max(2, min(20, payload.max_count))
    _upsert_setting(db, "short_msg_max", str(count))
    db.commit()
    return ShortMsgMaxResponse(max_count=count)


# ── Max tool rounds ───────────────────────────────────────────────────────────

class MaxToolRoundsResponse(BaseModel):
    max_rounds: int


class MaxToolRoundsUpdateRequest(BaseModel):
    max_rounds: int = Field(..., ge=3, le=50)


@router.get("/settings/max-tool-rounds", response_model=MaxToolRoundsResponse)
def get_max_tool_rounds(db: Session = Depends(get_db)) -> MaxToolRoundsResponse:
    row = db.query(Settings).filter(Settings.key == "max_tool_rounds").first()
    return MaxToolRoundsResponse(max_rounds=int(row.value) if row else 15)


@router.put("/settings/max-tool-rounds", response_model=MaxToolRoundsResponse)
def update_max_tool_rounds(
    payload: MaxToolRoundsUpdateRequest,
    db: Session = Depends(get_db),
) -> MaxToolRoundsResponse:
    rounds = max(3, min(50, payload.max_rounds))
    _upsert_setting(db, "max_tool_rounds", str(rounds))
    db.commit()
    return MaxToolRoundsResponse(max_rounds=rounds)


# ── Mood ─────────────────────────────────────────────────────────────────────

VALID_MOOD_TAGS = {
    "happy", "sad", "angry", "anxious", "tired", "emo", "flirty", "proud", "calm",
}


class MoodResponse(BaseModel):
    mood: str | None


class MoodUpdateRequest(BaseModel):
    mood: str


@router.get("/settings/mood", response_model=MoodResponse)
def get_mood(db: Session = Depends(get_db)) -> MoodResponse:
    latest_summary = (
        db.query(SessionSummary)
        .filter(SessionSummary.mood_tag.isnot(None))
        .order_by(SessionSummary.created_at.desc(), SessionSummary.id.desc())
        .first()
    )
    mood = latest_summary.mood_tag if latest_summary else None
    return MoodResponse(mood=mood)


@router.put("/settings/mood", response_model=MoodResponse)
def set_mood(
    payload: MoodUpdateRequest,
    db: Session = Depends(get_db),
) -> MoodResponse:
    mood = (payload.mood or "").strip().lower()
    if mood not in VALID_MOOD_TAGS:
        raise HTTPException(status_code=400, detail="Invalid mood")

    # Find latest session
    latest_session = (
        db.query(ChatSession)
        .order_by(ChatSession.updated_at.desc(), ChatSession.id.desc())
        .first()
    )
    if not latest_session:
        raise HTTPException(status_code=404, detail="No session found")

    # Find latest summary for that session, or create placeholder
    latest_summary = (
        db.query(SessionSummary)
        .filter(SessionSummary.session_id == latest_session.id)
        .order_by(SessionSummary.created_at.desc(), SessionSummary.id.desc())
        .first()
    )
    if latest_summary:
        latest_summary.mood_tag = mood
    else:
        latest_summary = SessionSummary(
            session_id=latest_session.id,
            assistant_id=latest_session.assistant_id,
            summary_content="(手动设置心情)",
            perspective="user",
            mood_tag=mood,
        )
        db.add(latest_summary)
    db.flush()

    # Mark as manual
    _upsert_setting(db, "mood_manual", "true")

    db.commit()
    return MoodResponse(mood=mood)


# ── Proactive messaging ─────────────────────────────────────────────────────

_PROACTIVE_DEFAULTS = {
    "proactive_enabled": "false",
    "proactive_first_enabled": "true",
    "proactive_first_min": "5",
    "proactive_first_max": "10",
    "proactive_retry_enabled": "true",
    "proactive_random_min": "15",
    "proactive_random_max": "120",
    "proactive_max_retries": "8",
    "proactive_voice_enabled": "false",
}


class ProactiveSettingsResponse(BaseModel):
    enabled: bool
    first_enabled: bool
    first_min: int
    first_max: int
    retry_enabled: bool
    retry_min: int
    retry_max: int
    max_retries: int
    voice_enabled: bool


class ProactiveSettingsUpdateRequest(BaseModel):
    enabled: bool | None = None
    first_enabled: bool | None = None
    first_min: int | None = Field(None, ge=1, le=30)
    first_max: int | None = Field(None, ge=1, le=30)
    retry_enabled: bool | None = None
    retry_min: int | None = Field(None, ge=5, le=720)
    retry_max: int | None = Field(None, ge=5, le=720)
    max_retries: int | None = Field(None, ge=1, le=99)
    voice_enabled: bool | None = None


def _read_proactive_settings(db: Session) -> dict[str, str]:
    rows = (
        db.query(Settings)
        .filter(Settings.key.in_(_PROACTIVE_DEFAULTS.keys()))
        .all()
    )
    kv = {row.key: row.value for row in rows}
    return {k: kv.get(k, v) for k, v in _PROACTIVE_DEFAULTS.items()}


def _proactive_response(raw: dict[str, str]) -> ProactiveSettingsResponse:
    return ProactiveSettingsResponse(
        enabled=raw["proactive_enabled"] == "true",
        first_enabled=raw["proactive_first_enabled"] == "true",
        first_min=_safe_int(raw["proactive_first_min"], 5),
        first_max=_safe_int(raw["proactive_first_max"], 10),
        retry_enabled=raw["proactive_retry_enabled"] == "true",
        retry_min=_safe_int(raw["proactive_random_min"], 15),
        retry_max=_safe_int(raw["proactive_random_max"], 120),
        max_retries=_safe_int(raw["proactive_max_retries"], 8),
        voice_enabled=raw["proactive_voice_enabled"] == "true",
    )


@router.get("/settings/proactive", response_model=ProactiveSettingsResponse)
def get_proactive_settings(
    db: Session = Depends(get_db),
) -> ProactiveSettingsResponse:
    raw = _read_proactive_settings(db)
    return _proactive_response(raw)


@router.put("/settings/proactive", response_model=ProactiveSettingsResponse)
def update_proactive_settings(
    payload: ProactiveSettingsUpdateRequest,
    db: Session = Depends(get_db),
) -> ProactiveSettingsResponse:
    field_map = {
        "enabled": ("proactive_enabled", lambda v: "true" if v else "false"),
        "first_enabled": ("proactive_first_enabled", lambda v: "true" if v else "false"),
        "first_min": ("proactive_first_min", lambda v: str(int(v))),
        "first_max": ("proactive_first_max", lambda v: str(int(v))),
        "retry_enabled": ("proactive_retry_enabled", lambda v: "true" if v else "false"),
        "retry_min": ("proactive_random_min", lambda v: str(int(v))),
        "retry_max": ("proactive_random_max", lambda v: str(int(v))),
        "max_retries": ("proactive_max_retries", lambda v: str(int(v))),
        "voice_enabled": ("proactive_voice_enabled", lambda v: "true" if v else "false"),
    }
    for field_name, (key, converter) in field_map.items():
        value = getattr(payload, field_name)
        if value is not None:
            _upsert_setting(db, key, converter(value))
    db.commit()
    raw = _read_proactive_settings(db)
    return _proactive_response(raw)


# ── Voice settings (TTS / STT) ──────────────────────────────────────────────

_VOICE_DEFAULTS = {
    "tts_api_key": "",
    "tts_group_id": "",
    "tts_voice_id": "",
    "tts_model": "speech-02-hd",
    "stt_fallback_key": "",
}


class VoiceSettingsResponse(BaseModel):
    tts_api_key: str
    tts_group_id: str
    tts_voice_id: str
    tts_model: str
    stt_fallback_key: str


class VoiceSettingsUpdateRequest(BaseModel):
    tts_api_key: str | None = None
    tts_group_id: str | None = None
    tts_voice_id: str | None = None
    tts_model: str | None = None
    stt_fallback_key: str | None = None


def _read_voice_settings(db: Session) -> dict[str, str]:
    rows = db.query(Settings).filter(Settings.key.in_(_VOICE_DEFAULTS.keys())).all()
    kv = {row.key: row.value for row in rows}
    return {k: kv.get(k, v) for k, v in _VOICE_DEFAULTS.items()}


@router.get("/settings/voice", response_model=VoiceSettingsResponse)
def get_voice_settings(db: Session = Depends(get_db)) -> VoiceSettingsResponse:
    raw = _read_voice_settings(db)
    return VoiceSettingsResponse(**raw)


@router.put("/settings/voice", response_model=VoiceSettingsResponse)
def update_voice_settings(
    payload: VoiceSettingsUpdateRequest,
    db: Session = Depends(get_db),
) -> VoiceSettingsResponse:
    for field_name in _VOICE_DEFAULTS:
        value = getattr(payload, field_name, None)
        if value is not None:
            _upsert_setting(db, field_name, value)
    db.commit()
    raw = _read_voice_settings(db)
    return VoiceSettingsResponse(**raw)


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

    # 3. Restore to target version
    row.content = target_content
    row.version = target_version
    row.needs_merge = False
    row.updated_at = datetime.now(TZ_EAST8)

    # 4. Release summaries: keep merged_into (option C), only clear merged_at_version
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
        released_ids.append(s.id)

    if released_ids:
        row.needs_merge = True

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

    # Step 1: flush un-merged overflow summaries into layers (per assistant)
    flushed = 0
    to_daily = 0
    to_longterm = 0
    svc = SummaryService(SessionLocal)
    TZ_EAST8 = timezone(timedelta(hours=8))
    _today = datetime.now(TZ_EAST8).date()

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

        a_to_daily = 0
        a_to_longterm = 0
        for s in overflow:
            s_date = s.time_end or s.created_at
            if s_date:
                if s_date.tzinfo is None:
                    s_date = s_date.replace(tzinfo=TZ_EAST8)
                s_date = s_date.astimezone(TZ_EAST8).date()
            else:
                s_date = _today
            if s_date == _today:
                s.merged_into = "daily"
                a_to_daily += 1
            else:
                s.merged_into = "longterm"
                a_to_longterm += 1
            flushed += 1
        to_daily += a_to_daily
        to_longterm += a_to_longterm

        # Ensure layer rows exist and are marked for merge
        if a_to_daily > 0:
            svc.ensure_layer_needs_merge(db, assistant.id, "daily")
        if a_to_longterm > 0:
            svc.ensure_layer_needs_merge(db, assistant.id, "longterm")
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
        "flushed": flushed, "to_daily": to_daily, "to_longterm": to_longterm,
        "merge_triggered": merged_layers,
    }
