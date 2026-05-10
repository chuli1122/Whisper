from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.models import Settings


def get_prompt_setting(db: Session, key: str, default: str) -> str:
    row = db.query(Settings).filter(Settings.key == key).first()
    if row and row.value:
        return row.value
    return default


def normalize_anthropic_base_url(base_url: str | None) -> str | None:
    """Clean a user-supplied base URL for the Anthropic SDK."""
    if not base_url:
        return None
    url = base_url.strip().rstrip("/")
    if url.endswith("/messages"):
        url = url[: -len("/messages")].rstrip("/")
    if url.endswith("/v1"):
        url = url[: -len("/v1")].rstrip("/")
    if url == "https://api.anthropic.com":
        return None
    return url or None
