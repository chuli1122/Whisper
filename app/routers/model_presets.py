from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import ModelPreset
from app.utils import format_datetime

logger = logging.getLogger(__name__)
router = APIRouter()


class PresetItem(BaseModel):
    id: int
    name: str
    model_name: str
    temperature: float | None
    top_p: float | None
    max_tokens: int
    thinking_budget: int
    api_provider_id: int
    created_at: str | None


class PresetsResponse(BaseModel):
    presets: list[PresetItem]


class PresetCreateRequest(BaseModel):
    name: str
    model_name: str
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int = 2048
    thinking_budget: int = 0
    api_provider_id: int


class PresetUpdateRequest(BaseModel):
    name: str | None = None
    model_name: str | None = None
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    thinking_budget: int | None = None
    api_provider_id: int | None = None


def _to_item(row: ModelPreset) -> PresetItem:
    return PresetItem(
        id=row.id,
        name=row.name,
        model_name=row.model_name,
        temperature=row.temperature,
        top_p=row.top_p,
        max_tokens=row.max_tokens,
        thinking_budget=row.thinking_budget or 0,
        api_provider_id=row.api_provider_id,
        created_at=format_datetime(row.created_at),
    )


@router.get("/presets", response_model=PresetsResponse)
def list_presets(
    provider_id: int | None = Query(None),
    db: Session = Depends(get_db),
) -> PresetsResponse:
    query = db.query(ModelPreset)
    if provider_id is not None:
        query = query.filter(ModelPreset.api_provider_id == provider_id)
    rows = query.order_by(ModelPreset.id.asc()).all()
    return PresetsResponse(presets=[_to_item(r) for r in rows])


@router.post("/presets", response_model=PresetItem, status_code=201)
def create_preset(
    payload: PresetCreateRequest,
    db: Session = Depends(get_db),
) -> PresetItem:
    preset = ModelPreset(
        name=payload.name,
        model_name=payload.model_name,
        temperature=payload.temperature,
        top_p=payload.top_p,
        max_tokens=payload.max_tokens,
        thinking_budget=payload.thinking_budget,
        api_provider_id=payload.api_provider_id,
    )
    db.add(preset)
    db.commit()
    db.refresh(preset)
    return _to_item(preset)


@router.put("/presets/{preset_id}", response_model=PresetItem)
def update_preset(
    preset_id: int,
    payload: PresetUpdateRequest,
    db: Session = Depends(get_db),
) -> PresetItem:
    preset = db.query(ModelPreset).filter(ModelPreset.id == preset_id).first()
    if not preset:
        raise HTTPException(status_code=404, detail="Preset not found")

    update_data = payload.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(preset, key, value)

    db.commit()
    db.refresh(preset)
    return _to_item(preset)


@router.delete("/presets/{preset_id}", status_code=204)
def delete_preset(
    preset_id: int,
    db: Session = Depends(get_db),
) -> None:
    preset = db.query(ModelPreset).filter(ModelPreset.id == preset_id).first()
    if not preset:
        raise HTTPException(status_code=404, detail="Preset not found")

    db.delete(preset)
    db.commit()
