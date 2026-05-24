"""Tests for ``check_fear_greed_freshness`` — recomputed-each-session gate.

The check measures age in NYSE trading sessions via ``tpcore.calendar``
so weekends/holidays don't false-fail.

Pins:
* GREEN when the gap (in NYSE sessions) is ≤ ``MAX_AGE_TRADING_DAYS``.
* RED ``stale`` when the gap exceeds the threshold.
* RED ``empty`` when table has no rows.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from tpcore.quality.validation.checks.fear_greed_freshness import (
    CHECK_NAME,
    MAX_AGE_TRADING_DAYS,
    check_fear_greed_freshness,
)


class _Conn:
    def __init__(self, latest):
        self._latest = latest

    async def fetchval(self, sql: str, *args: object) -> Any:
        # Task #18 P7: reads platform.macro_data with source='cnn_fear_greed'.
        sl = sql.lower()
        assert "platform.macro_data" in sl and "'cnn_fear_greed'" in sl
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
    # Yesterday — well within 3 NYSE-session threshold.
    today = datetime.now(UTC).date()
    pool = _Pool(today - timedelta(days=1))
    r = await check_fear_greed_freshness(pool)
    assert r.passed is True
    assert r.failures == []
    assert r.name == CHECK_NAME


async def test_fails_when_stale() -> None:
    # 30 calendar days = ~21 trading sessions, well past 3.
    today = datetime.now(UTC).date()
    pool = _Pool(today - timedelta(days=30))
    r = await check_fear_greed_freshness(pool)
    assert r.passed is False
    assert r.failures[0].reason == "stale"


async def test_fails_when_empty() -> None:
    pool = _Pool(None)
    r = await check_fear_greed_freshness(pool)
    assert r.passed is False
    assert r.failures[0].reason == "empty"


def test_max_age_three_trading_sessions() -> None:
    """Pin the threshold; loosening this gate silently is a defect."""
    assert MAX_AGE_TRADING_DAYS == 3
