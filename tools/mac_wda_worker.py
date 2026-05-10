#!/usr/bin/env python3
"""Mac-local WDA worker for phone screenshots and controls.

This script is invoked through the terminal bridge on the Mac. It talks to WDA
on the local Wi-Fi URL, then returns compact JSON to the VPS backend.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


STATE_DIR = Path.home() / ".local/state/wda-helper"
WDA_URL_FILE = STATE_DIR / "wda_url"
ENABLED_FILE = STATE_DIR / "enabled"
WDA_HELPER = Path.home() / ".local/bin/wda-helper"
TRIGGER_LOG = Path.home() / "Library/Logs/wda-helper/trigger.log"
DEFAULT_WDA_URL = os.environ.get("WDA_BASE_URL", "http://localhost:8100")
HTTP_TIMEOUT = 12


class WdaClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.session_id: str | None = None

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: int = HTTP_TIMEOUT,
    ) -> Any:
        body = None
        headers = {}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        request = urllib.request.Request(
            self._url(path),
            data=body,
            headers=headers,
            method=method,
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
        if not raw:
            return None
        return json.loads(raw.decode("utf-8"))

    def status(self) -> dict[str, Any]:
        data = self._request("GET", "/status", timeout=5)
        value = data.get("value", {}) if isinstance(data, dict) else {}
        return {"online": True, "ready": bool(value.get("ready")), "value": value}

    def screenshot(self) -> bytes:
        data = self._request("GET", "/screenshot", timeout=HTTP_TIMEOUT)
        return base64.b64decode(data["value"])

    def ensure_session(self) -> str:
        if self.session_id:
            try:
                self._request("GET", f"/session/{self.session_id}/window/size", timeout=5)
                return self.session_id
            except Exception:
                self.session_id = None
        data = self._request(
            "POST",
            "/session",
            {"capabilities": {"alwaysMatch": {}}},
            timeout=30,
        )
        self.session_id = data.get("sessionId") or data["value"]["sessionId"]
        return self.session_id

    def session_path(self, path: str) -> str:
        return f"/session/{self.ensure_session()}{path}"

    def tap(self, x: int | float, y: int | float) -> bool:
        body = {
            "actions": [{
                "type": "pointer",
                "id": "finger1",
                "parameters": {"pointerType": "touch"},
                "actions": [
                    {"type": "pointerMove", "duration": 0, "x": x, "y": y},
                    {"type": "pointerDown", "button": 0},
                    {"type": "pause", "duration": 50},
                    {"type": "pointerUp", "button": 0},
                ],
            }],
        }
        self._request("POST", self.session_path("/actions"), body)
        return True

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration: int = 500) -> bool:
        body = {
            "actions": [{
                "type": "pointer",
                "id": "finger1",
                "parameters": {"pointerType": "touch"},
                "actions": [
                    {"type": "pointerMove", "duration": 0, "x": x1, "y": y1},
                    {"type": "pointerDown", "button": 0},
                    {"type": "pointerMove", "duration": duration, "x": x2, "y": y2},
                    {"type": "pointerUp", "button": 0},
                ],
            }],
        }
        self._request("POST", self.session_path("/actions"), body)
        return True

    def type_text(self, text: str, element_type: str = "XCUIElementTypeTextView") -> bool:
        element_id = None
        for class_name in (element_type, "XCUIElementTypeTextField", "XCUIElementTypeSearchField"):
            data = self._request(
                "POST",
                self.session_path("/element"),
                {"using": "class name", "value": class_name},
            )
            value = data.get("value", {}) if isinstance(data, dict) else {}
            element_id = value.get("element-6066-11e4-a52e-4f735466cecf") or value.get("ELEMENT")
            if element_id:
                break
        if not element_id:
            return False
        self._request(
            "POST",
            self.session_path(f"/element/{element_id}/value"),
            {"value": [text]},
        )
        return True

    def press_home(self) -> bool:
        self._request("POST", "/wda/homescreen")
        return True

    def get_source(self) -> str:
        data = self._request("GET", self.session_path("/source"))
        return data.get("value", "") if isinstance(data, dict) else ""


def wda_url() -> str:
    try:
        value = WDA_URL_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        value = ""
    return value or os.environ.get("WDA_BASE_URL") or DEFAULT_WDA_URL


def result(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def run_wda_helper(*args: str, timeout: int = 20) -> dict[str, Any]:
    completed = subprocess.run(
        [str(WDA_HELPER), *args],
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    return {
        "exit_code": completed.returncode,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
    }


def trigger_wda_helper_start() -> dict[str, Any]:
    TRIGGER_LOG.parent.mkdir(parents=True, exist_ok=True)
    log = TRIGGER_LOG.open("ab")
    try:
        process = subprocess.Popen(
            [str(WDA_HELPER), "start"],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        return {"pid": process.pid, "log": str(TRIGGER_LOG)}
    finally:
        log.close()


def clear_enabled_flag() -> dict[str, Any]:
    try:
        ENABLED_FILE.unlink(missing_ok=True)
        return {"status": "ok", "path": str(ENABLED_FILE)}
    except Exception as exc:
        return {"status": "error", "path": str(ENABLED_FILE), "message": str(exc)}


def compress_screenshot(raw: bytes) -> tuple[bytes, str]:
    with tempfile.TemporaryDirectory(prefix="wda-helper-shot-") as tmpdir:
        source = Path(tmpdir) / "source.png"
        target = Path(tmpdir) / "screen.jpg"
        source.write_bytes(raw)
        completed = subprocess.run(
            [
                "/usr/bin/sips",
                "-s",
                "format",
                "jpeg",
                "-s",
                "formatOptions",
                "65",
                "-Z",
                "1280",
                str(source),
                "--out",
                str(target),
            ],
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        if completed.returncode == 0 and target.exists() and target.stat().st_size > 0:
            return target.read_bytes(), "jpg"
    return raw, "png"


def start_wda(timeout_seconds: int = 55) -> dict[str, Any]:
    enable = run_wda_helper("enable", timeout=10)
    trigger = trigger_wda_helper_start()
    deadline = time.monotonic() + timeout_seconds
    last_error: str | None = None
    while time.monotonic() < deadline:
        try:
            status = WdaClient(wda_url()).status()
            if status.get("online") and status.get("ready"):
                clear_enabled = clear_enabled_flag()
                touch = run_wda_helper("touch", timeout=10)
                return {
                    "status": "ok",
                    "online": True,
                    "ready": True,
                    "enable": enable,
                    "trigger": trigger,
                    "clear_enabled": clear_enabled,
                    "touch": touch,
                }
        except Exception as exc:
            last_error = str(exc)
        time.sleep(2)
    clear_enabled = clear_enabled_flag()
    return {
        "status": "error",
        "message": "WDA did not become ready before timeout",
        "enable": enable,
        "trigger": trigger,
        "clear_enabled": clear_enabled,
        "last_error": last_error,
    }


def execute(payload: dict[str, Any]) -> dict[str, Any]:
    action = payload.get("action", "status")
    client = WdaClient(wda_url())
    started = time.monotonic()

    if action == "start":
        return start_wda(int(payload.get("timeout_seconds", 55)))

    if action == "stop":
        stop = run_wda_helper("stop", timeout=20)
        return {"status": "ok" if stop["exit_code"] == 0 else "error", "stop": stop}

    if action == "status":
        status = client.status()
        status["status"] = "ok"
        status["wda_url"] = client.base_url
        return status

    if action == "screenshot":
        raw = client.screenshot()
        compressed, ext = compress_screenshot(raw)
        run_wda_helper("touch", timeout=10)
        return {
            "status": "ok",
            "format": ext,
            "bytes": len(compressed),
            "source_bytes": len(raw),
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "image_base64": base64.b64encode(compressed).decode("ascii"),
        }

    if action == "tap":
        ok = client.tap(payload.get("x", 0), payload.get("y", 0))
        run_wda_helper("touch", timeout=10)
        return {"status": "ok" if ok else "error", "action": "tap"}

    if action == "swipe":
        ok = client.swipe(
            int(payload.get("x", 0)),
            int(payload.get("y", 0)),
            int(payload.get("x2", 0)),
            int(payload.get("y2", 0)),
            int(payload.get("duration", 500)),
        )
        run_wda_helper("touch", timeout=10)
        return {"status": "ok" if ok else "error", "action": "swipe"}

    if action == "type_text":
        ok = client.type_text(str(payload.get("text", "")), str(payload.get("element_type", "XCUIElementTypeTextView")))
        run_wda_helper("touch", timeout=10)
        return {"status": "ok" if ok else "error", "action": "type_text"}

    if action == "press_home":
        ok = client.press_home()
        run_wda_helper("touch", timeout=10)
        return {"status": "ok" if ok else "error", "action": "press_home"}

    if action == "get_source":
        source = client.get_source()
        run_wda_helper("touch", timeout=10)
        return {"status": "ok", "source": source}

    return {"status": "error", "message": f"unknown action: {action}"}


def main() -> int:
    if len(sys.argv) > 1:
        raw = sys.argv[1]
    else:
        raw = sys.stdin.read()
    try:
        payload = json.loads(raw or "{}")
        result(execute(payload))
        return 0
    except (urllib.error.URLError, TimeoutError, subprocess.TimeoutExpired) as exc:
        result({"status": "error", "message": str(exc), "kind": exc.__class__.__name__})
        return 1
    except Exception as exc:
        result({"status": "error", "message": str(exc), "kind": exc.__class__.__name__})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
