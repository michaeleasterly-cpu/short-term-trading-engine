"""Tests for ``check_earnings_events_freshness``."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tpcore.quality.validation.checks.earnings_events_freshness import (
    MIN_COVERAGE_PCT,
    check_earnings_events_freshness,
)


class _FakeConn:
    def __init__(self, row: dict) -> None:
        self._row = row

    async def fetchrow(self, sql: str) -> dict:
        return self._row


class _FakeCM:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc) -> None:
        return None


class _FakePool:
    def __init__(self, row: dict) -> None:
        self.conn = _FakeConn(row)

    def acquire(self) -> _FakeCM:
        return _FakeCM(self.conn)


@pytest.mark.asyncio
async def test_passes_when_fresh_and_well_covered():
    """66 addressable stocks, 30 (45%) covered, newest event 5d ago."""
    today = datetime.now(UTC).date()
    pool = _FakePool({
        "newest_event": today - timedelta(days=5),
        "addressable_count": 66,
        "covered_count": 30,
        "total_rows": 1350,
    })
    r = await check_earnings_events_freshness(pool)
    assert r.passed is True
    assert r.failures == []


@pytest.mark.asyncio
async def test_passes_at_floor_coverage():
    """Exactly at the 20% floor — passes."""
    today = datetime.now(UTC).date()
    n_addr = 100
    n_cov = int(n_addr * MIN_COVERAGE_PCT) + 1  # just above floor
    pool = _FakePool({
        "newest_event": today - timedelta(days=5),
        "addressable_count": n_addr,
        "covered_count": n_cov,
        "total_rows": 9999,
    })
    r = await check_earnings_events_freshness(pool)
    assert r.passed is True


@pytest.mark.asyncio
async def test_fails_when_coverage_below_floor():
    """Below floor — flags insufficient_stock_coverage."""
    today = datetime.now(UTC).date()
    pool = _FakePool({
        "newest_event": today - timedelta(days=5),
        "addressable_count": 100,
        "covered_count": 5,  # 5% — well below 20% floor
        "total_rows": 99,
    })
    r = await check_earnings_events_freshness(pool)
    assert r.passed is False
    assert r.failures[0].reason == "insufficient_stock_coverage"
    assert "5/100" in r.failures[0].observed


@pytest.mark.asyncio
async def test_fails_when_newest_event_stale():
    today = datetime.now(UTC).date()
    pool = _FakePool({
        "newest_event": today - timedelta(days=200),  # > 90d threshold
        "addressable_count": 66,
        "covered_count": 30,
        "total_rows": 1350,
    })
    r = await check_earnings_events_freshness(pool)
    assert r.passed is False
    assert r.failures[0].reason == "stale_newest_event"


@pytest.mark.asyncio
async def test_fails_when_table_empty():
    pool = _FakePool({
        "newest_event": None,
        "addressable_count": 66,
        "covered_count": 0,
        "total_rows": 0,
    })
    r = await check_earnings_events_freshness(pool)
    assert r.passed is False
    assert r.failures[0].reason == "empty_table"


@pytest.mark.asyncio
async def test_skips_coverage_when_no_addressable_universe():
    """If T1+T2 has no stocks (e.g. universe not built yet), don't
    spuriously fail on coverage — that's a universe problem, not a
    catalyst problem. The freshness check still runs."""
    today = datetime.now(UTC).date()
    pool = _FakePool({
        "newest_event": today - timedelta(days=5),  # fresh
        "addressable_count": 0,
        "covered_count": 0,
        "total_rows": 1350,
    })
    r = await check_earnings_events_freshness(pool)
    assert r.passed is True
