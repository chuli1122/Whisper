from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.chat_service import MemoryService

router = APIRouter()


@router.get("/messages/search")
async def search_messages(
    query: str = Query(...),
    limit: int = Query(20),
    session_id: int | None = Query(None),
    db: Session = Depends(get_db),
) -> dict:
    memory_service = MemoryService(db)
    payload = {"query": query, "limit": limit, "session_id": session_id}
    return memory_service.search_chat_history(payload)
