"""
Terminal Bridge — manages WebSocket connections to multiple remote terminals.

Each device connects to /ws/terminal?token=...&name=win|mac.
Commands are routed to a specific device or the first available one.
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


class _DeviceConn:
    __slots__ = ("ws", "loop", "connected_at")

    def __init__(self, ws: WebSocket, loop: asyncio.AbstractEventLoop):
        self.ws = ws
        self.loop = loop
        self.connected_at = datetime.now()


class TerminalBridge:
    def __init__(self):
        self._devices: dict[str, _DeviceConn] = {}
        self._pending: dict[str, dict] = {}

    def set_connection(self, name: str, ws: WebSocket, loop: asyncio.AbstractEventLoop) -> None:
        if name == "default":
            name = "win"
        self._devices[name] = _DeviceConn(ws, loop)
        logger.info("[terminal] %s connected", name)

    def clear_connection(self, name: str) -> None:
        if name == "default":
            name = "win"
        self._devices.pop(name, None)
        for req_id, entry in list(self._pending.items()):
            if entry.get("device") == name:
                entry["result"] = json.dumps({"error": f"{name} 断开连接"})
                entry["event"].set()
        logger.info("[terminal] %s disconnected", name)

    def is_online(self, device: str | None = None) -> bool:
        if device:
            return device in self._devices
        return len(self._devices) > 0

    def online_devices(self) -> list[str]:
        return list(self._devices.keys())

    def execute(self, tool_name: str, arguments: dict, timeout: float = 120, device: str | None = None) -> dict:
        if device:
            conn = self._devices.get(device)
            if not conn:
                return {"error": f"{device} 不在线"}
        else:
            if not self._devices:
                return {"error": "没有终端在线"}
            device = next(iter(self._devices))
            conn = self._devices[device]

        req_id = str(uuid.uuid4())
        event = threading.Event()
        self._pending[req_id] = {"event": event, "result": None, "device": device}

        msg = json.dumps({"id": req_id, "tool": tool_name, "arguments": arguments})
        try:
            asyncio.run_coroutine_threadsafe(conn.ws.send_text(msg), conn.loop)
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
        entry = self._pending.get(req_id)
        if entry:
            entry["result"] = result
            entry["event"].set()
        else:
            logger.warning("[terminal] Received result for unknown request %s", req_id)


bridge = TerminalBridge()
