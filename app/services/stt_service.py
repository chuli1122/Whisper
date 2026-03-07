"""
STT Service — speech-to-text transcription.

Primary: Groq Whisper API (fast, cloud).
Fallback: Local faster-whisper via WebSocket terminal bridge.
"""
from __future__ import annotations

import base64
import logging

import httpx
from zhconv import convert as _zhconv

from app.database import SessionLocal
from app.models.models import Settings
from app.services.terminal_bridge import bridge as terminal_bridge

logger = logging.getLogger(__name__)


def _to_simplified(text: str) -> str:
    """Convert Traditional Chinese to Simplified."""
    return _zhconv(text, "zh-cn")


def transcribe(audio_bytes: bytes, file_name: str = "voice.ogg") -> str | None:
    """Transcribe audio to text. Returns text or None on failure."""
    if not audio_bytes:
        return None

    # Path 1: Groq API (fast, primary)
    try:
        result = _transcribe_via_groq(audio_bytes, file_name)
        if result:
            return result
    except Exception as e:
        logger.warning("[stt] Groq transcription failed: %s", e)

    # Path 2: Local faster-whisper via terminal bridge (fallback)
    if terminal_bridge.is_online():
        try:
            return _transcribe_via_terminal(audio_bytes, file_name)
        except Exception as e:
            logger.error("[stt] Terminal transcription failed: %s", e)

    return None


def _transcribe_via_terminal(audio_bytes: bytes, file_name: str) -> str | None:
    b64 = base64.b64encode(audio_bytes).decode("ascii")
    result = terminal_bridge.execute(
        "transcribe",
        {"audio_base64": b64, "file_name": file_name},
        timeout=300,
    )
    if isinstance(result, dict):
        if "error" in result:
            logger.warning("[stt] Terminal error: %s", result["error"])
            return None
        text = result.get("text", "").strip()
        if text:
            text = _to_simplified(text)
            logger.info("[stt] Terminal transcribed (%d chars): %s", len(text), text[:60])
            return text
    return None


def _transcribe_via_groq(audio_bytes: bytes, file_name: str) -> str | None:
    db = SessionLocal()
    try:
        row = db.query(Settings).filter(Settings.key == "stt_fallback_key").first()
        api_key = row.value if row else ""
    finally:
        db.close()

    if not api_key:
        logger.debug("[stt] No Groq API key configured, cannot fallback")
        return None

    url = "https://api.groq.com/openai/v1/audio/transcriptions"
    headers = {"Authorization": f"Bearer {api_key}"}

    # Determine MIME type
    ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else "ogg"
    mime_map = {"ogg": "audio/ogg", "mp3": "audio/mpeg", "wav": "audio/wav", "m4a": "audio/mp4"}
    mime = mime_map.get(ext, "audio/ogg")

    files = {"file": (file_name, audio_bytes, mime)}
    data = {"model": "whisper-large-v3-turbo", "language": "zh"}

    with httpx.Client(timeout=30) as client:
        resp = client.post(url, headers=headers, files=files, data=data)
        resp.raise_for_status()
        text = resp.json().get("text", "").strip()

    if text:
        text = _to_simplified(text)
        logger.info("[stt] Groq transcribed (%d chars): %s", len(text), text[:60])
        return text
    return None
