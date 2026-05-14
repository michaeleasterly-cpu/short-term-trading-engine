"""Unit tests for the SPY market-regime pre-scan gate.

Validates the four cases in ``sigma.scheduler._spy_regime_blocks_entries``:

1. Permissive regime → blocked=False, payload=None
2. Drawdown-recovery regime (SPY down ≥10% from 60d high + rebounding)
   → blocked=True with diagnostic payload
3. High-vol regime (20d annualized vol > 30%) → blocked=True
4. Insufficient data (< 60 SPY bars) → blocked=False (graceful degrade)
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from sigma.scheduler import _spy_regime_blocks_entries


def _row(d: date, close: Decimal) -> dict:
    return {"date": d, "close": close}


class _FakePool:
    """Minimal asyncpg-pool stand-in. Returns a fixed list of SPY rows."""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def acquire(self):
        rows = self._rows

        class _CM:
            async def __aenter__(self_inner):
                return _FakeConn(rows)

            async def __aexit__(self_inner, *_a):
                return None

        return _CM()


class _FakeConn:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    async def fetch(self, sql: str, *args):  # noqa: ARG002
        as_of: date = args[0]
        limit: int = args[1]
        # Mirror the SQL: date <= $1 ORDER BY date DESC LIMIT $2.
        # Return dicts (asyncpg.Record supports __getitem__, dicts do too).
        filtered = [r for r in self._rows if r["date"] <= as_of]
        filtered.sort(key=lambda r: r["date"], reverse=True)
        return filtered[:limit]


def _spy_bars_calm() -> list[dict]:
    """65 bars of a slowly-rising, low-volatility tape — no drawdown, no high vol."""
    base = date(2026, 1, 1)
    return [_row(base + timedelta(days=i), Decimal("100") + Decimal(str(i * 0.05))) for i in range(65)]


def _spy_bars_drawdown_recovery() -> list[dict]:
    """65 bars where SPY drops 15% then rebounds the last 5 days."""
    base = date(2026, 1, 1)
    rows: list[dict] = []
    # First 55 bars: ramp up to 100.
    for i in range(55):
        rows.append(_row(base + timedelta(days=i), Decimal("100")))
    # Next 5 bars: crash to 84 (16% drawdown from peak 100).
    for i in range(5):
        rows.append(_row(base + timedelta(days=55 + i), Decimal("84")))
    # Last 5 bars: rebound 84 → 86 (positive 5d return).
    for i in range(5):
        rows.append(_row(base + timedelta(days=60 + i), Decimal("84") + Decimal(str(i * 0.4))))
    return rows


def _spy_bars_high_vol() -> list[dict]:
    """65 bars alternating ±5% — annualized vol ~70%, no drawdown."""
    base = date(2026, 1, 1)
    rows: list[dict] = []
    price = Decimal("100")
    for i in range(65):
        price = price * (Decimal("1.05") if i % 2 == 0 else Decimal("0.95"))
        rows.append(_row(base + timedelta(days=i), price))
    return rows


@pytest.mark.asyncio
async def test_permissive_regime_returns_false() -> None:
    pool = _FakePool(_spy_bars_calm())
    blocked, payload = await _spy_regime_blocks_entries(pool, date(2026, 4, 1))
    assert blocked is False
    assert payload is None


@pytest.mark.asyncio
async def test_drawdown_recovery_regime_blocks() -> None:
    pool = _FakePool(_spy_bars_drawdown_recovery())
    blocked, payload = await _spy_regime_blocks_entries(pool, date(2026, 4, 1))
    assert blocked is True
    assert payload is not None
    assert payload["trigger_drawdown_recovery"] is True


@pytest.mark.asyncio
async def test_high_vol_regime_blocks() -> None:
    pool = _FakePool(_spy_bars_high_vol())
    blocked, payload = await _spy_regime_blocks_entries(pool, date(2026, 4, 1))
    assert blocked is True
    assert payload is not None
    assert payload["trigger_high_vol"] is True


@pytest.mark.asyncio
async def test_insufficient_data_degrades_to_allow() -> None:
    # Only 30 bars — fewer than the 60-day drawdown lookback.
    base = date(2026, 1, 1)
    rows = [_row(base + timedelta(days=i), Decimal("100")) for i in range(30)]
    pool = _FakePool(rows)
    blocked, payload = await _spy_regime_blocks_entries(pool, date(2026, 4, 1))
    assert blocked is False
    assert payload is None
