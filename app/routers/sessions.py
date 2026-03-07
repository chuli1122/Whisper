from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import and_, cast, func, or_
from sqlalchemy.dialects.postgresql import JSONB as JSONB_TYPE
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import Assistant, ChatSession, Message, SessionSummary, UserProfile
from app.utils import format_datetime, TZ_EAST8

logger = logging.getLogger(__name__)
router = APIRouter()
VALID_MOOD_TAGS = {
    "sad",
    "angry",
    "anxious",
    "tired",
    "emo",
    "happy",
    "flirty",
    "proud",
    "calm",
}


class SessionItem(BaseModel):
    id: int
    assistant_id: int | None
    title: str
    type: str
    created_at: str | None
    updated_at: str | None


class SessionListResponse(BaseModel):
    sessions: list[SessionItem]
    total: int


class SessionCreateRequest(BaseModel):
    assistant_id: int
    title: str = ""
    type: str = "chat"


class SessionUpdateRequest(BaseModel):
    title: str


class SessionDeleteResponse(BaseModel):
    status: str
    id: int


class SessionMessageItem(BaseModel):
    id: int
    role: str
    content: str
    meta_info: dict
    created_at: str | None
    summarized: bool = False
    summary_group_id: int | None = None


class SessionMessagesResponse(BaseModel):
    messages: list[SessionMessageItem]
    has_more: bool
    total: int = 0


class SessionSummaryItem(BaseModel):
    id: int
    session_id: int
    summary_content: str
    perspective: str
    msg_id_start: int | None
    msg_id_end: int | None
    time_start: str | None
    time_end: str | None
    mood_tag: str | None
    merged_into: str | None
    created_at: str | None


class SessionSummariesResponse(BaseModel):
    summaries: list[SessionSummaryItem]
    total: int


class MoodUpdateResponse(BaseModel):
    summary: SessionSummaryItem
    system_message: SessionMessageItem


class MoodSetResponse(BaseModel):
    mood_tag: str
    system_message: SessionMessageItem


class SessionSummaryUpdateRequest(BaseModel):
    mood_tag: str


class MessageUpdateRequest(BaseModel):
    content: str


class MessageDeleteResponse(BaseModel):
    status: str
    id: int


