"""Generation coordinator — global mutex so private chat (telegram/qq) and
group @ handlers (cafe, qq_group) never generate the model response in parallel.

Why: they all write to the same assistant session. If two chats run at once,
their trigger messages get interleaved into a single merged user block in the
next round's payload (chat_service auto-merges consecutive role=user messages).

Release timing: holder releases as soon as the chat stream is *done* — the
assistant response is already persisted to DB. The actual platform send
(QQ send_private_msg, send_group_msg, etc.) can still be running; we don't
wait for that because platform sends are slow and the next round only needs
the assistant DB row, not the delivered message.
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_holder: Optional[str] = None
_current_request_id: Optional[str] = None


def set_current_request_id(rid: Optional[str]) -> None:
    global _current_request_id
    _current_request_id = rid


def current_request_id() -> Optional[str]:
    return _current_request_id


class GenerationLock:
    """Context manager that serializes model-response generation across all
    channel handlers (telegram/qq private + cafe/qq_group @)."""

    def __init__(self, source: str):
        self.source = source
        self._released = False

    def __enter__(self) -> "GenerationLock":
        global _holder
        if _lock.locked():
            logger.info("[gen-lock] %s waiting (held by %s)", self.source, _holder)
        _lock.acquire()
        _holder = self.source
        logger.info("[gen-lock] acquired by %s", self.source)
        return self

    def release(self) -> None:
        """Release early (e.g. when stream is done but platform send is still running)."""
        global _holder, _current_request_id
        if self._released:
            return
        _holder = None
        _current_request_id = None
        _lock.release()
        self._released = True
        logger.info("[gen-lock] released by %s", self.source)

    def __exit__(self, *exc) -> None:
        self.release()


def current_holder() -> Optional[str]:
    return _holder
