"""NYSE (XNYS) calendar helpers.

All inputs and outputs are timezone-aware stdlib UTC datetimes. The
underlying ``exchange_calendars`` library uses pandas Timestamps
internally; we convert on the boundary so callers only ever deal with
``datetime``.

Boundary detail: ``exchange_calendars.is_session`` / ``session_open`` /
``session_close`` validate inputs through ``calendar_helpers.parse_date``,
which expects either a *naive* Timestamp or a Timestamp whose ``tz``
exposes ``.key`` (zoneinfo / pytz). The stdlib ``datetime.timezone.utc``
object has no ``.key``, so we pass naive UTC Timestamps for the
``is_session`` / ``session_open`` / ``session_close`` calls and keep
the aware Timestamp only for the open/close range comparison.
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
    """Tz-aware pandas Timestamp (stdlib UTC) for instant comparisons."""
    return pd.Timestamp(_ensure_utc(dt))


def _to_naive_utc_ts(dt: datetime) -> pd.Timestamp:
    """Naive UTC pandas Timestamp for ``exchange_calendars`` boundary calls.

    Same wall-clock UTC value; the tzinfo is stripped so the helper
    library doesn't try to read a ``.tz.key`` attribute that stdlib's
    ``datetime.timezone.utc`` doesn't expose.
    """
    return pd.Timestamp(_ensure_utc(dt).replace(tzinfo=None))


def is_trading_day(dt: datetime) -> bool:
    """Return True if ``dt`` (UTC) falls on an XNYS trading session."""
    cal = _calendar()
    d: date = _ensure_utc(dt).date()
    return bool(cal.is_session(pd.Timestamp(d)))


def session_contains(dt: datetime) -> bool:
    """Return True if ``dt`` is inside the regular trading session window."""
    cal = _calendar()
    naive = _to_naive_utc_ts(dt)
    session_day = naive.normalize()
    if not cal.is_session(session_day):
        return False
    open_ts = cal.session_open(session_day)
    close_ts = cal.session_close(session_day)
    return open_ts <= _to_ts(dt) <= close_ts


def require_market_closed(*, force: bool = False, now: datetime | None = None) -> bool:
    """Single source of truth for "is it safe to run this data op?".

    Returns ``True`` when the caller is cleared to proceed:
        * ``force=True`` short-circuits the check (operator override —
          use only for testing or controlled mid-session runs).
        * Otherwise, returns ``True`` iff the NYSE regular session is
          NOT currently in progress (so pre-market / after-hours /
          weekends / holidays all pass).

    Returns ``False`` when the regular session is live and ``force`` is
    not set — callers should refuse and emit a clear operator message.

    Wraps ``session_contains`` so half-days, holidays, and DST shifts
    are handled the same way the engines see them. Replaces the
    per-script inline checks that were drifting (``ops.py`` had its own
    ``_market_open_block_reason``; smoke test had its own
    ``_is_market_open``; etc.).
    """
    if force:
        return True
    return not session_contains(now or datetime.now(UTC))


def next_open(dt: datetime) -> datetime:
    """Next regular session open at or after ``dt`` (UTC)."""
    cal = _calendar()
    open_ts = cal.next_open(_to_naive_utc_ts(dt))
    return open_ts.to_pydatetime().astimezone(UTC)


def next_close(dt: datetime) -> datetime:
    """Next regular session close at or after ``dt`` (UTC)."""
    cal = _calendar()
    close_ts = cal.next_close(_to_naive_utc_ts(dt))
    return close_ts.to_pydatetime().astimezone(UTC)


def previous_close(dt: datetime) -> datetime:
    """Most recent session close at or before ``dt`` (UTC)."""
    cal = _calendar()
    close_ts = cal.previous_close(_to_naive_utc_ts(dt))
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


def first_session_of_month(year: int, month: int) -> date:
    """Return the first NYSE trading session in the given calendar month.

    Used by portfolio strategies that rebalance on the first session of each
    month — call once with ``year, month`` to learn whether ``as_of`` matches.
    Raises ``ValueError`` if the month has no sessions (defensive — would only
    happen if XNYS were closed for an entire calendar month, which it isn't)."""
    cal = _calendar()
    start = pd.Timestamp(year, month, 1)
    # End-of-month: day 28 + 4 lands in the next month, then floor to month-end.
    end = (start + pd.offsets.MonthEnd(0))
    sessions = cal.sessions_in_range(start, end)
    if len(sessions) == 0:
        raise ValueError(f"no XNYS sessions in {year}-{month:02d}")
    return sessions[0].date()


def sessions_in_range(start: date, end: date) -> list[date]:
    """All NYSE trading sessions in [start, end] inclusive, as python dates."""
    cal = _calendar()
    sessions = cal.sessions_in_range(pd.Timestamp(start), pd.Timestamp(end))
    return [ts.date() for ts in sessions]


def first_session() -> date:
    """Earliest NYSE trading session the bundled exchange_calendars
    knows about (typically ~20 years before "today"). Callers that
    validate completeness over historical macro data should clamp to
    this — anything older than the calendar can't be NYSE-session-
    expanded so the check has nothing to compare against."""
    return _calendar().first_session.date()


def first_sessions_of_each_month_in_range(start: date, end: date) -> list[date]:
    """Return the first trading session of each calendar month touched by
    [start, end]. Used by backtests that need to know the rebalance dates
    across a multi-year window without scanning panel data."""
    cal = _calendar()
    sessions = cal.sessions_in_range(pd.Timestamp(start), pd.Timestamp(end))
    out: list[date] = []
    last_year_month: tuple[int, int] | None = None
    for ts in sessions:
        d = ts.date()
        ym = (d.year, d.month)
        if ym != last_year_month:
            out.append(d)
            last_year_month = ym
    return out


def next_monday_open(dt: datetime) -> datetime:
    """Next Monday's session open (used for weekly risk resets).

    If ``dt`` is on a Monday before the open, returns *that* Monday's open;
    otherwise advances to the following Monday and returns its session open.
    Honors holidays — if Monday is closed, advances to the next session open.
    """
    dt_utc = _ensure_utc(dt)
    d = dt_utc.date()
    if d.weekday() == 0:  # Monday
        this_monday = datetime(d.year, d.month, d.day, tzinfo=UTC)
        this_open = next_open(this_monday)  # holiday-aware session open
        if dt_utc < this_open:
            return this_open
        next_monday = d + timedelta(days=7)
    else:
        days_ahead = (0 - d.weekday()) % 7  # Monday == 0
        next_monday = d + timedelta(days=days_ahead)
    candidate = datetime(next_monday.year, next_monday.month, next_monday.day, tzinfo=UTC)
    return next_open(candidate)