@router.get("/sessions", response_model=SessionListResponse)
def list_sessions(
    assistant_id: int | None = Query(None),
    limit: int = Query(20, ge=1, le=50),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> SessionListResponse:
    query = db.query(ChatSession)
    if assistant_id is not None:
        query = query.filter(ChatSession.assistant_id == assistant_id)

    total = query.count()
    rows = (
        query.order_by(ChatSession.updated_at.desc(), ChatSession.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    sessions = [
        SessionItem(
            id=row.id,
            assistant_id=row.assistant_id,
            title=row.title or "",
            type=row.type,
            created_at=format_datetime(row.created_at),
            updated_at=format_datetime(row.updated_at),
        )
        for row in rows
    ]
    return SessionListResponse(sessions=sessions, total=total)


@router.post("/sessions", response_model=SessionItem)
def create_session(
    payload: SessionCreateRequest,
    db: Session = Depends(get_db),
) -> SessionItem:
    now_utc = datetime.now(TZ_EAST8)
    row = ChatSession(
        assistant_id=payload.assistant_id,
        title=payload.title,
        type=payload.type,
        updated_at=now_utc,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return SessionItem(
        id=row.id,
        assistant_id=row.assistant_id,
        title=row.title or "",
        type=row.type,
        created_at=format_datetime(row.created_at),
        updated_at=format_datetime(row.updated_at),
    )


@router.get("/sessions/{session_id}/messages", response_model=SessionMessagesResponse)
def get_session_messages(
    session_id: int,
    limit: int = Query(50, ge=1, le=200),
    before_id: int | None = Query(None, ge=1),
    search: str | None = Query(None, min_length=1),
    role: str | None = Query(None),
    tg_msg_id: int | None = Query(None),
    include_no_message: bool = Query(False),
    only_no_message: bool = Query(False),
    db: Session = Depends(get_db),
) -> SessionMessagesResponse:
    session_row = db.query(ChatSession).filter(ChatSession.id == session_id).first()
    if session_row is None:
        raise HTTPException(status_code=404, detail="Session not found")

    if only_no_message:
        # Only return NO_MESSAGE judgment messages
        query = db.query(Message).filter(
            Message.session_id == session_id,
            Message.role == "assistant",
            Message.meta_info.has_key("no_message"),
        )
    else:
        _assistant_excludes = [
            ~Message.meta_info.has_key("tool_calls"),
            ~Message.meta_info.has_key("tool_call"),
        ]
        if not include_no_message:
            _assistant_excludes.append(~Message.meta_info.has_key("no_message"))

        query = db.query(Message).filter(
            Message.session_id == session_id,
            Message.role.in_(["user", "assistant", "system"]),
            func.length(func.trim(Message.content)) > 0,
            or_(
                Message.role != "assistant",
                and_(*_assistant_excludes),
            ),
        )
        if role:
            query = query.filter(Message.role == role)
    if before_id is not None:
        query = query.filter(Message.id < before_id)

    # Search filter
    if search:
        query = query.filter(Message.content.like(f"%{search}%"))

    # Telegram message ID filter
    if tg_msg_id is not None:
        query = query.filter(
            Message.telegram_message_id.op("@>")(cast([tg_msg_id], JSONB_TYPE))
        )

    # Count total (only on first page to avoid extra query on pagination)
    total = query.count() if before_id is None else 0

    # Query limit + 1 to check if there are more messages
    rows_desc = query.order_by(Message.id.desc()).limit(limit + 1).all()

    # Check if there are more messages
    has_more = len(rows_desc) > limit
    # Trim extra BEFORE reversing so we keep the newest messages
    rows_desc = rows_desc[:limit]
    rows = list(reversed(rows_desc))

    items = [
        SessionMessageItem(
            id=row.id,
            role=row.role,
            content=row.content,
            meta_info=row.meta_info or {},
            created_at=format_datetime(row.created_at),
            summarized=row.summary_group_id is not None,
            summary_group_id=row.summary_group_id,
        )
        for row in rows
    ]

    return SessionMessagesResponse(messages=items, has_more=has_more, total=total)


@router.get("/sessions/{session_id}/summaries", response_model=SessionSummariesResponse)
def get_session_summaries(
    session_id: int,
    search: str | None = Query(None, min_length=1),
    mood_tag: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> SessionSummariesResponse:
    session_row = db.query(ChatSession).filter(ChatSession.id == session_id).first()
    if session_row is None:
        raise HTTPException(status_code=404, detail="Session not found")

    base = db.query(SessionSummary).filter(
        SessionSummary.session_id == session_id, SessionSummary.deleted_at.is_(None)
    )
    if mood_tag:
        base = base.filter(SessionSummary.mood_tag == mood_tag)
    if search:
        base = base.filter(SessionSummary.summary_content.ilike(f"%{search}%"))
    total = base.count()
    rows = (
        base.order_by(SessionSummary.created_at.desc(), SessionSummary.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    items = [
        SessionSummaryItem(
            id=row.id,
            session_id=row.session_id,
            summary_content=row.summary_content,
            perspective=row.perspective,
            msg_id_start=row.msg_id_start,
            msg_id_end=row.msg_id_end,
            time_start=format_datetime(row.time_start),
            time_end=format_datetime(row.time_end),
            mood_tag=row.mood_tag,
            merged_into=row.merged_into,
            created_at=format_datetime(row.created_at),
        )
        for row in rows
    ]
    return SessionSummariesResponse(summaries=items, total=total)


@router.get("/summaries/{summary_id}", response_model=SessionSummaryItem)
def get_summary_by_id(
    summary_id: int,
    db: Session = Depends(get_db),
) -> SessionSummaryItem:
    row = db.get(SessionSummary, summary_id)
    if row is None or row.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Summary not found")
    return SessionSummaryItem(
        id=row.id,
        session_id=row.session_id,
        summary_content=row.summary_content,
        perspective=row.perspective,
        msg_id_start=row.msg_id_start,
        msg_id_end=row.msg_id_end,
        time_start=format_datetime(row.time_start),
        time_end=format_datetime(row.time_end),
        mood_tag=row.mood_tag,
        merged_into=row.merged_into,
        created_at=format_datetime(row.created_at),
    )


@router.put("/sessions/{session_id}/summaries/{summary_id}", response_model=MoodUpdateResponse)
def update_session_summary_mood(
    session_id: int,
    summary_id: int,
    payload: SessionSummaryUpdateRequest,
    db: Session = Depends(get_db),
) -> MoodUpdateResponse:
    session_row = db.query(ChatSession).filter(ChatSession.id == session_id).first()
    if session_row is None:
        raise HTTPException(status_code=404, detail="Session not found")

    mood_tag = (payload.mood_tag or "").strip().lower()
    if mood_tag not in VALID_MOOD_TAGS:
        raise HTTPException(status_code=400, detail="Invalid mood_tag")

    summary_row = (
        db.query(SessionSummary)
        .filter(
            SessionSummary.id == summary_id,
            SessionSummary.session_id == session_id,
        )
        .first()
    )
    if summary_row is None:
        raise HTTPException(status_code=404, detail="Summary not found")

    summary_row.mood_tag = mood_tag
    db.commit()
    db.refresh(summary_row)

    # Read user nickname
    user_profile = db.query(UserProfile).first()
    nickname = (user_profile.nickname if user_profile and user_profile.nickname else "她")

    # Insert system message
    sys_msg = Message(
        session_id=session_id,
        role="system",
        content=f"[{nickname}手动更改心情标签为: {mood_tag}]",
        meta_info={},
    )
    db.add(sys_msg)
    db.commit()
    db.refresh(sys_msg)

    return MoodUpdateResponse(
        summary=SessionSummaryItem(
            id=summary_row.id,
            session_id=summary_row.session_id,
            summary_content=summary_row.summary_content,
            perspective=summary_row.perspective,
            msg_id_start=summary_row.msg_id_start,
            msg_id_end=summary_row.msg_id_end,
            time_start=format_datetime(summary_row.time_start),
            time_end=format_datetime(summary_row.time_end),
            mood_tag=summary_row.mood_tag,
            merged_into=summary_row.merged_into,
            created_at=format_datetime(summary_row.created_at),
        ),
        system_message=SessionMessageItem(
            id=sys_msg.id,
            role=sys_msg.role,
            content=sys_msg.content,
            meta_info=sys_msg.meta_info or {},
            created_at=format_datetime(sys_msg.created_at),
        ),
    )


@router.put("/sessions/{session_id}/mood", response_model=MoodSetResponse)
def set_session_mood(
    session_id: int,
    payload: SessionSummaryUpdateRequest,
    db: Session = Depends(get_db),
) -> MoodSetResponse:
    session_row = db.query(ChatSession).filter(ChatSession.id == session_id).first()
    if session_row is None:
        raise HTTPException(status_code=404, detail="Session not found")

    mood_tag = (payload.mood_tag or "").strip().lower()
    if mood_tag not in VALID_MOOD_TAGS:
        raise HTTPException(status_code=400, detail="Invalid mood_tag")

    # Find latest summary for this session, or create a placeholder
    latest_summary = (
        db.query(SessionSummary)
        .filter(SessionSummary.session_id == session_id)
        .order_by(SessionSummary.created_at.desc(), SessionSummary.id.desc())
        .first()
    )
    if latest_summary:
        latest_summary.mood_tag = mood_tag
    else:
        latest_summary = SessionSummary(
            session_id=session_id,
            assistant_id=session_row.assistant_id,
            summary_content="(手动设置心情)",
            perspective="user",
            mood_tag=mood_tag,
        )
        db.add(latest_summary)
    db.flush()

    # Read user nickname
    user_profile = db.query(UserProfile).first()
    nickname = user_profile.nickname if user_profile and user_profile.nickname else "用户"

    # Insert system message
    sys_msg = Message(
        session_id=session_id,
        role="system",
        content=f"[{nickname}手动更改心情标签为: {mood_tag}]",
        meta_info={},
    )
    db.add(sys_msg)
    db.commit()
    db.refresh(sys_msg)

    return MoodSetResponse(
        mood_tag=mood_tag,
        system_message=SessionMessageItem(
            id=sys_msg.id,
            role=sys_msg.role,
            content=sys_msg.content,
            meta_info=sys_msg.meta_info or {},
            created_at=format_datetime(sys_msg.created_at),
        ),
    )


@router.put("/sessions/{session_id}", response_model=SessionItem)
def update_session(
    session_id: int,
    payload: SessionUpdateRequest,
    db: Session = Depends(get_db),
) -> SessionItem:
    row = db.query(ChatSession).filter(ChatSession.id == session_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Session not found")

    row.title = payload.title
    row.updated_at = datetime.now(TZ_EAST8)
    db.commit()
    db.refresh(row)
    return SessionItem(
        id=row.id,
        assistant_id=row.assistant_id,
        title=row.title or "",
        type=row.type,
        created_at=format_datetime(row.created_at),
        updated_at=format_datetime(row.updated_at),
    )


@router.delete("/sessions/{session_id}", response_model=SessionDeleteResponse)
def delete_session(
    session_id: int,
    db: Session = Depends(get_db),
) -> SessionDeleteResponse:
    row = db.query(ChatSession).filter(ChatSession.id == session_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Session not found")

    db.query(Message).filter(Message.session_id == session_id).delete(synchronize_session=False)
    db.delete(row)
    db.commit()
    return SessionDeleteResponse(status="deleted", id=session_id)


@router.put("/sessions/{session_id}/messages/{message_id}", response_model=SessionMessageItem)
def update_message(
    session_id: int,
    message_id: int,
    payload: MessageUpdateRequest,
    db: Session = Depends(get_db),
) -> SessionMessageItem:
    # Verify session exists
    session_row = db.query(ChatSession).filter(ChatSession.id == session_id).first()
    if session_row is None:
        raise HTTPException(status_code=404, detail="Session not found")

    # Find and update message
    message_row = (
        db.query(Message)
        .filter(Message.id == message_id, Message.session_id == session_id)
        .first()
    )
    if message_row is None:
        raise HTTPException(status_code=404, detail="Message not found")

    message_row.content = payload.content
    db.commit()
    db.refresh(message_row)

    return SessionMessageItem(
        id=message_row.id,
        role=message_row.role,
        content=message_row.content,
        meta_info=message_row.meta_info or {},
        created_at=format_datetime(message_row.created_at),
        summarized=message_row.summary_group_id is not None,
        summary_group_id=message_row.summary_group_id,
    )


class BatchDeleteRequest(BaseModel):
    ids: list[int]


class BatchDeleteResponse(BaseModel):
    deleted: int


@router.delete("/sessions/{session_id}/messages/batch", response_model=BatchDeleteResponse)
def batch_delete_messages(
    session_id: int,
    payload: BatchDeleteRequest,
    db: Session = Depends(get_db),
) -> BatchDeleteResponse:
    if not payload.ids:
        return BatchDeleteResponse(deleted=0)
    deleted = db.query(Message).filter(
        Message.session_id == session_id,
        Message.id.in_(payload.ids),
    ).delete(synchronize_session=False)
    db.commit()
    return BatchDeleteResponse(deleted=deleted)


@router.delete("/sessions/{session_id}/messages/{message_id}", response_model=MessageDeleteResponse)
def delete_message(
    session_id: int,
    message_id: int,
    db: Session = Depends(get_db),
) -> MessageDeleteResponse:
    # Verify session exists
    session_row = db.query(ChatSession).filter(ChatSession.id == session_id).first()
    if session_row is None:
        raise HTTPException(status_code=404, detail="Session not found")

    # Find and delete message
    message_row = (
        db.query(Message)
        .filter(Message.id == message_id, Message.session_id == session_id)
        .first()
    )
    if message_row is None:
        raise HTTPException(status_code=404, detail="Message not found")

    db.delete(message_row)
    db.commit()

    return MessageDeleteResponse(status="deleted", id=message_id)


# ── Summary trash / restore / permanent delete ─────────────────────────────

class TrashSummaryItem(BaseModel):
    id: int
    session_id: int
    summary_content: str
    mood_tag: str | None
    deleted_at: str | None
    created_at: str | None


class TrashSummariesResponse(BaseModel):
    summaries: list[TrashSummaryItem]
    total: int


class SummaryDeleteResponse(BaseModel):
    status: str
    id: int


@router.get("/sessions/{session_id}/summaries/trash", response_model=TrashSummariesResponse)
def list_summary_trash(
    session_id: int,
    db: Session = Depends(get_db),
) -> TrashSummariesResponse:
    query = db.query(SessionSummary).filter(
        SessionSummary.session_id == session_id,
        SessionSummary.deleted_at.is_not(None),
    )
    total = query.count()
    rows = query.order_by(SessionSummary.deleted_at.desc(), SessionSummary.id.desc()).all()
    items = [
        TrashSummaryItem(
            id=row.id,
            session_id=row.session_id,
            summary_content=row.summary_content,
            mood_tag=row.mood_tag,
            deleted_at=format_datetime(row.deleted_at),
            created_at=format_datetime(row.created_at),
        )
        for row in rows
    ]
    return TrashSummariesResponse(summaries=items, total=total)


@router.delete("/sessions/{session_id}/summaries/batch", response_model=BatchDeleteResponse)
def batch_delete_summaries(
    session_id: int,
    payload: BatchDeleteRequest,
    db: Session = Depends(get_db),
) -> BatchDeleteResponse:
    if not payload.ids:
        return BatchDeleteResponse(deleted=0)
    now = datetime.now(TZ_EAST8)
    deleted = db.query(SessionSummary).filter(
        SessionSummary.session_id == session_id,
        SessionSummary.id.in_(payload.ids),
        SessionSummary.deleted_at.is_(None),
    ).update({SessionSummary.deleted_at: now}, synchronize_session=False)
    # Clear summary_group_id on messages
    db.query(Message).filter(Message.summary_group_id.in_(payload.ids)).update(
        {Message.summary_group_id: None}, synchronize_session=False,
    )
    db.commit()
    return BatchDeleteResponse(deleted=deleted)


@router.delete("/sessions/{session_id}/summaries/{summary_id}", response_model=SummaryDeleteResponse)
def delete_summary(
    session_id: int,
    summary_id: int,
    db: Session = Depends(get_db),
) -> SummaryDeleteResponse:
    row = (
        db.query(SessionSummary)
        .filter(
            SessionSummary.id == summary_id,
            SessionSummary.session_id == session_id,
            SessionSummary.deleted_at.is_(None),
        )
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Summary not found")
    row.deleted_at = datetime.now(TZ_EAST8)
    # Clear summary_group_id so messages can be re-summarized
    db.query(Message).filter(Message.summary_group_id == summary_id).update(
        {Message.summary_group_id: None}, synchronize_session=False,
    )
    db.commit()
    return SummaryDeleteResponse(status="deleted", id=summary_id)


@router.post("/sessions/{session_id}/summaries/{summary_id}/restore", response_model=SummaryDeleteResponse)
def restore_summary(
    session_id: int,
    summary_id: int,
    db: Session = Depends(get_db),
) -> SummaryDeleteResponse:
    row = (
        db.query(SessionSummary)
        .filter(SessionSummary.id == summary_id, SessionSummary.session_id == session_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Summary not found")
    if row.deleted_at is None:
        raise HTTPException(status_code=400, detail="Summary is not deleted")
    row.deleted_at = None
    # Re-mark messages with this summary_group_id
    if row.msg_id_start and row.msg_id_end:
        db.query(Message).filter(
            Message.session_id == session_id,
            Message.id >= row.msg_id_start,
            Message.id <= row.msg_id_end,
            Message.summary_group_id.is_(None),
        ).update({Message.summary_group_id: summary_id}, synchronize_session=False)
    db.commit()
    return SummaryDeleteResponse(status="restored", id=summary_id)


@router.delete("/sessions/{session_id}/summaries/{summary_id}/permanent", response_model=SummaryDeleteResponse)
def delete_summary_permanent(
    session_id: int,
    summary_id: int,
    db: Session = Depends(get_db),
) -> SummaryDeleteResponse:
    row = (
        db.query(SessionSummary)
        .filter(SessionSummary.id == summary_id, SessionSummary.session_id == session_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Summary not found")
    db.delete(row)
    db.commit()
    return SummaryDeleteResponse(status="deleted_permanently", id=summary_id)


class SummaryContentUpdateRequest(BaseModel):
    summary_content: str


@router.patch("/sessions/{session_id}/summaries/{summary_id}", response_model=SessionSummaryItem)
def update_summary_content(
    session_id: int,
    summary_id: int,
    payload: SummaryContentUpdateRequest,
    db: Session = Depends(get_db),
) -> SessionSummaryItem:
    row = (
        db.query(SessionSummary)
        .filter(
            SessionSummary.id == summary_id,
            SessionSummary.session_id == session_id,
            SessionSummary.deleted_at.is_(None),
        )
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Summary not found")
    row.summary_content = payload.summary_content
    db.commit()
    db.refresh(row)
    return SessionSummaryItem(
        id=row.id,
        session_id=row.session_id,
        summary_content=row.summary_content,
        perspective=row.perspective,
        msg_id_start=row.msg_id_start,
        msg_id_end=row.msg_id_end,
        time_start=format_datetime(row.time_start),
        time_end=format_datetime(row.time_end),
        mood_tag=row.mood_tag,
        merged_into=row.merged_into,
        created_at=format_datetime(row.created_at),
    )


# ── Session info with assistant name ────────────────────────────────────────

class SessionInfoResponse(BaseModel):
    id: int
    assistant_id: int | None
    assistant_name: str | None
    title: str


@router.get("/sessions/{session_id}/info", response_model=SessionInfoResponse)
def get_session_info(
    session_id: int,
    db: Session = Depends(get_db),
) -> SessionInfoResponse:
    row = db.query(ChatSession).filter(ChatSession.id == session_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Session not found")
    name = None
    if row.assistant_id:
        ast = db.query(Assistant).filter(Assistant.id == row.assistant_id).first()
        if ast:
            name = ast.name
    return SessionInfoResponse(
        id=row.id,
        assistant_id=row.assistant_id,
        assistant_name=name,
        title=row.title or "",
    )
