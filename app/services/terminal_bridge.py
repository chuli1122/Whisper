"""
Terminal Bridge — manages WebSocket connection to a remote PC terminal.

The PC runs a client that connects to /ws/terminal. When the model calls
run_bash/read_file/write_file from Telegram, commands are routed through
this bridge to the PC and results are returned synchronously.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import uuid
from datetime import datetime

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class TerminalBridge:
    def __init__(self):
        self._ws: WebSocket | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pending: dict[str, dict] = {}  # {req_id: {"event": Event, "result": str}}
        self._connected_at: datetime | None = None

    def set_connection(self, ws: WebSocket, loop: asyncio.AbstractEventLoop) -> None:
        self._ws = ws
        self._loop = loop
        self._connected_at = datetime.now()
        logger.info("[terminal] PC connected")

    def clear_connection(self) -> None:
        # Cancel any pending requests
        for req_id, entry in list(self._pending.items()):
            entry["result"] = json.dumps({"error": "终端断开连接"})
            entry["event"].set()
        self._ws = None
        self._loop = None
        self._connected_at = None
        logger.info("[terminal] PC disconnected")

    def is_online(self) -> bool:
        return self._ws is not None

    def execute(self, tool_name: str, arguments: dict, timeout: float = 120) -> dict:
        """
        Synchronous — called from tool execution thread.
        Sends command to PC via WebSocket and waits for result.
        """
        if not self._ws or not self._loop:
            return {"error": "终端离线，无法执行"}

        req_id = str(uuid.uuid4())
        event = threading.Event()
        self._pending[req_id] = {"event": event, "result": None}

        msg = json.dumps({"id": req_id, "tool": tool_name, "arguments": arguments})
        try:
            asyncio.run_coroutine_threadsafe(self._ws.send_text(msg), self._loop)
        except Exception as e:
            self._pending.pop(req_id, None)
            return {"error": f"发送命令失败: {e}"}

        if not event.wait(timeout=timeout):
            self._pending.pop(req_id, None)
            return {"error": f"终端执行超时（{timeout}秒）"}

        raw = self._pending.pop(req_id, {}).get("result")
        if raw is None:
            return {"error": "未收到结果"}
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {"output": raw}

    def on_result(self, req_id: str, result: str) -> None:
        """Called by WebSocket handler when PC sends back a result."""
        entry = self._pending.get(req_id)
        if entry:
            entry["result"] = result
            entry["event"].set()
        else:
            logger.warning("[terminal] Received result for unknown request %s", req_id)


# Global singleton
bridge = TerminalBridge()
