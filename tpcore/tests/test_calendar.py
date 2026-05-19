"""Regression tests for ``tpcore.calendar`` — UTC-throughout, no ZoneInfo.

The bug we're guarding against: ``exchange_calendars`` >= 4.5 validates
tz-aware Timestamps by reading ``ts.tz.key``, which ``datetime.timezone.utc``
doesn't expose. Earlier the helpers passed a stdlib-aware Timestamp to
``is_session`` / ``session_open`` / ``session_close`` and crashed with
``AttributeError: 'datetime.timezone' object has no attribute 'key'``.

The fix uses naive UTC Timestamps on those boundary calls (same
wall-clock UTC, no tzinfo to introspect) and keeps the aware Timestamp
only for the range comparison. These tests pin that contract:

* ``session_contains`` works on a real session datetime;
* ``session_contains`` works on a known-closed datetime;
* ``next_open`` / ``next_close`` / ``previous_close`` round-trip a
  stdlib-UTC datetime without raising on the ``.tz.key`` lookup.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from tpcore.calendar import (
    is_trading_day,
    next_close,
    next_monday_open,
    next_open,
    previous_close,
    session_contains,
    trading_days_between,
)


def test_session_contains_during_regular_hours_returns_true() -> None:
    # 2024-01-08 (Mon) at 18:00 UTC = 13:00 ET — mid-session.
    mid_session = datetime(2024, 1, 8, 18, 0, tzinfo=UTC)
    assert session_contains(mid_session) is True


def test_session_contains_before_open_returns_false() -> None:
    # 2024-01-08 (Mon) at 10:00 UTC = 05:00 ET — pre-market.
    pre_market = datetime(2024, 1, 8, 10, 0, tzinfo=UTC)
    assert session_contains(pre_market) is False


def test_session_contains_after_close_returns_false() -> None:
    # 2024-01-08 (Mon) at 22:00 UTC = 17:00 ET — post-market.
    post_close = datetime(2024, 1, 8, 22, 0, tzinfo=UTC)
    assert session_contains(post_close) is False


def test_session_contains_on_weekend_returns_false() -> None:
    saturday = datetime(2024, 1, 6, 18, 0, tzinfo=UTC)
    sunday = datetime(2024, 1, 7, 18, 0, tzinfo=UTC)
    assert session_contains(saturday) is False
    assert session_contains(sunday) is False


def test_session_contains_on_holiday_returns_false() -> None:
    # 2024-07-04 was a regular holiday (US Independence Day).
    holiday_noon = datetime(2024, 7, 4, 16, 0, tzinfo=UTC)
    assert session_contains(holiday_noon) is False


def test_is_trading_day_weekend_vs_weekday() -> None:
    assert is_trading_day(datetime(2024, 1, 8, 12, 0, tzinfo=UTC)) is True  # Mon
    assert is_trading_day(datetime(2024, 1, 6, 12, 0, tzinfo=UTC)) is False  # Sat
    assert is_trading_day(datetime(2024, 7, 4, 12, 0, tzinfo=UTC)) is False  # holiday


def test_next_open_returns_stdlib_utc_aware() -> None:
    # Friday 22:00 UTC — past Friday's close. Next open should be Monday.
    fri_close = datetime(2024, 1, 5, 22, 0, tzinfo=UTC)
    nxt = next_open(fri_close)
    assert nxt.tzinfo is UTC
    # Monday 2024-01-08 14:30 UTC = 09:30 ET (DST off).
    assert nxt == datetime(2024, 1, 8, 14, 30, tzinfo=UTC)


def test_next_close_returns_stdlib_utc_aware() -> None:
    mid_session = datetime(2024, 1, 8, 18, 0, tzinfo=UTC)
    nxt = next_close(mid_session)
    assert nxt.tzinfo is UTC
    # Monday 2024-01-08 21:00 UTC = 16:00 ET.
    assert nxt == datetime(2024, 1, 8, 21, 0, tzinfo=UTC)


def test_previous_close_returns_stdlib_utc_aware() -> None:
    sat_noon = datetime(2024, 1, 6, 18, 0, tzinfo=UTC)
    prev = previous_close(sat_noon)
    assert prev.tzinfo is UTC
    # Friday 2024-01-05 21:00 UTC.
    assert prev == datetime(2024, 1, 5, 21, 0, tzinfo=UTC)


def test_trading_days_between_simple_week() -> None:
    mon = date(2024, 1, 8)
    fri = date(2024, 1, 12)
    # Per docstring: "sessions strictly between plus one" — Tue/Wed/Thu = 3 + 1 = 4.
    assert trading_days_between(mon, fri) == 4
    # Order-insensitive.
    assert trading_days_between(fri, mon) == 4


def test_naive_datetime_raises() -> None:
    naive = datetime(2024, 1, 8, 18, 0)  # noqa: DTZ001
    with pytest.raises(ValueError):
        session_contains(naive)


# ────────────────────────────────────────────────────────────────────────────
# next_monday_open — must honor its "before the open" contract (#245)
#
# Reference fixtures (verified against XNYS):
#   2024-01-08  Monday, session open 14:30Z
#   2024-01-15  Monday — MLK Day holiday (closed); next_open → 2024-01-16 14:30Z
#   2024-01-22  Monday, session open 14:30Z (next trading Monday after MLK)
# ────────────────────────────────────────────────────────────────────────────

_MON_0108_OPEN = datetime(2024, 1, 8, 14, 30, tzinfo=UTC)
_MON_0122_OPEN = datetime(2024, 1, 22, 14, 30, tzinfo=UTC)
# 2024-01-15 is MLK Day → the holiday-aware "following Monday" open is 01-16.
_TUE_0116_OPEN = datetime(2024, 1, 16, 14, 30, tzinfo=UTC)


def test_next_monday_open_monday_before_open_returns_this_monday() -> None:
    # Monday 2024-01-08 at 09:00Z — strictly before the 14:30Z open.
    dt = datetime(2024, 1, 8, 9, 0, tzinfo=UTC)
    assert next_monday_open(dt) == _MON_0108_OPEN


def test_next_monday_open_monday_exactly_at_open_advances() -> None:
    # Monday 2024-01-08 exactly AT the open → following Monday (01-15 is a
    # holiday, so the holiday-aware open is 2024-01-16).
    dt = datetime(2024, 1, 8, 14, 30, tzinfo=UTC)
    assert next_monday_open(dt) == _TUE_0116_OPEN


def test_next_monday_open_monday_mid_session_advances() -> None:
    dt = datetime(2024, 1, 8, 18, 0, tzinfo=UTC)
    assert next_monday_open(dt) == _TUE_0116_OPEN


def test_next_monday_open_monday_end_of_day_advances() -> None:
    dt = datetime(2024, 1, 8, 23, 59, tzinfo=UTC)
    assert next_monday_open(dt) == _TUE_0116_OPEN


@pytest.mark.parametrize(
    ("day", "hour"),
    [
        (9, 18),  # Tue 2024-01-09
        (10, 18),  # Wed
        (11, 18),  # Thu
        (12, 18),  # Fri
        (13, 18),  # Sat
        (14, 18),  # Sun
    ],
)
def test_next_monday_open_tue_through_sun_returns_next_monday(
    day: int, hour: int
) -> None:
    # The Monday following 2024-01-09..14 is 2024-01-15 (MLK holiday) →
    # holiday-aware open is 2024-01-16 14:30Z.
    dt = datetime(2024, 1, day, hour, 0, tzinfo=UTC)
    assert next_monday_open(dt) == _TUE_0116_OPEN


def test_next_monday_open_target_monday_is_holiday_before_open_branch() -> None:
    # Sunday 2024-01-14 → next Monday is 2024-01-15 (MLK, closed).
    # next_open must advance to 2024-01-16 14:30Z.
    dt = datetime(2024, 1, 14, 12, 0, tzinfo=UTC)
    assert next_monday_open(dt) == _TUE_0116_OPEN


def test_next_monday_open_following_monday_branch_skips_to_clean_monday() -> None:
    # Monday 2024-01-22 mid-session → following Monday 2024-01-29 (open day).
    dt = datetime(2024, 1, 22, 18, 0, tzinfo=UTC)
    assert next_monday_open(dt) == datetime(2024, 1, 29, 14, 30, tzinfo=UTC)


def test_next_monday_open_non_utc_tz_near_boundary() -> None:
    # 09:25 in UTC-05:00 == 14:25Z on Monday 2024-01-08 — strictly before the
    # 14:30Z open → this Monday's open. tz handling must be unchanged.
    est = timezone(timedelta(hours=-5))
    dt = datetime(2024, 1, 8, 9, 25, tzinfo=est)
    assert next_monday_open(dt) == _MON_0108_OPEN
    # 09:35 EST == 14:35Z — at/after open → following Monday (holiday-aware).
    dt_after = datetime(2024, 1, 8, 9, 35, tzinfo=est)
    assert next_monday_open(dt_after) == _TUE_0116_OPEN
