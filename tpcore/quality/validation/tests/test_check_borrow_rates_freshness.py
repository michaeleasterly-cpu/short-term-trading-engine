"""Tests for ``check_borrow_rates_freshness`` — IBorrowDesk freshness gate.

Pins:
* GREEN when newest ``date`` is within ``MAX_AGE_DAYS``.
* RED when newest ``date`` is older than ``MAX_AGE_DAYS`` (``reason=stale``).
* RED when the table is empty (``reason=empty``).
* The threshold is read from the ``FeedProfile`` SoT, not a check-local
  hardcode — a future profile bump propagates here without a code edit.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from tpcore.feeds import freshness_max_age_days
from tpcore.quality.validation.checks.borrow_rates_freshness import (
    CHECK_NAME,
    MAX_AGE_DAYS,
    check_borrow_rates_freshness,
)


class _Conn:
    def __init__(self, latest):
        self._latest = latest

    async def fetchval(self, sql: str, *args: object) -> Any:
        assert "platform.borrow_rates" in sql.lower()
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
    pool = _Pool(today - timedelta(days=1))
    r = await check_borrow_rates_freshness(pool)
    assert r.passed is True
    assert r.failures == []
    assert r.name == CHECK_NAME


async def test_fails_when_stale() -> None:
    today = datetime.now(UTC).date()
    pool = _Pool(today - timedelta(days=MAX_AGE_DAYS + 5))
    r = await check_borrow_rates_freshness(pool)
    assert r.passed is False
    assert r.failures[0].reason == "stale"


async def test_fails_when_empty() -> None:
    pool = _Pool(None)
    r = await check_borrow_rates_freshness(pool)
    assert r.passed is False
    assert r.failures[0].reason == "empty"


def test_max_age_sourced_from_feed_profile() -> None:
    """SoT consistency: MAX_AGE_DAYS must equal the FeedProfile value.

    Drift detector — if anyone hardcodes a constant in the check while
    leaving the import in place, this fails and points at the gap."""
    assert MAX_AGE_DAYS == freshness_max_age_days("iborrowdesk_borrow_rates", -1)
