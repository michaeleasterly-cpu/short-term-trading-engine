"""Tests for ``check_aaii_sentiment_freshness`` — AAII weekly vendor-anchored.

The check is VENDOR-ANCHORED (#165 facet 4): it reasons from the last
SCHEDULED Thursday publish (in UTC), not "today − N". So:

* GREEN when ``MAX(date)`` ≥ ``expected_latest_publish('aaii_sentiment', now)``.
* RED ``stale`` when our newest row predates the most-recent scheduled
  vendor publish (we missed a publish).
* RED ``empty`` when table is empty.
* ``MAX_AGE_DAYS`` is read from the ``FeedProfile`` SoT.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from tpcore.feeds import freshness_max_age_days
from tpcore.quality.validation.checks.aaii_sentiment_freshness import (
    CHECK_NAME,
    MAX_AGE_DAYS,
    check_aaii_sentiment_freshness,
)


class _Conn:
    def __init__(self, latest):
        self._latest = latest

    async def fetchval(self, sql: str, *args: object) -> Any:
        assert "platform.aaii_sentiment" in sql.lower()
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
    # Today is always ≥ the most recent scheduled Thursday publish — so
    # latest=today is deterministically green regardless of which weekday
    # the test runs (matches the conftest synthetic-healthy-data pattern).
    today = datetime.now(UTC).date()
    pool = _Pool(today)
    r = await check_aaii_sentiment_freshness(pool)
    assert r.passed is True
    assert r.failures == []
    assert r.name == CHECK_NAME


async def test_fails_when_stale() -> None:
    # Far enough behind that EVERY interpretation (vendor-anchored,
    # cadence-fallback) considers us stale — 60 days predates any
    # recent scheduled Thursday publish.
    today = datetime.now(UTC).date()
    pool = _Pool(today - timedelta(days=60))
    r = await check_aaii_sentiment_freshness(pool)
    assert r.passed is False
    assert r.failures[0].reason == "stale"


async def test_fails_when_empty() -> None:
    pool = _Pool(None)
    r = await check_aaii_sentiment_freshness(pool)
    assert r.passed is False
    assert r.failures[0].reason == "empty"


def test_max_age_sourced_from_feed_profile() -> None:
    assert MAX_AGE_DAYS == freshness_max_age_days("aaii_sentiment", -1)
