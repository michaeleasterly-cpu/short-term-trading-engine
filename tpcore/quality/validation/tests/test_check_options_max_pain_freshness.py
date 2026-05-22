"""Tests for ``check_options_max_pain_freshness`` — greeks.pro daily snapshot
gate.

Pins:
* GREEN when every expected symbol has a snapshot within ``MAX_AGE_DAYS``.
* RED ``stale`` when the latest snapshot is older than ``MAX_AGE_DAYS``.
* RED ``missing_symbol`` when an expected symbol has zero rows.
* ``MAX_AGE_DAYS`` is read from the ``FeedProfile`` SoT for the
  ``greeks_max_pain`` feed.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from tpcore.feeds import freshness_max_age_days
from tpcore.quality.validation.checks.options_max_pain_freshness import (
    CHECK_NAME,
    EXPECTED_SYMBOLS,
    MAX_AGE_DAYS,
    check_options_max_pain_freshness,
)


class _Conn:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    async def fetch(self, sql: str, *args: object) -> list[dict[str, Any]]:
        assert "platform.options_max_pain" in sql.lower()
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


async def test_passes_when_every_expected_symbol_fresh() -> None:
    today = datetime.now(UTC).date()
    rows = [
        {"symbol": s, "latest": today - timedelta(days=1)}
        for s in EXPECTED_SYMBOLS
    ]
    pool = _Pool(rows)
    r = await check_options_max_pain_freshness(pool)
    assert r.passed is True
    assert r.failures == []
    assert r.name == CHECK_NAME


async def test_fails_when_stale() -> None:
    today = datetime.now(UTC).date()
    rows = [
        {"symbol": s, "latest": today - timedelta(days=MAX_AGE_DAYS + 5)}
        for s in EXPECTED_SYMBOLS
    ]
    pool = _Pool(rows)
    r = await check_options_max_pain_freshness(pool)
    assert r.passed is False
    assert all(f.reason == "stale" for f in r.failures)


async def test_fails_when_symbol_missing() -> None:
    pool = _Pool([])  # no rows for any expected symbol
    r = await check_options_max_pain_freshness(pool)
    assert r.passed is False
    assert all(f.reason == "missing_symbol" for f in r.failures)
    assert len(r.failures) == len(EXPECTED_SYMBOLS)


def test_max_age_sourced_from_feed_profile() -> None:
    assert MAX_AGE_DAYS == freshness_max_age_days("greeks_max_pain", -1)
