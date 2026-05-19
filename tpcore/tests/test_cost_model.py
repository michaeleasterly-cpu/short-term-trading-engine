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

from reversion.backtest import _TIER_ROUND_TRIP_COSTS as _ReversionTierCosts
from reversion.backtest import _slippage_per_side as _reversion_slippage
from tpcore.backtest.cost_model import (
    DEFAULT_PER_SIDE_SLIPPAGE_BPS,
    DEFAULT_ROUND_TRIP_COST_PCT,
    SimpleCostModel,
    capital_gate_healthcheck,
    get_round_trip_cost,
    slippage_per_side,
)
from vector.backtest import _TIER_ROUND_TRIP_COSTS as _VectorTierCosts
from vector.backtest import _slippage_per_side as _vector_slippage


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


# ── P5.2.1 characterization: slippage_per_side consolidation (#11) ───────
#
# Independent hardcoded expected values (NOT the engine fn as oracle —
# that becomes tautological once the engine delegates). The engine
# constant SLIPPAGE_PER_SIDE == 0.0005 (both engines); the per-tier
# round-trip cost is halved for a per-side value.


def test_slippage_per_side_known_tier_ticker() -> None:
    tiers = {"SPY": 0.0030, "AAPL": 0.0150}
    # Known ticker → round-trip / 2.
    assert slippage_per_side("SPY", tiers, 0.0005) == 0.0015
    assert slippage_per_side("AAPL", tiers, 0.0005) == 0.0075


def test_slippage_per_side_unknown_ticker_uses_default() -> None:
    tiers = {"SPY": 0.0030}
    assert slippage_per_side("NOSUCH", tiers, 0.0005) == 0.0005


def test_slippage_per_side_empty_tier_dict_uses_default() -> None:
    assert slippage_per_side("ANY", {}, 0.0005) == 0.0005


def test_slippage_per_side_reversion_delegate_matches_literal() -> None:
    """Engine delegate must pass its own constant + tier dict correctly.

    Pinned against independent literals, then we prove the reversion
    delegate routes the same args to the shared fn. Private engine
    names are bound via import aliases (no ``module._x`` attribute
    access — SLF001-clean, mirroring the P5.1 de-tautologized
    ``test_cli_overrides`` pattern); the tier dict is mutated through
    the imported dict object itself.
    """
    rt_costs = _ReversionTierCosts
    rt_costs.clear()
    rt_costs.update({"SPY": 0.0030})
    try:
        assert _reversion_slippage("SPY") == 0.0015  # 0.0030 / 2
        assert _reversion_slippage("NOSUCH") == 0.0005  # SLIPPAGE_PER_SIDE
    finally:
        rt_costs.clear()


def test_slippage_per_side_vector_delegate_matches_literal() -> None:
    rt_costs = _VectorTierCosts
    rt_costs.clear()
    rt_costs.update({"AAPL": 0.0150})
    try:
        assert _vector_slippage("AAPL") == 0.0075  # 0.0150 / 2
        assert _vector_slippage("NOSUCH") == 0.0005  # SLIPPAGE_PER_SIDE
    finally:
        rt_costs.clear()


# ── P5.2.2 characterization: capital_gate_healthcheck consolidation (#7) ─
#
# Pinned against a hardcoded expected dict incl. the correct per-engine
# `engine` value; then the engine plug delegate must return exactly it.


def test_capital_gate_healthcheck_shape() -> None:
    out = capital_gate_healthcheck(
        "reversion",
        Decimal("10000"),
        Decimal("2000"),
        5,
    )
    assert out == {
        "engine": "reversion",
        "plug": "capital_gate",
        "ok": True,
        "details": {
            "engine_equity_usd": "10000",
            "max_position_usd": "2000",
            "max_positions": 5,
        },
    }


def test_capital_gate_healthcheck_reversion_delegate_matches_literal() -> None:
    from reversion.plugs.capital_gate import ReversionCapitalGate

    gate = ReversionCapitalGate(
        engine_equity=Decimal("10000"),
        max_position_usd=Decimal("2000"),
        max_positions=5,
    )
    assert gate.healthcheck() == {
        "engine": "reversion",
        "plug": "capital_gate",
        "ok": True,
        "details": {
            "engine_equity_usd": "10000",
            "max_position_usd": "2000",
            "max_positions": 5,
        },
    }


def test_capital_gate_healthcheck_vector_delegate_matches_literal() -> None:
    from vector.plugs.capital_gate import VectorCapitalGate

    gate = VectorCapitalGate(
        engine_equity=Decimal("10000"),
        max_position_usd=Decimal("2000"),
        max_positions=5,
    )
    assert gate.healthcheck() == {
        "engine": "vector",
        "plug": "capital_gate",
        "ok": True,
        "details": {
            "engine_equity_usd": "10000",
            "max_position_usd": "2000",
            "max_positions": 5,
        },
    }
