"""Unit tests for ``tpcore.backtest.cost_model``.

Covers:
* ``SimpleCostModel.adjusted_fill_price`` direction handling.
* ``SimpleCostModel`` default = T4 round-trip per-side equivalent
  (75 bps per side / 150 bps round-trip), not the legacy 5 bps.
* ``get_round_trip_cost`` returns the ticker's median_spread_pct.
* ``get_round_trip_cost`` falls back to ``DEFAULT_ROUND_TRIP_COST_PCT``
  when the ticker is missing from ``platform.liquidity_tiers``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import pytest

from tpcore.backtest.cost_model import (
    DEFAULT_PER_SIDE_SLIPPAGE_BPS,
    DEFAULT_ROUND_TRIP_COST_PCT,
    SimpleCostModel,
    get_round_trip_cost,
)


def test_default_slippage_matches_t4_round_trip() -> None:
    """Default per-side slippage is half of the T4 round-trip default."""
    m = SimpleCostModel()
    assert m.slippage_bps == DEFAULT_PER_SIDE_SLIPPAGE_BPS
    # Round-trip via buy then sell at the same ref price.
    ref = Decimal("100.00")
    buy_fill = m.adjusted_fill_price(ref, "buy")
    sell_fill = m.adjusted_fill_price(ref, "sell")
    round_trip_loss = (buy_fill - sell_fill) / ref
    # 75 bps per side → 150 bps round-trip = 1.5% = DEFAULT_ROUND_TRIP_COST_PCT.
    assert round_trip_loss == DEFAULT_ROUND_TRIP_COST_PCT


def test_adjusted_fill_price_directions() -> None:
    m = SimpleCostModel(slippage_bps=Decimal("100"))  # 1%
    ref = Decimal("100")
    assert m.adjusted_fill_price(ref, "buy") == Decimal("101.00")
    assert m.adjusted_fill_price(ref, "sell") == Decimal("99.00")


def test_adjusted_fill_price_invalid_side_raises() -> None:
    m = SimpleCostModel()
    with pytest.raises(ValueError):
        m.adjusted_fill_price(Decimal("100"), "long")


# ── DB-backed lookup with a fake pool ───────────────────────────────────


@dataclass
class _Recorded:
    sql: str
    args: tuple


class _FakeConn:
    def __init__(self, *, fetchrow_handler=None) -> None:
        self._fetchrow = fetchrow_handler or (lambda sql, *args: None)
        self.calls: list[_Recorded] = []

    async def fetchrow(self, sql: str, *args) -> Any:
        self.calls.append(_Recorded(sql=sql, args=args))
        return self._fetchrow(sql, *args)


class _FakeAcquireCM:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc) -> None:
        return None


@dataclass
class _FakePool:
    conn: _FakeConn = field(default_factory=_FakeConn)

    def acquire(self) -> _FakeAcquireCM:
        return _FakeAcquireCM(self.conn)


@pytest.mark.asyncio
async def test_get_round_trip_cost_returns_median_for_known_ticker() -> None:
    def handler(sql: str, *args):
        if args == ("SPY",):
            return {
                "tier": 1,
                "median_spread_pct": Decimal("0.000123"),
                "provisional": False,
                "last_updated": None,
            }
        return None

    pool = _FakePool(_FakeConn(fetchrow_handler=handler))
    cost = await get_round_trip_cost(pool, "SPY")
    assert cost == Decimal("0.000123")


@pytest.mark.asyncio
async def test_get_round_trip_cost_falls_back_to_default_when_unknown() -> None:
    pool = _FakePool(_FakeConn(fetchrow_handler=lambda sql, *args: None))
    cost = await get_round_trip_cost(pool, "NOSUCH")
    assert cost == DEFAULT_ROUND_TRIP_COST_PCT


@pytest.mark.asyncio
async def test_get_round_trip_cost_falls_back_when_median_is_null() -> None:
    """Defensive: row present but median is NULL (shouldn't happen given
    NOT NULL on the column, but the helper should still be safe)."""
    def handler(sql: str, *args):
        return {
            "tier": 4,
            "median_spread_pct": None,
            "provisional": True,
            "last_updated": None,
        }

    pool = _FakePool(_FakeConn(fetchrow_handler=handler))
    cost = await get_round_trip_cost(pool, "EDGE")
    assert cost == DEFAULT_ROUND_TRIP_COST_PCT
