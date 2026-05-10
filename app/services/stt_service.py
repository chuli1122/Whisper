"""
STT Service — speech-to-text transcription.

Primary: Groq Whisper API (fast, cloud).
Fallback: Local faster-whisper via WebSocket terminal bridge.
"""
from __future__ import annotations

import base64
import logging
import subprocess
import tempfile
import os

import httpx
from zhconv import convert as _zhconv

from app.database import SessionLocal
from app.models.models import Settings
from app.services.terminal_bridge import bridge as terminal_bridge

logger = logging.getLogger(__name__)


def _to_simplified(text: str) -> str:
    """Convert Traditional Chinese to Simplified."""
    return _zhconv(text, "zh-cn")


def _convert_to_wav(audio_bytes: bytes, src_ext: str = "silk") -> tuple[bytes, str] | None:
    """Convert audio to wav using pilk (for silk) or ffmpeg. Returns (wav_bytes, 'voice.wav') or None."""
    in_path = out_path = pcm_path = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=f".{src_ext}", delete=False) as f_in:
            f_in.write(audio_bytes)
            in_path = f_in.name

        out_path = in_path.rsplit(".", 1)[0] + ".wav"

        if src_ext == "silk":
            # silk → pcm via pilk, then pcm → wav via ffmpeg (lossless)
            try:
                import pilk
                pcm_path = in_path.rsplit(".", 1)[0] + ".pcm"
                # pilk needs the raw silk data, try with and without header
                duration = pilk.decode(in_path, pcm_path)
                logger.info("[stt] pilk decoded silk → pcm, duration=%dms", duration)
                # Try multiple sample rates — QQ silk is typically 24000 but some are 16000
                for sample_rate in [24000, 16000, 48000]:
                    result = subprocess.run(
                        ["ffmpeg", "-y", "-f", "s16le", "-ar", str(sample_rate), "-ac", "1",
                         "-i", pcm_path, "-ar", "16000", "-ac", "1", out_path],
                        capture_output=True, timeout=15,
                    )
                    if result.returncode == 0 and os.path.exists(out_path):
                        out_size = os.path.getsize(out_path)
                        # Sanity check: wav should be reasonable size for the duration
                        expected_min = duration * 16  # 16000 Hz * 2 bytes * duration_s / 1000
                        if out_size > expected_min * 0.5:
                            logger.info("[stt] silk→wav with sample_rate=%d, size=%d", sample_rate, out_size)
                            break
                else:
                    # All rates failed sanity, use the last result
                    pass
            except ImportError:
                logger.warning("[stt] pilk not installed, trying ffmpeg direct")
                result = subprocess.run(
                    ["ffmpeg", "-y", "-i", in_path, "-ar", "16000", "-ac", "1", out_path],
                    capture_output=True, timeout=15,
                )
        else:
            result = subprocess.run(
                ["ffmpeg", "-y", "-i", in_path, "-ar", "16000", "-ac", "1", out_path],
                capture_output=True, timeout=15,
            )

        if result.returncode == 0 and os.path.exists(out_path):
            with open(out_path, "rb") as f_out:
                wav_bytes = f_out.read()
            if wav_bytes:
                logger.info("[stt] Converted %s to wav (%d bytes)", src_ext, len(wav_bytes))
                return wav_bytes, "voice.wav"
        else:
            logger.warning("[stt] ffmpeg conversion failed: %s",
                           (result.stderr or b"")[:300].decode(errors="replace"))
    except Exception as e:
        logger.warning("[stt] audio conversion error: %s", e)
    finally:
        for p in [in_path, out_path, pcm_path]:
            if p:
                try:
                    os.unlink(p)
                except Exception:
                    pass
    return None


def transcribe(audio_bytes: bytes, file_name: str = "voice.ogg") -> str | None:
    """Transcribe audio to text. Returns text or None on failure."""
    if not audio_bytes:
        return None

    # Convert unsupported formats (silk, amr, etc.) to wav
    ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
    if ext not in ("ogg", "mp3", "wav", "m4a", "webm", "flac"):
        converted = _convert_to_wav(audio_bytes, ext or "silk")
        if converted:
            audio_bytes, file_name = converted
        else:
            logger.warning("[stt] Could not convert %s, trying raw upload", ext)

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
