"""
NapCat OneBot v11 HTTP API wrapper.
"""
from __future__ import annotations

import base64
import logging

import httpx

from .config import NAPCAT_API_URL

logger = logging.getLogger(__name__)

_TIMEOUT = 15.0


async def send_private_msg(user_id: int, text: str) -> int | None:
    """Send a private text message. Returns message_id or None."""
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(f"{NAPCAT_API_URL}/send_private_msg", json={
            "user_id": user_id,
            "message": [{"type": "text", "data": {"text": text}}],
        })
        resp.raise_for_status()
        data = resp.json().get("data") or {}
        return data.get("message_id")


async def send_group_msg(group_id: int, text: str, reply_to: int | None = None) -> int | None:
    """Send a group text message. Optionally quote-reply to a message_id. Returns message_id or None."""
    segments: list[dict] = []
    if reply_to:
        segments.append({"type": "reply", "data": {"id": str(reply_to)}})
    segments.append({"type": "text", "data": {"text": text}})
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(f"{NAPCAT_API_URL}/send_group_msg", json={
            "group_id": group_id,
            "message": segments,
        })
        resp.raise_for_status()
        data = resp.json().get("data") or {}
        return data.get("message_id")


async def send_group_voice(group_id: int, audio_bytes: bytes) -> int | None:
    """Send a group voice message as base64-encoded record."""
    b64 = base64.b64encode(audio_bytes).decode("ascii")
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(f"{NAPCAT_API_URL}/send_group_msg", json={
            "group_id": group_id,
            "message": [{"type": "record", "data": {"file": f"base64://{b64}"}}],
        })
        resp.raise_for_status()
        data = resp.json().get("data") or {}
        return data.get("message_id")


async def send_private_voice(user_id: int, audio_bytes: bytes) -> int | None:
    """Send a voice message as base64-encoded record."""
    b64 = base64.b64encode(audio_bytes).decode("ascii")
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(f"{NAPCAT_API_URL}/send_private_msg", json={
            "user_id": user_id,
            "message": [{"type": "record", "data": {"file": f"base64://{b64}"}}],
        })
        resp.raise_for_status()
        data = resp.json().get("data") or {}
        return data.get("message_id")


async def send_private_image(user_id: int, image_bytes: bytes) -> int | None:
    """Send an image as base64."""
    b64 = base64.b64encode(image_bytes).decode("ascii")
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(f"{NAPCAT_API_URL}/send_private_msg", json={
            "user_id": user_id,
            "message": [{"type": "image", "data": {"file": f"base64://{b64}"}}],
        })
        resp.raise_for_status()
        data = resp.json().get("data") or {}
        return data.get("message_id")


async def download_file(url: str) -> bytes:
    """Download a file (voice/image) from NapCat's cache URL."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.content
