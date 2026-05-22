"""Tests for ``check_liquidity_tiers_freshness`` — quarterly tier refresh +
T1/T2 coverage gate.

Pins:
* GREEN when newest ``last_updated`` is within ``MAX_AGE_DAYS`` and T1/T2
  coverage of the active prices_daily universe is ≥ ``MIN_T1_T2_COVERAGE_PCT``.
* RED ``stale_assignment`` when newest ``last_updated`` is too old.
* RED ``insufficient_t1_t2_coverage`` when coverage drops below 3%.
* RED ``empty_table`` when the table has no rows.
* ``MAX_AGE_DAYS`` is read from the ``FeedProfile`` SoT.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from tpcore.feeds import freshness_max_age_days
from tpcore.quality.validation.checks.liquidity_tiers_freshness import (
    CHECK_NAME,
    MAX_AGE_DAYS,
    MIN_T1_T2_COVERAGE_PCT,
    check_liquidity_tiers_freshness,
)


class _Conn:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self._row = row

    async def fetchrow(self, sql: str, *args: object) -> dict[str, Any] | None:
        assert "platform.liquidity_tiers" in sql.lower()
        return self._row


class _AcquireCM:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _Conn:
        return self._conn

    async def __aexit__(self, *exc: object) -> None:
        return None


class _Pool:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self._row = row

    def acquire(self) -> _AcquireCM:
        return _AcquireCM(_Conn(self._row))


async def test_passes_when_fresh_and_covered() -> None:
    now = datetime.now(UTC)
    pool = _Pool({
        "latest": now - timedelta(days=10),
        "rows_total": 5000,
        "t1_t2_count": 1000,  # 20% — well above 3% floor
        "active_universe": 5000,
    })
    r = await check_liquidity_tiers_freshness(pool)
    assert r.passed is True
    assert r.failures == []
    assert r.name == CHECK_NAME


async def test_fails_when_stale() -> None:
    now = datetime.now(UTC)
    pool = _Pool({
        "latest": now - timedelta(days=MAX_AGE_DAYS + 30),
        "rows_total": 5000,
        "t1_t2_count": 1000,
        "active_universe": 5000,
    })
    r = await check_liquidity_tiers_freshness(pool)
    assert r.passed is False
    reasons = [f.reason for f in r.failures]
    assert "stale_assignment" in reasons


async def test_fails_when_coverage_below_floor() -> None:
    now = datetime.now(UTC)
    pool = _Pool({
        "latest": now - timedelta(days=10),
        "rows_total": 5000,
        "t1_t2_count": 50,  # 1% — below 3% floor
        "active_universe": 5000,
    })
    r = await check_liquidity_tiers_freshness(pool)
    assert r.passed is False
    reasons = [f.reason for f in r.failures]
    assert "insufficient_t1_t2_coverage" in reasons


async def test_fails_when_empty_table() -> None:
    pool = _Pool({
        "latest": None,
        "rows_total": 0,
        "t1_t2_count": 0,
        "active_universe": 0,
    })
    r = await check_liquidity_tiers_freshness(pool)
    assert r.passed is False
    assert r.failures[0].reason == "empty_table"


def test_max_age_sourced_from_feed_profile() -> None:
    assert MAX_AGE_DAYS == freshness_max_age_days("liquidity_tiers", -1)


def test_t1_t2_coverage_floor_pinned() -> None:
    """Pin the 3% floor — loosening this gate silently is a defect."""
    assert MIN_T1_T2_COVERAGE_PCT == 0.03
