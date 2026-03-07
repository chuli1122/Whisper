from __future__ import annotations

from datetime import datetime, timedelta, timezone

TZ_EAST8 = timezone(timedelta(hours=8))


def format_datetime(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ_EAST8)
    return dt.astimezone(TZ_EAST8).isoformat()


def format_datetime_short(dt: datetime | None) -> str | None:
    """Return compact 'YYYY.MM.DD HH:MM' format in UTC+8."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ_EAST8)
    return dt.astimezone(TZ_EAST8).strftime("%Y.%m.%d %H:%M")
