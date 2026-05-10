"""Post-reply triggers: summary generation, etc."""
from __future__ import annotations

import logging
import time
import threading

from sqlalchemy.orm import Session, sessionmaker

from app.models.models import Message, SessionSummary
from app.services.summary_service import SummaryService

logger = logging.getLogger(__name__)

# Per-session lock to prevent concurrent summary generation
_summary_locks: dict[int, threading.Lock] = {}
_summary_locks_guard = threading.Lock()

# Track summary failures per session for backoff
_summary_fail_info: dict[int, tuple[float, int]] = {}  # session_id → (last_fail_time, fail_count)
_SUMMARY_BACKOFF_BASE = 120  # 2 min base backoff
_SUMMARY_BACKOFF_MAX = 600   # 10 min max backoff

_summary_skip_rounds: dict[int, int] = {}
_POST_SUMMARY_SKIP = 1


def _get_summary_lock(session_id: int) -> threading.Lock:
    with _summary_locks_guard:
        if session_id not in _summary_locks:
            _summary_locks[session_id] = threading.Lock()
        return _summary_locks[session_id]


def maybe_trigger_post_reply(
    session_id: int,
    assistant_id: int | None,
    background_tasks=None,
) -> None:
    """Post-reply triggers (currently none — image/file processing moved to on-demand tools)."""
    pass


def trigger_summary(
    session_factory: sessionmaker,
    session_id: int,
    message_ids: list[int],
    assistant_id: int,
) -> None:
    if not session_factory:
        logger.warning(
            "Summary trigger skipped: session_factory is not configured (session_id=%s).",
            session_id,
        )
        return
    if not message_ids:
        logger.warning(
            "Summary trigger skipped: no trimmed message ids (session_id=%s).",
            session_id,
        )
        return

    # Post-summary skip: 3 rounds after a successful summary
    remaining = _summary_skip_rounds.get(session_id, 0)
    if remaining > 0:
        _summary_skip_rounds[session_id] = remaining - 1
        logger.info(
            "Summary trigger skipped: post-summary skip (%d rounds left, session_id=%s).",
            remaining - 1, session_id,
        )
        return

    # Backoff after repeated failures
    fail_info = _summary_fail_info.get(session_id)
    if fail_info:
        last_fail, fail_count = fail_info
        backoff = min(_SUMMARY_BACKOFF_BASE * fail_count, _SUMMARY_BACKOFF_MAX)
        elapsed = time.monotonic() - last_fail
        if elapsed < backoff:
            logger.info(
                "Summary trigger skipped: backoff %ds remaining (session_id=%s, fails=%d).",
                int(backoff - elapsed), session_id, fail_count,
            )
            return

    # Prevent concurrent summary generation for the same session
    lock = _get_summary_lock(session_id)
    if not lock.acquire(blocking=False):
        logger.info(
            "Summary trigger skipped: another summary in progress (session_id=%s).",
            session_id,
        )
        return

    db: Session = session_factory()
    try:
        last_summary = (
            db.query(SessionSummary)
            .filter(
                SessionSummary.session_id == session_id,
                SessionSummary.assistant_id == assistant_id,
                SessionSummary.deleted_at.is_(None),
                SessionSummary.msg_id_end.isnot(None),
            )
            .order_by(SessionSummary.msg_id_end.desc())
            .first()
        )
        last_end = last_summary.msg_id_end if last_summary else 0

        trimmed_messages = (
            db.query(Message)
            .filter(
                Message.session_id == session_id,
                Message.id.in_(message_ids),
                Message.id > last_end,
                Message.summary_group_id.is_(None),
            )
            .order_by(Message.created_at.asc(), Message.id.asc())
            .all()
        )
        if not trimmed_messages:
            logger.info(
                "Summary trigger skipped: all trimmed messages already summarized "
                "(session_id=%s, last_end=%s, candidates=%d).",
                session_id, last_end, len(message_ids),
            )
            return
        MIN_SUMMARY_MESSAGES = 10
        if len(trimmed_messages) < MIN_SUMMARY_MESSAGES:
            logger.info(
                "Summary trigger deferred: only %d messages (min %d) "
                "(session_id=%s, range=%s~%s).",
                len(trimmed_messages), MIN_SUMMARY_MESSAGES,
                session_id, trimmed_messages[0].id, trimmed_messages[-1].id,
            )
            return
        logger.info(
            "Summary trigger: %d new messages (session_id=%s, last_end=%s, range=%s~%s).",
            len(trimmed_messages), session_id, last_end,
            trimmed_messages[0].id, trimmed_messages[-1].id,
        )
        summary_service = SummaryService(session_factory)
        summary_service.generate_summary(session_id, trimmed_messages, assistant_id)

        # Success — clear backoff, set 3-round skip
        _summary_fail_info.pop(session_id, None)
        _summary_skip_rounds[session_id] = _POST_SUMMARY_SKIP
    except Exception:
        logger.exception(
            "Summary trigger failed (session_id=%s, assistant_id=%s).",
            session_id,
            assistant_id,
        )
        prev = _summary_fail_info.get(session_id)
        fail_count = (prev[1] + 1) if prev else 1
        _summary_fail_info[session_id] = (time.monotonic(), fail_count)
        logger.info("Summary backoff: fail_count=%d, next retry in %ds", fail_count,
                    min(_SUMMARY_BACKOFF_BASE * fail_count, _SUMMARY_BACKOFF_MAX))
    finally:
        lock.release()
        db.close()
