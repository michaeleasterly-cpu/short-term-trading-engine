"""Tests for ``check_prices_daily_freshness`` — per-ticker freshness +
universe-wide staleness + coverage-collapse guard.

Three independent invariants:
1. Every ``CRITICAL_TICKERS`` ticker has a bar within ``CRITICAL_MAX_AGE_DAYS``.
2. ≤ ``UNIVERSE_STALE_PCT_MAX`` of active tickers are older than
   ``UNIVERSE_MAX_AGE_DAYS``.
3. Latest-session distinct-ticker count is ≥ (1 - ``COVERAGE_COLLAPSE_PCT``)
   of the trailing average.

Wave 6 SoT consistency pin (2026-05-22): ``CRITICAL_MAX_AGE_DAYS`` is read
from the FeedProfile via ``freshness_max_age_days('prices_daily', 5)`` —
no hardcoded duplication. A future profile bump propagates automatically.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from tpcore.feeds import freshness_max_age_days
from tpcore.quality.validation.checks.prices_daily_freshness import (
    CHECK_NAME,
    COVERAGE_COLLAPSE_PCT,
    COVERAGE_TRAILING_SESSIONS,
    CRITICAL_MAX_AGE_DAYS,
    CRITICAL_TICKERS,
    UNIVERSE_MAX_AGE_DAYS,
    UNIVERSE_STALE_PCT_MAX,
    check_prices_daily_freshness,
)


class _Conn:
    def __init__(
        self,
        *,
        critical_ages_days: dict[str, int | None],
        active_tickers: int,
        stale_tickers: int,
        coverage_rows: list[dict[str, Any]],
    ) -> None:
        self._critical_ages_days = critical_ages_days
        self._active = active_tickers
        self._stale = stale_tickers
        self._coverage_rows = coverage_rows

    async def fetch(self, sql: str, *args: object) -> list[dict[str, Any]]:
        sql_lower = sql.lower()
        today = datetime.now(UTC).date()
        # Critical-ticker probe
        if "unnest($1::text[])" in sql_lower:
            tickers = list(args[0])
            out = []
            for t in tickers:
                age = self._critical_ages_days.get(t)
                last_bar = (
                    None if age is None else today - timedelta(days=age)
                )
                out.append({"ticker": t, "last_bar": last_bar})
            return out
        # Coverage-collapse probe (rows ordered date DESC, latest first)
        if "count(distinct ticker)" in sql_lower:
            return self._coverage_rows
        raise AssertionError(f"unexpected fetch SQL: {sql}")

    async def fetchrow(self, sql: str, *args: object) -> dict[str, Any] | None:
        # Universe-wide probe
        assert "active_tickers" in sql.lower()
        return {
            "active_tickers": self._active,
            "stale_tickers": self._stale,
        }


class _AcquireCM:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _Conn:
        return self._conn

    async def __aexit__(self, *exc: object) -> None:
        return None


class _Pool:
    def __init__(self, **kwargs: Any) -> None:
        self._kwargs = kwargs

    def acquire(self) -> _AcquireCM:
        return _AcquireCM(_Conn(**self._kwargs))


def _healthy_coverage_rows(latest_n: int = 5000) -> list[dict[str, Any]]:
    """COVERAGE_TRAILING_SESSIONS+1 sessions; first row is latest with
    ``latest_n`` distinct tickers; trailing rows all have ``latest_n`` so
    the latest is exactly at the trailing avg (no collapse)."""
    today = datetime.now(UTC).date()
    return [
        {"date": today - timedelta(days=i), "n": latest_n}
        for i in range(COVERAGE_TRAILING_SESSIONS + 1)
    ]


async def test_passes_when_all_critical_fresh_and_universe_clean() -> None:
    pool = _Pool(
        critical_ages_days={t: 1 for t in CRITICAL_TICKERS},
        active_tickers=5000,
        stale_tickers=0,
        coverage_rows=_healthy_coverage_rows(),
    )
    r = await check_prices_daily_freshness(pool)
    assert r.passed is True
    assert r.failures == []
    assert r.name == CHECK_NAME


async def test_fails_when_critical_ticker_missing() -> None:
    ages: dict[str, int | None] = {t: 1 for t in CRITICAL_TICKERS}
    ages["SPY"] = None  # missing entirely
    pool = _Pool(
        critical_ages_days=ages,
        active_tickers=5000,
        stale_tickers=0,
        coverage_rows=_healthy_coverage_rows(),
    )
    r = await check_prices_daily_freshness(pool)
    assert r.passed is False
    reasons = [f.reason for f in r.failures]
    assert "missing_ticker" in reasons


async def test_fails_when_critical_ticker_stale() -> None:
    ages: dict[str, int | None] = {t: 1 for t in CRITICAL_TICKERS}
    ages["SPY"] = CRITICAL_MAX_AGE_DAYS + 10
    pool = _Pool(
        critical_ages_days=ages,
        active_tickers=5000,
        stale_tickers=0,
        coverage_rows=_healthy_coverage_rows(),
    )
    r = await check_prices_daily_freshness(pool)
    assert r.passed is False
    reasons = [f.reason for f in r.failures]
    assert "critical_ticker_stale" in reasons


async def test_fails_when_universe_stale_excess() -> None:
    # 10% of active universe is stale — well above the 2% line
    pool = _Pool(
        critical_ages_days={t: 1 for t in CRITICAL_TICKERS},
        active_tickers=5000,
        stale_tickers=500,
        coverage_rows=_healthy_coverage_rows(),
    )
    r = await check_prices_daily_freshness(pool)
    assert r.passed is False
    reasons = [f.reason for f in r.failures]
    assert "universe_stale_excess" in reasons


async def test_fails_when_coverage_collapses() -> None:
    """The 2026-05-15 daily_bars incident class: MAX(date) looks current
    because a fraction of the universe is publishing today, but
    distinct-ticker count on that day has cratered. Latest = 500;
    trailing avg = 5000; 500 < 5000 * (1 - 0.30) = 3500 → collapse."""
    today = datetime.now(UTC).date()
    rows = [{"date": today, "n": 500}]
    rows += [
        {"date": today - timedelta(days=i + 1), "n": 5000}
        for i in range(COVERAGE_TRAILING_SESSIONS)
    ]
    pool = _Pool(
        critical_ages_days={t: 1 for t in CRITICAL_TICKERS},
        active_tickers=5000,
        stale_tickers=0,
        coverage_rows=rows,
    )
    r = await check_prices_daily_freshness(pool)
    assert r.passed is False
    reasons = [f.reason for f in r.failures]
    assert "coverage_collapse" in reasons


def test_critical_max_age_sourced_from_feed_profile() -> None:
    """Wave 6 SoT lockstep: CRITICAL_MAX_AGE_DAYS must equal the
    ``freshness_max_age_days('prices_daily', ...)`` value. A future
    FeedProfile bump propagates here automatically without a code edit."""
    assert CRITICAL_MAX_AGE_DAYS == freshness_max_age_days("prices_daily", -1)


def test_universe_stale_pct_max_pinned() -> None:
    """Pin the 2% universe-staleness gate; loosening it silently widens
    the silent-failure surface area."""
    assert UNIVERSE_STALE_PCT_MAX == 0.02


def test_universe_max_age_days_pinned() -> None:
    """Pin the 14d universe-tail tolerance. No separate FeedProfile entry
    exists for this dimension (the profile tracks the critical/SLA
    threshold). When the profile ever models the universe-tail this pin
    must update in lockstep — that's the design contract."""
    assert UNIVERSE_MAX_AGE_DAYS == 14


def test_coverage_collapse_pct_pinned() -> None:
    assert COVERAGE_COLLAPSE_PCT == 0.30
    assert COVERAGE_TRAILING_SESSIONS == 20
