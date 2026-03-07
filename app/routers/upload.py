from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile

router = APIRouter()

UPLOAD_DIR = Path(__file__).parent.parent.parent / "static" / "uploads"
ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
MAX_SIZE = 10 * 1024 * 1024  # 10 MB


@router.post("/upload-image")
async def upload_image(file: UploadFile) -> dict:
    if file.content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(status_code=400, detail="只支持图片格式 (jpeg/png/gif/webp)")

    data = await file.read()
    if len(data) > MAX_SIZE:
        raise HTTPException(status_code=400, detail="图片不能超过 10MB")

    ext = (file.filename or "image").rsplit(".", 1)[-1].lower()
    if ext not in {"jpg", "jpeg", "png", "gif", "webp"}:
        ext = "jpg"

    filename = f"{uuid.uuid4().hex}.{ext}"
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    (UPLOAD_DIR / filename).write_bytes(data)

    return {"url": f"/static/uploads/{filename}"}
