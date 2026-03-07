from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import Assistant
from app.utils import format_datetime, TZ_EAST8

logger = logging.getLogger(__name__)
router = APIRouter()


class AssistantItem(BaseModel):
    id: int
    name: str
    avatar_url: str | None = None
    model_preset_id: int | None = None
    created_at: str | None


class AssistantsResponse(BaseModel):
    assistants: list[AssistantItem]


class AssistantCreateRequest(BaseModel):
    name: str


class AssistantUpdateRequest(BaseModel):
    name: str | None = None
    avatar_url: str | None = None
    system_prompt: str | None = None
    model_preset_id: int | None = None
    summary_model_preset_id: int | None = None
    summary_fallback_preset_id: int | None = None
    rule_set_ids: list[Any] | None = None


class AssistantFullResponse(BaseModel):
    id: int
    name: str
    avatar_url: str | None = None
    system_prompt: str
    model_preset_id: int | None = None
    summary_model_preset_id: int | None = None
    summary_fallback_preset_id: int | None = None
    rule_set_ids: list[Any] | None = None
    created_at: str | None


def _assistant_to_full(a: Assistant) -> AssistantFullResponse:
    return AssistantFullResponse(
        id=a.id,
        name=a.name,
        avatar_url=a.avatar_url,
        system_prompt=a.system_prompt or "",
        model_preset_id=a.model_preset_id,
        summary_model_preset_id=a.summary_model_preset_id,
        summary_fallback_preset_id=a.summary_fallback_preset_id,
        rule_set_ids=a.rule_set_ids,
        created_at=format_datetime(a.created_at),
    )


@router.get("/assistants", response_model=AssistantsResponse)
def list_assistants(db: Session = Depends(get_db)) -> AssistantsResponse:
    rows = (
        db.query(Assistant)
        .filter(Assistant.deleted_at.is_(None))
        .order_by(Assistant.id.asc())
        .all()
    )
    items = [
        AssistantItem(
            id=row.id,
            name=row.name,
            avatar_url=row.avatar_url,
            model_preset_id=row.model_preset_id,
            created_at=format_datetime(row.created_at),
        )
        for row in rows
    ]
    return AssistantsResponse(assistants=items)


@router.get("/assistants/{assistant_id}", response_model=AssistantFullResponse)
def get_assistant(
    assistant_id: int,
    db: Session = Depends(get_db),
) -> AssistantFullResponse:
    assistant = (
        db.query(Assistant)
        .filter(Assistant.id == assistant_id, Assistant.deleted_at.is_(None))
        .first()
    )
    if not assistant:
        raise HTTPException(status_code=404, detail="Assistant not found")
    return _assistant_to_full(assistant)


@router.post("/assistants", response_model=AssistantFullResponse)
def create_assistant(
    payload: AssistantCreateRequest,
    db: Session = Depends(get_db),
) -> AssistantFullResponse:
    assistant = Assistant(
        name=payload.name,
        system_prompt="",
    )
    db.add(assistant)
    db.commit()
    db.refresh(assistant)
    return _assistant_to_full(assistant)


@router.put("/assistants/{assistant_id}", response_model=AssistantFullResponse)
def update_assistant(
    assistant_id: int,
    payload: AssistantUpdateRequest,
    db: Session = Depends(get_db),
) -> AssistantFullResponse:
    assistant = (
        db.query(Assistant)
        .filter(Assistant.id == assistant_id, Assistant.deleted_at.is_(None))
        .first()
    )
    if not assistant:
        raise HTTPException(status_code=404, detail="Assistant not found")

    update_data = payload.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(assistant, key, value)

    db.commit()
    db.refresh(assistant)
    return _assistant_to_full(assistant)


@router.delete("/assistants/{assistant_id}")
def delete_assistant(
    assistant_id: int,
    db: Session = Depends(get_db),
):
    assistant = (
        db.query(Assistant)
        .filter(Assistant.id == assistant_id, Assistant.deleted_at.is_(None))
        .first()
    )
    if not assistant:
        raise HTTPException(status_code=404, detail="Assistant not found")

    assistant.deleted_at = datetime.now(TZ_EAST8)
    db.commit()
    return {"message": "deleted"}
