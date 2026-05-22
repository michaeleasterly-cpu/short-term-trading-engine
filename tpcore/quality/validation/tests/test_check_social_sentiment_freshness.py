"""Tests for ``check_social_sentiment_freshness`` — ApeWisdom freshness +
T1/T2 coverage floor.

Pins:
* GREEN when latest is within ``MAX_AGE_DAYS`` and coverage ≥ floor.
* RED ``stale`` when latest is older than ``MAX_AGE_DAYS``.
* RED ``low_coverage`` when fewer than ``MIN_COVERAGE_PCT`` of T1+T2 are covered.
* RED ``empty`` when there are zero rows in the table.
* ``MAX_AGE_DAYS`` is read from the ``FeedProfile`` SoT.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from tpcore.feeds import freshness_max_age_days
from tpcore.quality.validation.checks.social_sentiment_freshness import (
    CHECK_NAME,
    MAX_AGE_DAYS,
    MIN_COVERAGE_PCT,
    check_social_sentiment_freshness,
)


class _Conn:
    def __init__(self, *, latest, universe: int, covered: int) -> None:
        self._latest = latest
        self._universe = universe
        self._covered = covered

    async def fetchval(self, sql: str, *args: object) -> Any:
        assert "max(date)" in sql.lower()
        return self._latest

    async def fetchrow(self, sql: str, *args: object) -> dict[str, Any] | None:
        assert "covered" in sql.lower()
        return {"universe": self._universe, "covered": self._covered}


class _AcquireCM:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _Conn:
        return self._conn

    async def __aexit__(self, *exc: object) -> None:
        return None


class _Pool:
    def __init__(self, *, latest, universe: int, covered: int) -> None:
        self._conn = _Conn(latest=latest, universe=universe, covered=covered)

    def acquire(self) -> _AcquireCM:
        return _AcquireCM(self._conn)


async def test_passes_when_fresh_and_above_coverage_floor() -> None:
    today = datetime.now(UTC).date()
    # 50% coverage well above 15% floor
    pool = _Pool(latest=today - timedelta(days=1), universe=100, covered=50)
    r = await check_social_sentiment_freshness(pool)
    assert r.passed is True
    assert r.failures == []
    assert r.name == CHECK_NAME


async def test_fails_when_stale() -> None:
    today = datetime.now(UTC).date()
    pool = _Pool(
        latest=today - timedelta(days=MAX_AGE_DAYS + 5),
        universe=100, covered=50,
    )
    r = await check_social_sentiment_freshness(pool)
    assert r.passed is False
    reasons = [f.reason for f in r.failures]
    assert "stale" in reasons


async def test_fails_when_coverage_below_floor() -> None:
    today = datetime.now(UTC).date()
    # ~5% coverage well below 15% floor
    pool = _Pool(latest=today - timedelta(days=1), universe=100, covered=5)
    r = await check_social_sentiment_freshness(pool)
    assert r.passed is False
    reasons = [f.reason for f in r.failures]
    assert "low_coverage" in reasons


async def test_fails_when_empty() -> None:
    pool = _Pool(latest=None, universe=0, covered=0)
    r = await check_social_sentiment_freshness(pool)
    assert r.passed is False
    assert r.failures[0].reason == "empty"


def test_max_age_sourced_from_feed_profile() -> None:
    assert MAX_AGE_DAYS == freshness_max_age_days(
        "apewisdom_social_sentiment", -1
    )


def test_min_coverage_is_evidence_based_floor() -> None:
    """The coverage floor must be a reachable, evidence-derived number
    (not the previous structurally-unreachable 30%). 15% is the canonical
    value set 2026-05-16 — guard against silent regressions."""
    assert MIN_COVERAGE_PCT == 0.15
