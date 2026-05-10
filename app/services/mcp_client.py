"""MCP over SSE client (generic).

Connects to an MCP server's /mcp/sse endpoint, performs one tool call, returns
the text result. Used by forum_cli/forum_guide today; future MCPs can reuse the
same helper by passing a different endpoint URL.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from queue import Empty, Queue
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 30.0


def call(
    endpoint: str,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Perform a single MCP tool call over SSE.

    endpoint: full SSE URL, e.g. https://daskio.de5.net/mcp/sse
              or short-code form https://host/mcp/<code>/sse
    Returns {"result": "...text..."} on success, {"error": "..."} on failure.
    """
    if not endpoint.startswith(("http://", "https://")):
        return {"error": f"invalid MCP endpoint: {endpoint}"}
    scheme_host = endpoint.split("/", 3)
    if len(scheme_host) < 4:
        return {"error": f"invalid MCP endpoint: {endpoint}"}
    base = "/".join(scheme_host[:3])

    queue: Queue[tuple[str, str]] = Queue()
    stop_flag = threading.Event()

    def _read_sse() -> None:
        try:
            with httpx.stream(
                "GET",
                endpoint,
                headers={"Accept": "text/event-stream"},
                timeout=httpx.Timeout(timeout, read=timeout * 2),
            ) as resp:
                current_data: list[str] = []
                for line in resp.iter_lines():
                    if stop_flag.is_set():
                        return
                    if not line:
                        if current_data:
                            queue.put(("data", "\n".join(current_data)))
                            current_data = []
                        continue
                    if line.startswith("data:"):
                        current_data.append(line[5:].lstrip())
        except Exception as exc:
            queue.put(("error", str(exc)))

    thread = threading.Thread(target=_read_sse, daemon=True)
    thread.start()

    try:
        messages_path = _wait_for_endpoint(queue, timeout)
        if not messages_path:
            return {"error": "MCP: no endpoint received"}

        msg_url = base + messages_path
        call_id = str(uuid.uuid4())

        with httpx.Client(timeout=timeout) as client:
            init_resp = client.post(msg_url, json={
                "jsonrpc": "2.0",
                "id": "init",
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "yoru-mcp", "version": "0.1"},
                },
            })
            if init_resp.status_code != 202:
                return {"error": f"MCP initialize failed: HTTP {init_resp.status_code}"}
            client.post(msg_url, json={
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            })
            call_resp = client.post(msg_url, json={
                "jsonrpc": "2.0",
                "id": call_id,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments},
            })
            if call_resp.status_code != 202:
                return {"error": f"MCP tools/call failed: HTTP {call_resp.status_code}"}

        return _wait_for_response(queue, call_id, timeout)
    finally:
        stop_flag.set()


def _wait_for_endpoint(queue: Queue[tuple[str, str]], timeout: float) -> str | None:
    """Read the initial `endpoint` SSE event to learn the messages path.

    The server sends something like `data: /mcp/<code>/messages/?session_id=xxx`.
    Returning the full path preserves any short-code prefix.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            kind, payload = queue.get(timeout=0.5)
        except Empty:
            continue
        if kind == "error":
            logger.warning("[mcp_client] SSE error before endpoint: %s", payload)
            return None
        if payload.startswith("/") and "session_id=" in payload:
            return payload
    return None


def _wait_for_response(queue: Queue[tuple[str, str]], call_id: str, timeout: float) -> dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            kind, payload = queue.get(timeout=0.5)
        except Empty:
            continue
        if kind == "error":
            return {"error": f"MCP SSE error: {payload}"}
        try:
            data = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            continue
        if data.get("id") != call_id:
            continue
        if "error" in data:
            err = data["error"]
            msg = err.get("message") if isinstance(err, dict) else str(err)
            return {"error": f"MCP error: {msg}"}
        result = data.get("result") or {}
        contents = result.get("content") or []
        texts = [c.get("text", "") for c in contents if isinstance(c, dict) and c.get("type") == "text"]
        return {"result": "\n".join(texts)}
    return {"error": "MCP call timeout"}
