from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from openai import OpenAI
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import ApiProvider, Assistant, ModelPreset, TheaterCard, TheaterStory
from app.utils import format_datetime, TZ_EAST8

logger = logging.getLogger(__name__)
router = APIRouter()


class TheaterCardItem(BaseModel):
    id: int
    name: str
    setting: str | None
    rule_set_ids: list[Any] | None
    created_at: str | None


class TheaterCardsResponse(BaseModel):
    cards: list[TheaterCardItem]
    total: int


class TheaterCardCreateRequest(BaseModel):
    name: str
    setting: str | None = None
    rule_set_ids: list[Any] | None = None


class TheaterCardUpdateRequest(BaseModel):
    name: str | None = None
    setting: str | None = None
    rule_set_ids: list[Any] | None = None


class TheaterStoryItem(BaseModel):
    id: int
    card_id: int
    ai_partner: str
    title: str
    summary: str | None
    tags: dict[str, Any]
    story_timespan: str | None
    started_at: str | None
    updated_at: str | None


class TheaterStoriesResponse(BaseModel):
    stories: list[TheaterStoryItem]
    total: int


class TheaterStoryCreateRequest(BaseModel):
    card_id: int
    ai_partner: str
    title: str
    summary: str | None = None
    tags: dict[str, Any] | None = None
    story_timespan: str | None = None


class TheaterStoryUpdateRequest(BaseModel):
    title: str | None = None
    summary: str | None = None
    tags: dict[str, Any] | None = None
    story_timespan: str | None = None


class TheaterSummarizeRequest(BaseModel):
    messages: list[dict[str, Any]]
    card_setting: str | None = None
    assistant_id: int


class TheaterCompressRequest(BaseModel):
    detailed_summaries: list[str]
    card_setting: str | None = None
    assistant_id: int


class TheaterTextResponse(BaseModel):
    text: str


def _story_to_item(row: TheaterStory) -> TheaterStoryItem:
    return TheaterStoryItem(
        id=row.id,
        card_id=row.card_id,
        ai_partner=row.ai_partner,
        title=row.title,
        summary=row.summary,
        tags=row.tags or {},
        story_timespan=row.story_timespan,
        started_at=format_datetime(row.started_at),
        updated_at=format_datetime(row.updated_at),
    )


def _resolve_primary_preset(db: Session, assistant: Assistant) -> ModelPreset | None:
    if assistant.summary_model_preset_id:
        preset = db.get(ModelPreset, assistant.summary_model_preset_id)
        if preset:
            return preset
    summary_named = db.query(ModelPreset).filter(ModelPreset.name == "summary").first()
    if summary_named:
        return summary_named
    return db.get(ModelPreset, assistant.model_preset_id)


def _resolve_fallback_preset(db: Session, assistant: Assistant) -> ModelPreset | None:
    if not assistant.summary_fallback_preset_id:
        return None
    return db.get(ModelPreset, assistant.summary_fallback_preset_id)


def _call_model_text(
    db: Session, preset: ModelPreset, system_prompt: str, user_prompt: str
) -> str:
    api_provider = db.get(ApiProvider, preset.api_provider_id)
    if not api_provider:
        raise ValueError(f"API provider not found for preset_id={preset.id}")

    base_url = api_provider.base_url
    if base_url.endswith("/chat/completions"):
        base_url = base_url[: -len("/chat/completions")]
        if not base_url.endswith("/v1"):
            base_url = f"{base_url.rstrip('/')}/v1"
    client = OpenAI(api_key=api_provider.api_key, base_url=base_url)

    response = client.chat.completions.create(
        model=preset.model_name,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=preset.temperature,
        top_p=preset.top_p,
        max_tokens=preset.max_tokens,
    )
    if not response.choices:
        raise ValueError("LLM response contained no choices")
    content = response.choices[0].message.content or ""
    return content.strip()


