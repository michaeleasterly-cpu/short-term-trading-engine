"""Tests for ``tpcore.risk.RiskGovernor`` against the in-memory store."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

from tpcore.interfaces.broker import OrderSide, Position
from tpcore.risk.governor import (
    InMemoryRiskStateStore,
    RiskDecision,
    RiskGovernor,
    RiskLimits,
)


def _broker_with_positions(positions: list[Position] | None = None) -> AsyncMock:
    broker = AsyncMock()
    broker.get_positions.return_value = positions or []
    broker.emergency_cancel_all.return_value = 0
    return broker


# ────────────────────────────────────────────────────────────────────────────
# check_trade — happy + each blocking path
# ────────────────────────────────────────────────────────────────────────────


async def test_check_trade_allows_when_state_is_healthy() -> None:
    governor = RiskGovernor(
        state_store=InMemoryRiskStateStore(),
        broker=_broker_with_positions(),
        platform_capital=Decimal("100000"),
    )
    await governor.register_engine("sigma", engine_equity=Decimal("10000"))
    result = await governor.check_trade("sigma", size=Decimal("1500"), direction=OrderSide.BUY)
    assert result.decision is RiskDecision.ALLOW
    assert result.allowed is True


async def test_check_trade_rejects_unregistered_engine() -> None:
    governor = RiskGovernor(state_store=InMemoryRiskStateStore(), broker=_broker_with_positions())
    result = await governor.check_trade("ghost", size=Decimal("500"), direction=OrderSide.BUY)
    assert result.decision is RiskDecision.BLOCK
    assert "no risk state" in (result.reason or "")


async def test_check_trade_rejects_zero_size() -> None:
    governor = RiskGovernor(state_store=InMemoryRiskStateStore(), broker=_broker_with_positions())
    await governor.register_engine("sigma", engine_equity=Decimal("10000"))
    result = await governor.check_trade("sigma", size=Decimal("0"), direction=OrderSide.BUY)
    assert result.decision is RiskDecision.BLOCK


async def test_kill_switch_blocks_all_trades() -> None:
    governor = RiskGovernor(state_store=InMemoryRiskStateStore(), broker=_broker_with_positions())
    await governor.register_engine("sigma", engine_equity=Decimal("10000"))
    await governor.emergency_kill(reason="incident #42")
    result = await governor.check_trade("sigma", size=Decimal("100"), direction=OrderSide.BUY)
    assert result.decision is RiskDecision.BLOCK
    assert "kill switch" in (result.reason or "")


async def test_daily_loss_cap_blocks_new_trades() -> None:
    store = InMemoryRiskStateStore()
    governor = RiskGovernor(state_store=store, broker=_broker_with_positions())
    await governor.register_engine("sigma", engine_equity=Decimal("10000"))
    # 5% of $10k = $500. Tip the engine just past the floor.
    await governor.record_fill("sigma", realized_pnl=Decimal("-501"), position_delta=0)
    result = await governor.check_trade("sigma", size=Decimal("100"), direction=OrderSide.BUY)
    assert result.decision is RiskDecision.BLOCK
    assert "daily loss cap" in (result.reason or "")


async def test_weekly_loss_cap_blocks_new_trades() -> None:
    """Weekly cap (10%) blocks even when daily counter is innocuous."""
    store = InMemoryRiskStateStore()
    governor = RiskGovernor(state_store=store, broker=_broker_with_positions())
    state = await governor.register_engine("sigma", engine_equity=Decimal("10000"))
    # Reset daily so it doesn't dominate; accumulate weekly past the floor.
    state = state.model_copy(
        update={"daily_pnl": Decimal("0"), "weekly_pnl": Decimal("-1100")}
    )
    await store.put(state)
    result = await governor.check_trade("sigma", size=Decimal("100"), direction=OrderSide.BUY)
    assert result.decision is RiskDecision.BLOCK
    assert "weekly loss cap" in (result.reason or "")


async def test_max_concurrent_positions_blocks() -> None:
    governor = RiskGovernor(
        state_store=InMemoryRiskStateStore(),
        broker=_broker_with_positions(),
        limits=RiskLimits(max_open_positions=2),
    )
    await governor.register_engine("sigma", engine_equity=Decimal("10000"))
    await governor.record_fill("sigma", realized_pnl=Decimal("0"), position_delta=2)
    result = await governor.check_trade("sigma", size=Decimal("100"), direction=OrderSide.BUY)
    assert result.decision is RiskDecision.BLOCK
    assert "max concurrent positions" in (result.reason or "")


async def test_platform_net_long_cap_blocks_excess_buy() -> None:
    """A new BUY pushing total long market value past 60% of capital is blocked."""
    existing = Position(
        symbol="MSFT",
        qty=Decimal("100"),
        avg_entry_price=Decimal("400"),
        market_value=Decimal("55000"),  # already at 55% of $100k
    )
    broker = _broker_with_positions([existing])
    governor = RiskGovernor(
        state_store=InMemoryRiskStateStore(),
        broker=broker,
        platform_capital=Decimal("100000"),
    )
    await governor.register_engine("sigma", engine_equity=Decimal("10000"))
    # Adding $6k pushes total to 61% → blocked.
    blocked = await governor.check_trade("sigma", size=Decimal("6000"), direction=OrderSide.BUY)
    assert blocked.decision is RiskDecision.BLOCK
    assert "net-long" in (blocked.reason or "")
    # Adding $4k stays under cap → allowed.
    allowed = await governor.check_trade("sigma", size=Decimal("4000"), direction=OrderSide.BUY)
    assert allowed.decision is RiskDecision.ALLOW


# ────────────────────────────────────────────────────────────────────────────
# Counter reset
# ────────────────────────────────────────────────────────────────────────────


async def test_daily_counter_resets_after_next_open() -> None:
    store = InMemoryRiskStateStore()
    governor = RiskGovernor(state_store=store, broker=_broker_with_positions())
    state = await governor.register_engine("sigma", engine_equity=Decimal("10000"))
    # Backdate the daily reset so the lazy-reset path fires.
    past = state.model_copy(
        update={
            "daily_pnl": Decimal("-400"),
            "daily_reset_at": datetime.now(UTC) - timedelta(days=1),
        }
    )
    await store.put(past)
    await governor.check_trade("sigma", size=Decimal("100"), direction=OrderSide.BUY)
    fresh = await store.get("sigma")
    assert fresh is not None
    assert fresh.daily_pnl == Decimal("0")
    assert fresh.daily_reset_at > datetime.now(UTC)


# ────────────────────────────────────────────────────────────────────────────
# record_fill
# ────────────────────────────────────────────────────────────────────────────


async def test_record_fill_updates_counters() -> None:
    store = InMemoryRiskStateStore()
    governor = RiskGovernor(state_store=store, broker=_broker_with_positions())
    await governor.register_engine("sigma", engine_equity=Decimal("10000"))
    s1 = await governor.record_fill("sigma", realized_pnl=Decimal("50"), position_delta=1)
    assert s1.daily_pnl == Decimal("50")
    assert s1.weekly_pnl == Decimal("50")
    assert s1.open_positions == 1
    s2 = await governor.record_fill("sigma", realized_pnl=Decimal("-30"), position_delta=-1)
    assert s2.daily_pnl == Decimal("20")
    assert s2.weekly_pnl == Decimal("20")
    assert s2.open_positions == 0


# ────────────────────────────────────────────────────────────────────────────
# check_cost — Phase 2 B6 gate
# ────────────────────────────────────────────────────────────────────────────


class _CostFakeConn:
    """Minimal asyncpg connection that returns a fixed median_spread_pct."""

    def __init__(self, median: Decimal | None) -> None:
        self._median = median

    async def fetchrow(self, sql: str, *args):
        if self._median is None:
            return None
        return {
            "tier": 4,
            "median_spread_pct": self._median,
            "provisional": False,
            "last_updated": datetime.now(UTC),
        }


class _CostFakeAcquireCM:
    def __init__(self, conn: _CostFakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _CostFakeConn:
        return self._conn

    async def __aexit__(self, *exc) -> None:
        return None


class _CostFakePool:
    def __init__(self, median: Decimal | None) -> None:
        self._conn = _CostFakeConn(median)

    def acquire(self) -> _CostFakeAcquireCM:
        return _CostFakeAcquireCM(self._conn)


async def test_check_cost_allows_when_cost_below_edge() -> None:
    governor = RiskGovernor(
        state_store=InMemoryRiskStateStore(),
        broker=_broker_with_positions(),
        pool=_CostFakePool(median=Decimal("0.0050")),  # 50 bps
    )
    result = await governor.check_cost("AAPL", expected_edge_pct=Decimal("0.030"))  # 3% edge
    assert result.decision is RiskDecision.ALLOW


async def test_check_cost_blocks_when_cost_exceeds_edge() -> None:
    governor = RiskGovernor(
        state_store=InMemoryRiskStateStore(),
        broker=_broker_with_positions(),
        pool=_CostFakePool(median=Decimal("0.030")),  # 3% spread
    )
    result = await governor.check_cost("WIDE", expected_edge_pct=Decimal("0.010"))  # 1% edge
    assert result.decision is RiskDecision.BLOCK
    assert result.reason is not None
    assert "cost" in result.reason.lower()


async def test_check_cost_allows_when_no_pool_wired() -> None:
    """Tests / dev paths that don't pass a pool see ALLOW — back-compat."""
    governor = RiskGovernor(
        state_store=InMemoryRiskStateStore(),
        broker=_broker_with_positions(),
    )
    result = await governor.check_cost("AAPL", expected_edge_pct=Decimal("0.001"))
    assert result.decision is RiskDecision.ALLOW


