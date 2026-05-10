from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from app.services.media_service import get_file_path, verify_signature

router = APIRouter()

_MIME_MAP = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


@router.get("/media/{filename}")
def serve_media(
    filename: str,
    exp: str = Query(...),
    sig: str = Query(...),
):
    if not verify_signature(filename, exp, sig):
        raise HTTPException(status_code=403, detail="Invalid or expired link")

    path = get_file_path(filename)
    if not path:
        raise HTTPException(status_code=404, detail="File not found")

    mime = _MIME_MAP.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(path, media_type=mime)
