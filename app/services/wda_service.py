"""WDA (WebDriverAgent) iPhone 控制服务

通过已配置的 WDA HTTP 地址连接 iPhone，提供截图、点击、滑动、输入等能力。
WDA 通常由 Mac 侧 helper 自动启动，并通过 Mac Tailscale 代理暴露给后端。
"""
from __future__ import annotations

import base64
import json
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

WDA_TIMEOUT = 15
_SESSION_TIMEOUT = 30


class WdaService:
    def __init__(self, base_url: str):
        self._base = base_url.rstrip("/")
        self._session_id: str | None = None

    # ── 连接管理 ──

    def _url(self, path: str) -> str:
        return f"{self._base}{path}"

    def _session_url(self, path: str) -> str:
        if not self._session_id:
            self._ensure_session()
        return f"{self._base}/session/{self._session_id}{path}"

    def _ensure_session(self) -> None:
        if self._session_id:
            try:
                r = httpx.get(
                    f"{self._base}/session/{self._session_id}/window/size",
                    timeout=5,
                )
                if r.status_code == 200:
                    return
            except Exception:
                pass
            self._session_id = None

        r = httpx.post(
            self._url("/session"),
            json={"capabilities": {"alwaysMatch": {}}},
            timeout=_SESSION_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        self._session_id = data.get("sessionId") or data["value"]["sessionId"]
        logger.info("[wda] session created: %s", self._session_id)

    def status(self) -> dict[str, Any]:
        try:
            r = httpx.get(self._url("/status"), timeout=5)
            data = r.json()
            return {"online": True, "ready": data.get("value", {}).get("ready", False)}
        except Exception as e:
            return {"online": False, "error": str(e)}

    # ── 截图 ──

    def screenshot(self) -> bytes | None:
        try:
            r = httpx.get(self._url("/screenshot"), timeout=WDA_TIMEOUT)
            data = r.json()
            return base64.b64decode(data["value"])
        except Exception:
            logger.exception("[wda] screenshot failed")
            return None

    def screenshot_b64(self) -> str | None:
        try:
            r = httpx.get(self._url("/screenshot"), timeout=WDA_TIMEOUT)
            data = r.json()
            return data["value"]
        except Exception:
            logger.exception("[wda] screenshot failed")
            return None

    # ── 操作 ──

    def tap(self, x: int | float, y: int | float) -> bool:
        try:
            self._ensure_session()
            body = {
                "actions": [{
                    "type": "pointer", "id": "finger1",
                    "parameters": {"pointerType": "touch"},
                    "actions": [
                        {"type": "pointerMove", "duration": 0, "x": x, "y": y},
                        {"type": "pointerDown", "button": 0},
                        {"type": "pause", "duration": 50},
                        {"type": "pointerUp", "button": 0},
                    ],
                }],
            }
            r = httpx.post(self._session_url("/actions"), json=body, timeout=WDA_TIMEOUT)
            return r.status_code == 200
        except Exception:
            logger.exception("[wda] tap failed")
            return False

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration: int = 500) -> bool:
        try:
            self._ensure_session()
            body = {
                "actions": [{
                    "type": "pointer", "id": "finger1",
                    "parameters": {"pointerType": "touch"},
                    "actions": [
                        {"type": "pointerMove", "duration": 0, "x": x1, "y": y1},
                        {"type": "pointerDown", "button": 0},
                        {"type": "pointerMove", "duration": duration, "x": x2, "y": y2},
                        {"type": "pointerUp", "button": 0},
                    ],
                }],
            }
            r = httpx.post(self._session_url("/actions"), json=body, timeout=WDA_TIMEOUT)
            return r.status_code == 200
        except Exception:
            logger.exception("[wda] swipe failed")
            return False

    def type_text(self, text: str, element_type: str = "XCUIElementTypeTextView") -> bool:
        try:
            self._ensure_session()
            r = httpx.post(
                self._session_url("/element"),
                json={"using": "class name", "value": element_type},
                timeout=WDA_TIMEOUT,
            )
            data = r.json()
            eid = data["value"].get("element-6066-11e4-a52e-4f735466cecf") or data["value"].get("ELEMENT")
            if not eid:
                for v in ("XCUIElementTypeTextField", "XCUIElementTypeSearchField"):
                    r2 = httpx.post(
                        self._session_url("/element"),
                        json={"using": "class name", "value": v},
                        timeout=WDA_TIMEOUT,
                    )
                    d2 = r2.json()
                    eid = d2["value"].get("element-6066-11e4-a52e-4f735466cecf") or d2["value"].get("ELEMENT")
                    if eid:
                        break
            if not eid:
                return False

            body = json.dumps({"value": [text]}).encode("utf-8")
            r = httpx.post(
                self._session_url(f"/element/{eid}/value"),
                content=body,
                headers={"Content-Type": "application/json; charset=utf-8"},
                timeout=WDA_TIMEOUT,
            )
            return r.status_code == 200
        except Exception:
            logger.exception("[wda] type_text failed")
            return False

    def press_home(self) -> bool:
        try:
            r = httpx.post(self._url("/wda/homescreen"), timeout=WDA_TIMEOUT)
            return r.status_code == 200
        except Exception:
            logger.exception("[wda] press_home failed")
            return False

    def get_source(self) -> str | None:
        try:
            self._ensure_session()
            r = httpx.get(self._session_url("/source"), timeout=WDA_TIMEOUT)
            data = r.json()
            return data.get("value", "")
        except Exception:
            logger.exception("[wda] get_source failed")
            return None

    def window_size(self) -> dict[str, int] | None:
        try:
            self._ensure_session()
            r = httpx.get(self._session_url("/window/size"), timeout=WDA_TIMEOUT)
            data = r.json()
            return data.get("value")
        except Exception:
            return None

    def open_url(self, url: str) -> bool:
        try:
            self._ensure_session()
            r = httpx.post(
                self._session_url("/url"),
                json={"url": url},
                timeout=WDA_TIMEOUT,
            )
            return r.status_code == 200
        except Exception:
            logger.exception("[wda] open_url failed")
            return False

    def find_element_by_name(self, name: str) -> dict[str, Any] | None:
        try:
            self._ensure_session()
            r = httpx.post(
                self._session_url("/element"),
                json={"using": "name", "value": name},
                timeout=WDA_TIMEOUT,
            )
            if r.status_code != 200:
                return None
            data = r.json()
            return data.get("value")
        except Exception:
            return None


_instance: WdaService | None = None


def get_wda_service(base_url: str | None = None) -> WdaService:
    global _instance
    if _instance is None:
        if not base_url:
            import os
            base_url = os.environ.get("WDA_BASE_URL", "http://localhost:8100")
        _instance = WdaService(base_url)
    return _instance