async def test_check_cost_uses_t4_default_for_unknown_ticker() -> None:
    """Unknown ticker → T4 default (1.50% round-trip). Edge < 1.50% blocks."""
    governor = RiskGovernor(
        state_store=InMemoryRiskStateStore(),
        broker=_broker_with_positions(),
        pool=_CostFakePool(median=None),  # no row in liquidity_tiers
    )
    blocked = await governor.check_cost("NOSUCH", expected_edge_pct=Decimal("0.010"))
    assert blocked.decision is RiskDecision.BLOCK
    allowed = await governor.check_cost("NOSUCH", expected_edge_pct=Decimal("0.020"))
    assert allowed.decision is RiskDecision.ALLOW


async def test_check_trade_threads_cost_gate_through_when_kwargs_set() -> None:
    """``check_trade(ticker=..., expected_edge_pct=...)`` invokes the cost gate
    after the rest of the checks. If cost > edge, the trade blocks even though
    no other check would have."""
    store = InMemoryRiskStateStore()
    governor = RiskGovernor(
        state_store=store,
        broker=_broker_with_positions(),
        platform_capital=Decimal("100000"),
        pool=_CostFakePool(median=Decimal("0.040")),  # 4% spread
    )
    await governor.register_engine("sigma", engine_equity=Decimal("10000"))
    result = await governor.check_trade(
        "sigma",
        size=Decimal("1500"),
        direction=OrderSide.BUY,
        ticker="WIDE",
        expected_edge_pct=Decimal("0.020"),  # 2% edge
    )
    assert result.decision is RiskDecision.BLOCK
    assert "cost" in (result.reason or "").lower()


async def test_check_trade_back_compat_no_ticker_no_cost_gate() -> None:
    """Existing callers that don't pass ticker still work — cost gate is opt-in."""
    governor = RiskGovernor(
        state_store=InMemoryRiskStateStore(),
        broker=_broker_with_positions(),
        platform_capital=Decimal("100000"),
        pool=_CostFakePool(median=Decimal("0.040")),  # large spread, but should be ignored
    )
    await governor.register_engine("sigma", engine_equity=Decimal("10000"))
    result = await governor.check_trade("sigma", size=Decimal("1500"), direction=OrderSide.BUY)
    assert result.decision is RiskDecision.ALLOW
