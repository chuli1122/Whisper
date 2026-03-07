from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import ApiProvider
from app.utils import format_datetime

logger = logging.getLogger(__name__)
router = APIRouter()


class ProviderItem(BaseModel):
    id: int
    name: str
    base_url: str
    api_key: str
    auth_type: str
    created_at: str | None


class ProvidersResponse(BaseModel):
    providers: list[ProviderItem]


class ProviderCreateRequest(BaseModel):
    name: str
    base_url: str
    api_key: str
    auth_type: str = "api_key"


class ProviderUpdateRequest(BaseModel):
    name: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    auth_type: str | None = None


def _to_item(row: ApiProvider) -> ProviderItem:
    return ProviderItem(
        id=row.id,
        name=row.name,
        base_url=row.base_url,
        api_key=row.api_key,
        auth_type=row.auth_type,
        created_at=format_datetime(row.created_at),
    )


@router.get("/providers", response_model=ProvidersResponse)
def list_providers(db: Session = Depends(get_db)) -> ProvidersResponse:
    rows = db.query(ApiProvider).order_by(ApiProvider.id.asc()).all()
    return ProvidersResponse(providers=[_to_item(r) for r in rows])


@router.post("/providers", response_model=ProviderItem, status_code=201)
def create_provider(
    payload: ProviderCreateRequest,
    db: Session = Depends(get_db),
) -> ProviderItem:
    provider = ApiProvider(
        name=payload.name,
        base_url=payload.base_url,
        api_key=payload.api_key,
        auth_type=payload.auth_type,
    )
    db.add(provider)
    db.commit()
    db.refresh(provider)
    return _to_item(provider)


@router.put("/providers/{provider_id}", response_model=ProviderItem)
def update_provider(
    provider_id: int,
    payload: ProviderUpdateRequest,
    db: Session = Depends(get_db),
) -> ProviderItem:
    provider = db.query(ApiProvider).filter(ApiProvider.id == provider_id).first()
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")

    update_data = payload.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(provider, key, value)

    db.commit()
    db.refresh(provider)
    return _to_item(provider)


@router.delete("/providers/{provider_id}", status_code=204)
def delete_provider(
    provider_id: int,
    db: Session = Depends(get_db),
) -> None:
    provider = db.query(ApiProvider).filter(ApiProvider.id == provider_id).first()
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")

    db.delete(provider)
    db.commit()
