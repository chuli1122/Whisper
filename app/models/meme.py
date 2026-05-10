from __future__ import annotations

from datetime import datetime, timedelta, timezone

from typing import Any

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.models import Base

TZ_EAST8 = timezone(timedelta(hours=8))


def _now_beijing() -> datetime:
    return datetime.now(TZ_EAST8)


class Meme(Base):
    __tablename__ = "memes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    term: Mapped[str] = mapped_column(String(100), nullable=False)
    category: Mapped[str | None] = mapped_column(String(50), nullable=True)
    type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    keywords: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_beijing)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_beijing, onupdate=_now_beijing)
