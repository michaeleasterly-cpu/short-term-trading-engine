"""NYSE (XNYS) calendar helpers.

All inputs and outputs are timezone-aware UTC datetimes. The underlying
``exchange_calendars`` library uses pandas Timestamps internally; we convert
on the boundary so callers only ever deal with stdlib ``datetime``.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from functools import lru_cache

import exchange_calendars as ec
import pandas as pd

_CAL_NAME = "XNYS"


@lru_cache(maxsize=1)
def _calendar() -> ec.ExchangeCalendar:
    return ec.get_calendar(_CAL_NAME)


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("datetime must be timezone-aware (UTC)")
    return dt.astimezone(UTC)


def _to_ts(dt: datetime) -> pd.Timestamp:
    return pd.Timestamp(_ensure_utc(dt))


def is_trading_day(dt: datetime) -> bool:
    """Return True if ``dt`` (UTC) falls on an XNYS trading session."""
    cal = _calendar()
    d: date = _ensure_utc(dt).date()
    return bool(cal.is_session(pd.Timestamp(d)))


def session_contains(dt: datetime) -> bool:
    """Return True if ``dt`` is inside the regular trading session window."""
    cal = _calendar()
    ts = _to_ts(dt)
    if not cal.is_session(ts.normalize()):
        return False
    open_ts = cal.session_open(ts.normalize())
    close_ts = cal.session_close(ts.normalize())
    return open_ts <= ts <= close_ts


def next_open(dt: datetime) -> datetime:
    """Next regular session open at or after ``dt`` (UTC)."""
    cal = _calendar()
    ts = _to_ts(dt)
    open_ts = cal.next_open(ts)
    return open_ts.to_pydatetime().astimezone(UTC)


def next_close(dt: datetime) -> datetime:
    """Next regular session close at or after ``dt`` (UTC)."""
    cal = _calendar()
    ts = _to_ts(dt)
    close_ts = cal.next_close(ts)
    return close_ts.to_pydatetime().astimezone(UTC)


def previous_close(dt: datetime) -> datetime:
    """Most recent session close at or before ``dt`` (UTC)."""
    cal = _calendar()
    ts = _to_ts(dt)
    close_ts = cal.previous_close(ts)
    return close_ts.to_pydatetime().astimezone(UTC)


def trading_days_between(d1: date, d2: date) -> int:
    """Approximate count of NYSE trading sessions between ``d1`` and ``d2``.

    Returns the absolute distance — order of arguments doesn't matter. If
    both endpoints are trading sessions, the result is the number of
    sessions strictly between them plus one (e.g. consecutive trading days
    → 1). When an endpoint is a holiday/weekend the count is approximately
    right; this is intended for tolerances like "within 5 trading days",
    not for exact arithmetic.
    """
    cal = _calendar()
    lo, hi = sorted([d1, d2])
    if lo == hi:
        return 0
    sessions = cal.sessions_in_range(pd.Timestamp(lo), pd.Timestamp(hi))
    return max(0, len(sessions) - 1)


def next_monday_open(dt: datetime) -> datetime:
    """Next Monday's session open (used for weekly risk resets).

    If ``dt`` is on a Monday before the open, returns *that* Monday's open;
    otherwise advances to the following Monday and returns its session open.
    Honors holidays — if Monday is closed, advances to the next session open.
    """
    d = _ensure_utc(dt).date()
    days_ahead = (0 - d.weekday()) % 7  # Monday == 0
    monday = d + timedelta(days=days_ahead)
    candidate = datetime(monday.year, monday.month, monday.day, tzinfo=UTC)
    return next_open(candidate)
