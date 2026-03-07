from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.maintenance_service import MaintenanceService

logger = logging.getLogger(__name__)
router = APIRouter()


class MaintenanceRunResponse(BaseModel):
    expired_cleaned: int
    similar_merged: int
    trash_cleaned: int


@router.post("/maintenance/run", response_model=MaintenanceRunResponse)
def run_maintenance(db: Session = Depends(get_db)) -> MaintenanceRunResponse:
    payload = MaintenanceService(db).run_all()
    return MaintenanceRunResponse(**payload)
