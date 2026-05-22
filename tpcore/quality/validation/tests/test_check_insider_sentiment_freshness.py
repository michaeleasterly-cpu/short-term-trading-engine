"""Tests for ``check_insider_sentiment_freshness`` — Finnhub MSPR monthly gate.

The check is freshness-only (no invented coverage threshold — the
catalyst episode lesson) and measures age in MONTHS via
``year * 12 + month``.

Pins:
* GREEN when newest period is within ``MAX_AGE_MONTHS``.
* RED ``stale`` when newest period is older than ``MAX_AGE_MONTHS``.
* RED ``empty`` when table is empty (zero rows).
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from tpcore.quality.validation.checks.insider_sentiment_freshness import (
    CHECK_NAME,
    MAX_AGE_MONTHS,
    check_insider_sentiment_freshness,
)


def _current_period() -> int:
    now = datetime.now(UTC)
    return now.year * 12 + now.month


class _Conn:
    def __init__(self, *, newest_period, rows_total: int) -> None:
        self._row = {"newest_period": newest_period, "rows_total": rows_total}

    async def fetchrow(self, sql: str, *args: object) -> dict[str, Any] | None:
        assert "platform.insider_sentiment" in sql.lower()
        return self._row


class _AcquireCM:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _Conn:
        return self._conn

    async def __aexit__(self, *exc: object) -> None:
        return None


class _Pool:
    def __init__(self, *, newest_period, rows_total: int) -> None:
        self._conn = _Conn(newest_period=newest_period, rows_total=rows_total)

    def acquire(self) -> _AcquireCM:
        return _AcquireCM(self._conn)


async def test_passes_when_fresh() -> None:
    cur = _current_period()
    # 1 month old — well within 3-month allowance
    pool = _Pool(newest_period=cur - 1, rows_total=10)
    r = await check_insider_sentiment_freshness(pool)
    assert r.passed is True
    assert r.failures == []
    assert r.name == CHECK_NAME


async def test_fails_when_stale() -> None:
    cur = _current_period()
    pool = _Pool(newest_period=cur - (MAX_AGE_MONTHS + 2), rows_total=10)
    r = await check_insider_sentiment_freshness(pool)
    assert r.passed is False
    assert r.failures[0].reason == "stale"


async def test_fails_when_empty_zero_rows() -> None:
    pool = _Pool(newest_period=None, rows_total=0)
    r = await check_insider_sentiment_freshness(pool)
    assert r.passed is False
    assert r.failures[0].reason == "empty"


async def test_fails_when_empty_zero_rows_with_period_none() -> None:
    """Even if the aggregate row exists with newest_period=None
    (no rows in table), the check must red as empty, not crash."""
    pool = _Pool(newest_period=None, rows_total=0)
    r = await check_insider_sentiment_freshness(pool)
    assert r.passed is False
    assert r.failures[0].reason == "empty"


def test_max_age_months_is_three_per_freshness_policy() -> None:
    """Freshness policy: monthly cadence, 3-month tolerance. Pin the
    value so a silent drift loosens the gate."""
    assert MAX_AGE_MONTHS == 3
