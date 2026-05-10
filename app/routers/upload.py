from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, UploadFile

from app.services.media_service import make_signed_url, save_image

router = APIRouter()

ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
MAX_SIZE = 10 * 1024 * 1024  # 10 MB


@router.post("/upload-image")
async def upload_image(file: UploadFile, request: Request) -> dict:
    if file.content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(status_code=400, detail="只支持图片格式 (jpeg/png/gif/webp)")

    data = await file.read()
    if len(data) > MAX_SIZE:
        raise HTTPException(status_code=400, detail="图片不能超过 10MB")

    ext = (file.filename or "image").rsplit(".", 1)[-1].lower()
    if ext not in {"jpg", "jpeg", "png", "gif", "webp"}:
        ext = "jpg"

    filename = save_image(data, ext)
    base_url = str(request.base_url).rstrip("/")
    url = make_signed_url(filename, base_url)

    return {"url": url, "media_ref": f"media:{filename}"}
