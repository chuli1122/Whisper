"""Signed-URL media storage for images.

Images are saved to MEDIA_DIR as files. Access is gated by HMAC-signed URLs
with an expiry timestamp. Old files are cleaned up by a periodic cron job.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
import uuid
from io import BytesIO
from pathlib import Path

logger = logging.getLogger(__name__)

MEDIA_DIR = Path(__file__).parent.parent.parent / "media"
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

_URL_EXPIRY_SECONDS = 5 * 86400  # 5 days
_IMAGE_RETENTION_SECONDS = 5 * 86400  # 5 days (images)
_FILE_RETENTION_SECONDS = 30 * 86400  # 30 days (documents)


def _signing_key() -> bytes:
    secret = os.getenv("WHISPER_SECRET") or os.getenv("WHISPER_PASSWORD") or "fallback"
    return secret.encode()


def _sign(filename: str, exp: int) -> str:
    msg = f"{filename}:{exp}".encode()
    return hmac.new(_signing_key(), msg, hashlib.sha256).hexdigest()[:32]


def save_image(file_bytes: bytes | BytesIO, ext: str = "jpg") -> str:
    """Save image bytes to disk. Returns the filename (no path)."""
    if isinstance(file_bytes, BytesIO):
        file_bytes.seek(0)
        file_bytes = file_bytes.read()

    filename = f"{uuid.uuid4().hex}.{ext}"
    (MEDIA_DIR / filename).write_bytes(file_bytes)
    return filename


def save_image_from_data_url(data_url: str) -> str:
    """Save a data:image/...;base64,... URL to disk. Returns filename."""
    import base64

    _, b64 = data_url.split(",", 1)
    raw = base64.b64decode(b64)
    return save_image(raw, "jpg")


def make_signed_url(filename: str, base_url: str = "") -> str:
    """Generate a signed URL for the given filename."""
    exp = int(time.time()) + _URL_EXPIRY_SECONDS
    sig = _sign(filename, exp)
    return f"{base_url}/api/media/{filename}?exp={exp}&sig={sig}"


def verify_signature(filename: str, exp: str, sig: str) -> bool:
    """Verify that the signature is valid and not expired."""
    try:
        exp_int = int(exp)
    except (ValueError, TypeError):
        return False
    if time.time() > exp_int:
        return False
    expected = _sign(filename, exp_int)
    return hmac.compare_digest(sig, expected)


def get_file_path(filename: str) -> Path | None:
    """Return full path if file exists, else None."""
    p = MEDIA_DIR / filename
    if p.exists() and p.is_file():
        return p
    return None


_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


def cleanup_old_files() -> int:
    """Delete expired files: images after 5 days, other files after 30 days."""
    now = time.time()
    count = 0
    for f in MEDIA_DIR.iterdir():
        if not f.is_file():
            continue
        age = now - f.stat().st_mtime
        max_age = _IMAGE_RETENTION_SECONDS if f.suffix.lower() in _IMAGE_EXTENSIONS else _FILE_RETENTION_SECONDS
        if age > max_age:
            f.unlink()
            count += 1
    return count


def compress_image_if_needed(raw: bytes, mime: str) -> tuple[bytes, str]:
    """If raw exceeds ~3.5MB, recompress as JPEG to fit Anthropic's 5MB cap.
    GIF is always converted (historical mime-mismatch caused 400; only first frame kept)."""
    threshold = 3_500_000  # raw → base64 ≈ 4.7MB, safely under 5MB cap
    if len(raw) <= threshold and mime != "image/gif":
        return raw, mime
    try:
        from PIL import Image
        im = Image.open(BytesIO(raw))
        if im.mode != 'RGB':
            im = im.convert('RGB')
        out = raw
        for max_dim, quality in ((2048, 85), (1536, 75)):
            scaled = im.copy()
            scaled.thumbnail((max_dim, max_dim))
            buf = BytesIO()
            scaled.save(buf, format='JPEG', quality=quality)
            out = buf.getvalue()
            if len(out) <= threshold:
                break
        return out, "image/jpeg"
    except Exception as e:
        logger.warning("[image-compress] failed mime=%s size=%d: %s", mime, len(raw), e)
        return raw, mime
