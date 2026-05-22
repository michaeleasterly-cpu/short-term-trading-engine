"""Tests for ``check_sec_filings_freshness`` — Form 4 + 8-K freshness +
T1/T2 stock-coverage floor.

Pins:
* GREEN when newest filing is within ``MAX_AGE_DAYS`` and stock coverage
  is ≥ ``MIN_COVERAGE_PCT``.
* RED ``stale_newest_filing`` when the newest filing is too old.
* RED ``insufficient_stock_coverage`` when fewer than ``MIN_COVERAGE_PCT``
  of T1+T2 stocks have a filing in ``COVERAGE_WINDOW_DAYS``.
* RED ``empty_tables`` when both tables are empty.
* Universe sentinel — when ``addressable_count`` is zero the check does
  NOT fire a coverage red (universe problem, not SEC problem).
* ``MAX_AGE_DAYS`` is read from the ``FeedProfile`` SoT.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from tpcore.feeds import freshness_max_age_days
from tpcore.quality.validation.checks.sec_filings_freshness import (
    CHECK_NAME,
    COVERAGE_WINDOW_DAYS,
    MAX_AGE_DAYS,
    MIN_COVERAGE_PCT,
    check_sec_filings_freshness,
)


class _Conn:
    def __init__(self, row: dict[str, Any]) -> None:
        self._row = row

    async def fetchrow(self, sql: str, *args: object) -> dict[str, Any] | None:
        assert "sec_insider_transactions" in sql.lower()
        assert "sec_material_events" in sql.lower()
        return self._row


class _AcquireCM:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _Conn:
        return self._conn

    async def __aexit__(self, *exc: object) -> None:
        return None


class _Pool:
    def __init__(self, row: dict[str, Any]) -> None:
        self._row = row

    def acquire(self) -> _AcquireCM:
        return _AcquireCM(_Conn(self._row))


async def test_passes_when_fresh_and_covered() -> None:
    today = datetime.now(UTC).date()
    pool = _Pool({
        "newest_filing": today - timedelta(days=2),
        "addressable_count": 100,
        "covered_count": 60,  # 60% — well above 30% floor
        "insider_rows": 500,
        "material_rows": 700,
    })
    r = await check_sec_filings_freshness(pool)
    assert r.passed is True
    assert r.failures == []
    assert r.name == CHECK_NAME


async def test_fails_when_newest_filing_stale() -> None:
    today = datetime.now(UTC).date()
    pool = _Pool({
        "newest_filing": today - timedelta(days=MAX_AGE_DAYS + 10),
        "addressable_count": 100,
        "covered_count": 60,
        "insider_rows": 500,
        "material_rows": 700,
    })
    r = await check_sec_filings_freshness(pool)
    assert r.passed is False
    reasons = [f.reason for f in r.failures]
    assert "stale_newest_filing" in reasons


async def test_fails_when_coverage_below_floor() -> None:
    today = datetime.now(UTC).date()
    pool = _Pool({
        "newest_filing": today - timedelta(days=2),
        "addressable_count": 100,
        "covered_count": 5,  # 5% — well below 30% floor
        "insider_rows": 100,
        "material_rows": 100,
    })
    r = await check_sec_filings_freshness(pool)
    assert r.passed is False
    reasons = [f.reason for f in r.failures]
    assert "insufficient_stock_coverage" in reasons


async def test_fails_when_both_tables_empty() -> None:
    pool = _Pool({
        "newest_filing": None,
        "addressable_count": 100,
        "covered_count": 0,
        "insider_rows": 0,
        "material_rows": 0,
    })
    r = await check_sec_filings_freshness(pool)
    assert r.passed is False
    assert r.failures[0].reason == "empty_tables"


async def test_skips_coverage_when_universe_empty() -> None:
    """Universe sentinel: addressable_count=0 must not fire a SEC
    coverage red (it's a universe-build problem, not a SEC problem)."""
    today = datetime.now(UTC).date()
    pool = _Pool({
        "newest_filing": today - timedelta(days=2),  # fresh
        "addressable_count": 0,
        "covered_count": 0,
        "insider_rows": 500,
        "material_rows": 700,
    })
    r = await check_sec_filings_freshness(pool)
    assert r.passed is True


def test_max_age_sourced_from_feed_profile() -> None:
    # The check is gated on the newer of the two SEC tables; the profile
    # used for the threshold is sec_insider_transactions (14d default).
    assert MAX_AGE_DAYS == freshness_max_age_days(
        "sec_insider_transactions", -1
    )


def test_coverage_window_pinned() -> None:
    """180d coverage window is the audited value; pin to catch silent
    relaxation."""
    assert COVERAGE_WINDOW_DAYS == 180
    assert MIN_COVERAGE_PCT == 0.30
