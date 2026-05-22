"""Tests for ``check_macro_indicators_freshness`` — multi-series FRED gate.

Pins:
* GREEN when every expected indicator has a recent observation
  (``latest_date`` within ``MAX_AGE_DAYS``, ``rows_total > 0``).
* RED ``empty_table`` when the table is empty.
* RED ``missing_indicator`` when an expected indicator has zero rows.
* RED ``stale_indicator`` for an indicator whose newest ``date`` is older
  than ``MAX_AGE_DAYS``.
* ``MAX_AGE_DAYS`` is read from the ``FeedProfile`` SoT.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from tpcore.feeds import freshness_max_age_days
from tpcore.quality.validation.checks.macro_indicators_freshness import (
    CHECK_NAME,
    EXPECTED_INDICATORS,
    MAX_AGE_DAYS,
    check_macro_indicators_freshness,
)


class _Conn:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    async def fetch(self, sql: str, *args: object) -> list[dict[str, Any]]:
        assert "platform.macro_indicators" in sql.lower()
        return self._rows


class _AcquireCM:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _Conn:
        return self._conn

    async def __aexit__(self, *exc: object) -> None:
        return None


class _Pool:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def acquire(self) -> _AcquireCM:
        return _AcquireCM(_Conn(self._rows))


def _fresh_rows() -> list[dict[str, Any]]:
    today = datetime.now(UTC).date() - timedelta(days=5)
    return [
        {"indicator": ind, "latest_date": today, "rows_total": 100}
        for ind in EXPECTED_INDICATORS
    ]


async def test_passes_when_all_indicators_fresh() -> None:
    pool = _Pool(_fresh_rows())
    r = await check_macro_indicators_freshness(pool)
    assert r.passed is True
    assert r.failures == []
    assert r.name == CHECK_NAME


async def test_fails_when_table_empty() -> None:
    pool = _Pool([])
    r = await check_macro_indicators_freshness(pool)
    assert r.passed is False
    assert r.failures[0].reason == "empty_table"


async def test_fails_when_indicator_missing() -> None:
    # Drop one indicator from the rows; the check should flag it.
    rows = [r for r in _fresh_rows() if r["indicator"] != "vix"]
    pool = _Pool(rows)
    r = await check_macro_indicators_freshness(pool)
    assert r.passed is False
    missing = [f for f in r.failures if f.ticker == "vix"]
    assert len(missing) == 1
    assert missing[0].reason == "missing_indicator"


async def test_fails_when_indicator_stale() -> None:
    rows = _fresh_rows()
    # Walk one indicator into the stale-zone.
    today = datetime.now(UTC).date()
    for r in rows:
        if r["indicator"] == "vix":
            r["latest_date"] = today - timedelta(days=MAX_AGE_DAYS + 30)
    pool = _Pool(rows)
    result = await check_macro_indicators_freshness(pool)
    assert result.passed is False
    stale = [f for f in result.failures if f.ticker == "vix"]
    assert len(stale) == 1
    assert stale[0].reason == "stale_indicator"


async def test_fails_when_indicator_has_zero_observations() -> None:
    rows = _fresh_rows()
    for r in rows:
        if r["indicator"] == "vix":
            r["rows_total"] = 0
    pool = _Pool(rows)
    result = await check_macro_indicators_freshness(pool)
    assert result.passed is False
    zero = [f for f in result.failures if f.ticker == "vix"]
    assert len(zero) == 1
    assert zero[0].reason == "zero_observations"


def test_max_age_sourced_from_feed_profile() -> None:
    assert MAX_AGE_DAYS == freshness_max_age_days("macro_indicators", -1)