def _call_text_with_fallback(
    db: Session, assistant: Assistant, system_prompt: str, user_prompt: str
) -> str:
    primary = _resolve_primary_preset(db, assistant)
    if not primary:
        raise HTTPException(status_code=400, detail="No available summary model preset")
    fallback = _resolve_fallback_preset(db, assistant)
    try:
        return _call_model_text(db, primary, system_prompt, user_prompt)
    except Exception:
        logger.exception(
            "Primary theater summary model call failed (assistant_id=%s, preset_id=%s).",
            assistant.id,
            primary.id,
        )
        if fallback and fallback.id != primary.id:
            try:
                return _call_model_text(db, fallback, system_prompt, user_prompt)
            except Exception:
                logger.exception(
                    "Fallback theater summary model call failed (assistant_id=%s, preset_id=%s).",
                    assistant.id,
                    fallback.id,
                )
    raise HTTPException(status_code=502, detail="Model generation failed")


@router.get("/theater/cards", response_model=TheaterCardsResponse)
def list_cards(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> TheaterCardsResponse:
    query = db.query(TheaterCard)
    total = query.count()
    rows = (
        query.order_by(TheaterCard.created_at.desc(), TheaterCard.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    cards = [
        TheaterCardItem(
            id=row.id,
            name=row.name,
            setting=row.setting,
            rule_set_ids=row.rule_set_ids,
            created_at=format_datetime(row.created_at),
        )
        for row in rows
    ]
    return TheaterCardsResponse(cards=cards, total=total)


@router.post("/theater/cards", response_model=TheaterCardItem)
def create_card(
    payload: TheaterCardCreateRequest,
    db: Session = Depends(get_db),
) -> TheaterCardItem:
    row = TheaterCard(
        name=payload.name,
        setting=payload.setting,
        rule_set_ids=payload.rule_set_ids or [],
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return TheaterCardItem(
        id=row.id,
        name=row.name,
        setting=row.setting,
        rule_set_ids=row.rule_set_ids,
        created_at=format_datetime(row.created_at),
    )


@router.get("/theater/cards/{card_id}", response_model=TheaterCardItem)
def get_card(card_id: int, db: Session = Depends(get_db)) -> TheaterCardItem:
    row = db.query(TheaterCard).filter(TheaterCard.id == card_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Card not found")
    return TheaterCardItem(
        id=row.id,
        name=row.name,
        setting=row.setting,
        rule_set_ids=row.rule_set_ids,
        created_at=format_datetime(row.created_at),
    )


@router.put("/theater/cards/{card_id}", response_model=TheaterCardItem)
def update_card(
    card_id: int,
    payload: TheaterCardUpdateRequest,
    db: Session = Depends(get_db),
) -> TheaterCardItem:
    row = db.query(TheaterCard).filter(TheaterCard.id == card_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Card not found")
    if hasattr(payload, "model_dump"):
        update_data = payload.model_dump(exclude_unset=True)
    else:
        update_data = payload.dict(exclude_unset=True)
    for key, value in update_data.items():
        setattr(row, key, value)
    db.commit()
    db.refresh(row)
    return TheaterCardItem(
        id=row.id,
        name=row.name,
        setting=row.setting,
        rule_set_ids=row.rule_set_ids,
        created_at=format_datetime(row.created_at),
    )


@router.delete("/theater/cards/{card_id}")
def delete_card(card_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    row = db.query(TheaterCard).filter(TheaterCard.id == card_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Card not found")
    db.delete(row)
    db.commit()
    return {"status": "deleted", "id": card_id}


@router.get("/theater/stories", response_model=TheaterStoriesResponse)
def list_stories(
    card_id: int | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> TheaterStoriesResponse:
    query = db.query(TheaterStory)
    if card_id is not None:
        query = query.filter(TheaterStory.card_id == card_id)
    total = query.count()
    rows = (
        query.order_by(TheaterStory.updated_at.desc(), TheaterStory.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return TheaterStoriesResponse(
        stories=[_story_to_item(row) for row in rows],
        total=total,
    )


@router.post("/theater/stories", response_model=TheaterStoryItem)
def create_story(
    payload: TheaterStoryCreateRequest,
    db: Session = Depends(get_db),
) -> TheaterStoryItem:
    now_utc = datetime.now(TZ_EAST8)
    row = TheaterStory(
        card_id=payload.card_id,
        ai_partner=payload.ai_partner,
        title=payload.title,
        summary=payload.summary,
        tags=payload.tags or {},
        story_timespan=payload.story_timespan,
        started_at=now_utc,
        updated_at=now_utc,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _story_to_item(row)


@router.get("/theater/stories/{story_id}", response_model=TheaterStoryItem)
def get_story(story_id: int, db: Session = Depends(get_db)) -> TheaterStoryItem:
    row = db.query(TheaterStory).filter(TheaterStory.id == story_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Story not found")
    return _story_to_item(row)


@router.put("/theater/stories/{story_id}", response_model=TheaterStoryItem)
def update_story(
    story_id: int,
    payload: TheaterStoryUpdateRequest,
    db: Session = Depends(get_db),
) -> TheaterStoryItem:
    row = db.query(TheaterStory).filter(TheaterStory.id == story_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Story not found")
    if hasattr(payload, "model_dump"):
        update_data = payload.model_dump(exclude_unset=True)
    else:
        update_data = payload.dict(exclude_unset=True)
    for key, value in update_data.items():
        setattr(row, key, value)
    row.updated_at = datetime.now(TZ_EAST8)
    db.commit()
    db.refresh(row)
    return _story_to_item(row)


@router.delete("/theater/stories/{story_id}")
def delete_story(story_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    row = db.query(TheaterStory).filter(TheaterStory.id == story_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Story not found")
    db.delete(row)
    db.commit()
    return {"status": "deleted", "id": story_id}


@router.post("/theater/summarize", response_model=TheaterTextResponse)
def summarize_theater(
    payload: TheaterSummarizeRequest,
    db: Session = Depends(get_db),
) -> TheaterTextResponse:
    assistant = db.get(Assistant, payload.assistant_id)
    if not assistant:
        raise HTTPException(status_code=404, detail="Assistant not found")

    lines: list[str] = []
    for item in payload.messages:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role", "unknown")).strip()
        content = str(item.get("content", "")).strip()
        if not content:
            continue
        lines.append(f"{role}: {content}")
    conversation_text = "\n".join(lines).strip()
    if not conversation_text:
        raise HTTPException(status_code=400, detail="messages is empty")

    system_prompt = (
        "You summarize roleplay conversations in Chinese. "
        "Output only plain text, 200-300 Chinese characters, rich in detail and coherent."
    )
    user_prompt = "[Conversation]\n" + conversation_text
    if payload.card_setting:
        user_prompt += f"\n\n[Card setting]\n{payload.card_setting.strip()}"

    text = _call_text_with_fallback(db, assistant, system_prompt, user_prompt)
    return TheaterTextResponse(text=text)


@router.post("/theater/compress", response_model=TheaterTextResponse)
def compress_theater(
    payload: TheaterCompressRequest,
    db: Session = Depends(get_db),
) -> TheaterTextResponse:
    assistant = db.get(Assistant, payload.assistant_id)
    if not assistant:
        raise HTTPException(status_code=404, detail="Assistant not found")
    summaries = [item.strip() for item in payload.detailed_summaries if isinstance(item, str) and item.strip()]
    if not summaries:
        raise HTTPException(status_code=400, detail="detailed_summaries is empty")

    system_prompt = (
        "You compress multiple detailed roleplay summaries in Chinese. "
        "Output only plain text around 500 Chinese characters, keeping timeline and key emotional turns."
    )
    user_prompt = "[Detailed summaries]\n" + "\n\n".join(f"- {item}" for item in summaries)
    if payload.card_setting:
        user_prompt += f"\n\n[Card setting]\n{payload.card_setting.strip()}"

    text = _call_text_with_fallback(db, assistant, system_prompt, user_prompt)
    return TheaterTextResponse(text=text)
