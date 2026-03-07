from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class CotBroadcaster:
    """Thread-safe broadcaster: sync code can call publish() to push COT data to WebSocket clients."""

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        # Accumulated state for active (in-flight) requests so new clients
        # can receive content that was streamed before they connected.
        self._active: dict[str, dict[str, Any]] = {}

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def connect(self, ws: WebSocket) -> None:
        self._clients.add(ws)
        logger.info("COT WS client connected (%d total)", len(self._clients))

    def disconnect(self, ws: WebSocket) -> None:
        self._clients.discard(ws)
        logger.info("COT WS client disconnected (%d total)", len(self._clients))

    # ── State tracking ──

    def _track(self, data: dict[str, Any]) -> None:
        """Accumulate streaming state for active requests (called from publish)."""
        request_id = data.get("request_id")
        msg_type = data.get("type")
        if not request_id or not msg_type:
            return

        if msg_type == "done":
            self._active.pop(request_id, None)
            return

        if request_id not in self._active:
            self._active[request_id] = {
                "thinking": {},       # round_index -> accumulated text
                "text": "",           # accumulated reply text
                "injected_memories": None,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cache_hit": False,
            }
        st = self._active[request_id]

        if msg_type == "thinking_delta":
            ri = data.get("round_index", 0)
            st["thinking"][ri] = st["thinking"].get(ri, "") + data.get("content", "")
        elif msg_type == "text_delta":
            st["text"] += data.get("content", "")
        elif msg_type == "injected_memories":
            st["injected_memories"] = data.get("memories")
        elif msg_type == "tokens_update":
            st["prompt_tokens"] = data.get("prompt_tokens", 0)
            st["completion_tokens"] = data.get("completion_tokens", 0)
            st["cache_hit"] = data.get("cache_hit", False)

    # ── Replay to a newly connected client ──

    async def replay_to(self, ws: WebSocket) -> None:
        """Send accumulated state of all active requests to *ws*."""
        if self._active:
            logger.info("[COT replay] %d active request(s) to replay", len(self._active))
        for request_id, st in list(self._active.items()):
            logger.info(
                "[COT replay] request=%s thinking_rounds=%d text_len=%d",
                request_id[:8], len(st["thinking"]), len(st["text"]),
            )
            snapshot: dict[str, Any] = {
                "type": "replay_snapshot",
                "request_id": request_id,
                "rounds": [
                    {"round_index": ri, "thinking": text}
                    for ri, text in sorted(st["thinking"].items())
                ],
                "text_preview": st["text"],
                "injected_memories": st.get("injected_memories"),
                "prompt_tokens": st.get("prompt_tokens", 0),
                "completion_tokens": st.get("completion_tokens", 0),
                "cache_hit": st.get("cache_hit", False),
            }
            try:
                await ws.send_text(json.dumps(snapshot, ensure_ascii=False))
            except Exception:
                break

    # ── Publish ──

    def publish(self, data: dict[str, Any]) -> None:
        """Called from any thread. Schedules async broadcast on the event loop."""
        self._track(data)
        if not self._clients or not self._loop:
            return
        try:
            self._loop.call_soon_threadsafe(
                self._loop.create_task, self._broadcast(data)
            )
        except RuntimeError:
            pass  # loop closed

    async def _broadcast(self, data: dict[str, Any]) -> None:
        payload = json.dumps(data, ensure_ascii=False)
        dead: list[WebSocket] = []
        for ws in list(self._clients):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._clients.discard(ws)


cot_broadcaster = CotBroadcaster()
