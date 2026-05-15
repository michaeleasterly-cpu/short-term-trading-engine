"""Tests for the zero-tolerance prices_daily_completeness invariant.

The check derives its expected NYSE sessions from the real
``tpcore.calendar`` (XNYS), so these tests use a fake pool that captures
the window the check requested and returns synthetic per-ticker
coverage aligned to that real session list. This keeps the calendar
truth real (ungameable) while letting each test inject a precise
coverage shape.
"""
from __future__ import annotations

from typing import Any

import pytest

from tpcore.quality.validation.checks import prices_daily_completeness as pdc
from tpcore.quality.validation.checks.prices_daily_completeness import (
    check_prices_daily_completeness,
)


class _Conn:
    def __init__(self, owner: _Pool) -> None:
        self._owner = owner

    async def fetch(self, sql: str, *args) -> list[dict[str, Any]]:
        # args = (tier, min_vol, window_sessions, window_start)
        self._owner.window = list(args[2])
        return self._owner.row_builder(self._owner.window)


class _AcquireCM:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _Conn:
        return self._conn

    async def __aexit__(self, *exc) -> None:
        return None


class _Pool:
    """Fake pool whose fetch result is built from the real window."""

    def __init__(self, row_builder) -> None:
        self.row_builder = row_builder
        self.window: list = []

    def acquire(self) -> _AcquireCM:
        return _AcquireCM(_Conn(self))


async def test_full_coverage_passes() -> None:
    """A liquid live name with every window session present → pass."""

    def builder(window):
        return [{
            "ticker": "AAPL",
            "first_bar": window[0],
            "last_bar": window[-1],
            "window_dates": list(window),
        }]

    result = await check_prices_daily_completeness(_Pool(builder))
    assert result.passed is True
    assert result.failed == 0
    assert result.name == "prices_daily_completeness"


async def test_single_missing_session_fails() -> None:
    """One missing session inside the active range → zero tolerance fail."""

    def builder(window):
        present = list(window)
        dropped = present.pop(len(present) // 2)  # drop a mid-window session
        builder.dropped = dropped
        return [{
            "ticker": "AAPL",
            "first_bar": window[0],
            "last_bar": window[-1],
            "window_dates": present,
        }]

    result = await check_prices_daily_completeness(_Pool(builder))
    assert result.passed is False
    assert result.failed == 1
    f = result.failures[0]
    assert f.ticker == "AAPL"
    assert f.reason == "missing_session"
    assert builder.dropped.isoformat() in f.observed


async def test_dark_name_is_excluded_not_failed() -> None:
    """A liquid name that went fully dark is a halt/delist, not a gap."""

    def builder(window):
        # last_bar well before the live floor → excluded by liveness gate
        return [{
            "ticker": "DEADCO",
            "first_bar": window[0],
            "last_bar": window[0],  # only the oldest session, then silent
            "window_dates": [window[0]],
        }]

    result = await check_prices_daily_completeness(_Pool(builder))
    # Excluded, so it neither passes-with-gap nor fails the invariant.
    assert result.passed is True
    assert result.failed == 0


async def test_pre_ipo_sessions_not_demanded() -> None:
    """Sessions before the ticker's first bar are never required."""

    def builder(window):
        # IPO'd mid-window: only the back half exists, and it's complete.
        half = len(window) // 2
        return [{
            "ticker": "NEWCO",
            "first_bar": window[half],
            "last_bar": window[-1],
            "window_dates": list(window[half:]),
        }]

    result = await check_prices_daily_completeness(_Pool(builder))
    assert result.passed is True
    assert result.failed == 0


async def test_empty_universe_fails_loud() -> None:
    """No liquid names resolved → cannot verify → explicit failure."""
    result = await check_prices_daily_completeness(_Pool(lambda w: []))
    assert result.passed is False
    assert result.failures[0].reason == "empty_liquid_universe"


async def test_broken_calendar_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the calendar yields no sessions, fail rather than vacuously pass."""
    monkeypatch.setattr(pdc.cal, "sessions_in_range", lambda *a, **k: [])
    result = await check_prices_daily_completeness(_Pool(lambda w: []))
    assert result.passed is False
    assert result.failures[0].reason == "no_sessions"
