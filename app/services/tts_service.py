"""
TTS Service — MiniMax T2A v2 text-to-speech.

Reads API credentials from the Settings table.
Returns audio bytes on success, None on failure.
"""
from __future__ import annotations

import logging
import re

import httpx

from app.database import SessionLocal
from app.models.models import Settings

logger = logging.getLogger(__name__)

VALID_EMOTIONS = {"happy", "sad", "angry", "fearful", "disgusted", "surprised", "neutral"}
EMOTION_TAG_RE = re.compile(r"\[\[voice:(\w+)\]\]")


def parse_emotion_tag(content: str) -> tuple[str, str | None]:
    """Extract and strip [[voice:EMOTION]] from content.
    Returns (clean_content, emotion_or_None)."""
    match = EMOTION_TAG_RE.search(content)
    if not match:
        return content, None
    emotion = match.group(1).lower()
    clean = EMOTION_TAG_RE.sub("", content).strip()
    return clean, emotion if emotion in VALID_EMOTIONS else None


def _read_tts_settings() -> dict[str, str]:
    db = SessionLocal()
    try:
        keys = ["tts_api_key", "tts_group_id", "tts_voice_id", "tts_model"]
        rows = db.query(Settings).filter(Settings.key.in_(keys)).all()
        return {r.key: r.value for r in rows}
    finally:
        db.close()


def synthesize(text: str, emotion: str | None = None) -> bytes | None:
    """Call MiniMax T2A v2 API. Returns MP3 audio bytes or None on failure."""
    if not text or not text.strip():
        return None

    kv = _read_tts_settings()
    api_key = kv.get("tts_api_key", "")
    group_id = kv.get("tts_group_id", "")
    voice_id = kv.get("tts_voice_id", "")
    model = kv.get("tts_model", "") or "speech-02-hd"

    if not api_key or not group_id or not voice_id:
        logger.debug("[tts] Missing API credentials, skipping")
        return None

    url = f"https://api.minimax.io/v1/t2a_v2?GroupId={group_id}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    voice_setting: dict = {
        "voice_id": voice_id,
        "speed": 1.0,
        "vol": 1.0,
        "pitch": 0,
    }
    if emotion and emotion in VALID_EMOTIONS:
        voice_setting["emotion"] = emotion

    payload = {
        "model": model,
        "text": text,
        "stream": False,
        "voice_setting": voice_setting,
        "audio_setting": {
            "sample_rate": 32000,
            "bitrate": 128000,
            "format": "mp3",
            "channel": 1,
        },
    }

    try:
        with httpx.Client(timeout=30) as client:
            resp = client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()

        status_code = data.get("base_resp", {}).get("status_code", -1)
        if status_code != 0:
            logger.error("[tts] MiniMax API error: %s", data.get("base_resp"))
            return None

        audio_hex = data.get("data", {}).get("audio", "")
        if not audio_hex:
            logger.error("[tts] No audio data in response")
            return None

        audio_bytes = bytes.fromhex(audio_hex)
        logger.info("[tts] Synthesized %d bytes (emotion=%s)", len(audio_bytes), emotion)
        return audio_bytes

    except Exception as e:
        logger.error("[tts] Synthesis failed: %s", e)
        return None
