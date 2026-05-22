"""Tests for ``check_short_interest_freshness`` — FINRA bi-monthly gate.

Pins:
* GREEN when newest ``settlement_date`` is within ``MAX_AGE_DAYS``.
* RED ``stale`` when newest ``settlement_date`` is older than ``MAX_AGE_DAYS``.
* RED ``empty`` when table is empty.
* ``MAX_AGE_DAYS`` is read from the ``FeedProfile`` SoT (42d, cadence-
  derived 2026-05-16: bi-monthly ~16d + ~13d dissemination + ~13d slack).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from tpcore.feeds import freshness_max_age_days
from tpcore.quality.validation.checks.short_interest_freshness import (
    CHECK_NAME,
    MAX_AGE_DAYS,
    check_short_interest_freshness,
)


class _Conn:
    def __init__(self, latest):
        self._latest = latest

    async def fetchval(self, sql: str, *args: object) -> Any:
        assert "platform.short_interest" in sql.lower()
        assert "settlement_date" in sql.lower()
        return self._latest


class _AcquireCM:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _Conn:
        return self._conn

    async def __aexit__(self, *exc: object) -> None:
        return None


class _Pool:
    def __init__(self, latest) -> None:
        self._latest = latest

    def acquire(self) -> _AcquireCM:
        return _AcquireCM(_Conn(self._latest))


async def test_passes_when_fresh() -> None:
    today = datetime.now(UTC).date()
    pool = _Pool(today - timedelta(days=10))
    r = await check_short_interest_freshness(pool)
    assert r.passed is True
    assert r.failures == []
    assert r.name == CHECK_NAME


async def test_fails_when_stale() -> None:
    today = datetime.now(UTC).date()
    pool = _Pool(today - timedelta(days=MAX_AGE_DAYS + 5))
    r = await check_short_interest_freshness(pool)
    assert r.passed is False
    assert r.failures[0].reason == "stale"


async def test_fails_when_empty() -> None:
    pool = _Pool(None)
    r = await check_short_interest_freshness(pool)
    assert r.passed is False
    assert r.failures[0].reason == "empty"


def test_max_age_sourced_from_feed_profile() -> None:
    assert MAX_AGE_DAYS == freshness_max_age_days("finra_short_interest", -1)
