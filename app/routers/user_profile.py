from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import UserProfile
from app.utils import format_datetime, TZ_EAST8

logger = logging.getLogger(__name__)
router = APIRouter()


class UserProfileResponse(BaseModel):
    id: int
    nickname: str | None
    basic_info: str | None
    avatar_url: str | None
    background_url: str | None
    theme: dict[str, Any] | None
    updated_at: str | None


class UserProfileUpdateRequest(BaseModel):
    nickname: str | None = None
    basic_info: str | None = None
    avatar_url: str | None = None
    background_url: str | None = None
    theme: dict[str, Any] | None = None


def _get_or_create_profile(db: Session) -> UserProfile:
    profile = db.query(UserProfile).order_by(UserProfile.id.asc()).first()
    if profile is not None:
        return profile

    profile = UserProfile(
        nickname=None,
        basic_info=None,
        avatar_url=None,
        background_url=None,
        theme={},
        updated_at=datetime.now(TZ_EAST8),
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def _to_response(profile: UserProfile) -> UserProfileResponse:
    return UserProfileResponse(
        id=profile.id,
        nickname=profile.nickname,
        basic_info=profile.basic_info,
        avatar_url=profile.avatar_url,
        background_url=profile.background_url,
        theme=profile.theme or {},
        updated_at=format_datetime(profile.updated_at),
    )


@router.get("/user/profile", response_model=UserProfileResponse)
def get_user_profile(db: Session = Depends(get_db)) -> UserProfileResponse:
    profile = _get_or_create_profile(db)
    return _to_response(profile)


@router.put("/user/profile", response_model=UserProfileResponse)
def update_user_profile(
    payload: UserProfileUpdateRequest,
    db: Session = Depends(get_db),
) -> UserProfileResponse:
    profile = _get_or_create_profile(db)
    if hasattr(payload, "model_dump"):
        update_data = payload.model_dump(exclude_unset=True)
    else:
        update_data = payload.dict(exclude_unset=True)

    for key, value in update_data.items():
        setattr(profile, key, value)

    profile.updated_at = datetime.now(TZ_EAST8)
    db.commit()
    db.refresh(profile)
    return _to_response(profile)
