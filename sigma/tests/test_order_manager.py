"""Tests for ``sigma.order_manager.SigmaOrderManager``.

Mocks the broker and risk governor — never hits Alpaca. Covers:
    * Full submit flow (gate → governor → broker.submit_execution_decision).
    * Tier 1 fill drives ``handle_tier1_fill`` and a partial AAR.
    * Tier 2 fill drives a final combined-P&L AAR.
    * Hard stop before Tier 1 fills cancels the open Tier 2 limit.
    * Daily loss limit blocks a new submission.
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock

from tpcore.aar.models import ExitReason
from tpcore.interfaces.broker import (
    Order,
    OrderClass,
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
)
from tpcore.risk.governor import (
    InMemoryRiskStateStore,
    RiskGovernor,
)

from sigma.models import ExecutionDecision, Phase, PhaseAssessment
from sigma.order_manager import ENGINE_ID, SigmaOrderManager
from sigma.plugs.aar_logging import SigmaAARLogging
from sigma.plugs.capital_gate import SigmaCapitalGate
from sigma.plugs.lifecycle_analysis import SigmaLifecycleAnalysis


# ────────────────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ────────────────────────────────────────────────────────────────────────────


def _assessment(ticker: str = "AAPL") -> PhaseAssessment:
    return PhaseAssessment(
        ticker=ticker,
        as_of=date(2026, 5, 9),
        phase=Phase.ACTIVE,
        entry_price=Decimal("180.00"),
        stop_price=Decimal("174.60"),
        take_profit_mid=Decimal("184.00"),
        take_profit_far=Decimal("188.00"),
    )


def _decision(ticker: str = "AAPL", trade_key: str = "AAPL_1700000000") -> ExecutionDecision:
    return ExecutionDecision(
        ticker=ticker,
        qty=8,
        tier1_qty=4,
        tier2_qty=4,
        notional_usd=Decimal("1440.00"),
        risk_amount_usd=Decimal("43.20"),
        order_payloads=[
            {
                "symbol": ticker,
                "qty": "4",
                "side": "buy",
                "type": "market",
                "time_in_force": "day",
                "order_class": "bracket",
                "take_profit": {"limit_price": "184.00"},
                "stop_loss": {"stop_price": "174.60"},
                "client_order_id": f"{trade_key}_tier1",
            },
            {
                "symbol": ticker,
                "qty": "4",
                "side": "sell",
                "type": "limit",
                "limit_price": "188.00",
                "time_in_force": "gtc",
                "client_order_id": f"{trade_key}_tier2",
            },
        ],
        constructed_at=datetime(2026, 5, 9, 13, 30, tzinfo=UTC),
    )


def _placed_order(
    *,
    client_order_id: str,
    side: OrderSide,
    order_type: OrderType,
    status: OrderStatus = OrderStatus.NEW,
    qty: Decimal = Decimal("4"),
    filled_qty: Decimal = Decimal("0"),
    avg_fill_price: Decimal | None = None,
    filled_at: datetime | None = None,
    broker_order_id: str | None = None,
    order_class: OrderClass = OrderClass.SIMPLE,
    take_profit_limit_price: Decimal | None = None,
    stop_loss_stop_price: Decimal | None = None,
    submitted_at: datetime | None = None,
    time_in_force: TimeInForce = TimeInForce.DAY,
) -> Order:
    return Order(
        client_order_id=client_order_id,
        broker_order_id=broker_order_id,
        symbol="AAPL",
        side=side,
        qty=qty,
        order_type=order_type,
        time_in_force=time_in_force,
        status=status,
        filled_qty=filled_qty,
        avg_fill_price=avg_fill_price,
        submitted_at=submitted_at or datetime(2026, 5, 9, 13, 30, tzinfo=UTC),
        filled_at=filled_at,
        order_class=order_class,
        take_profit_limit_price=take_profit_limit_price,
        stop_loss_stop_price=stop_loss_stop_price,
        engine_id="sigma",
    )


def _broker_mock(open_position_count: int = 0) -> AsyncMock:
    broker = AsyncMock()
    broker.get_positions.return_value = []
    broker.emergency_cancel_all.return_value = 0
    broker.list_recent_orders.return_value = []
    broker.cancel_order.return_value = None
    broker.submit_execution_decision.return_value = []
    broker.get_account.return_value = type(
        "Acct", (), {"equity": Decimal("10000"), "paper": True}
    )()
    return broker


async def _make_manager(
    *,
    broker: AsyncMock | None = None,
    starting_equity: Decimal = Decimal("10000"),
) -> tuple[SigmaOrderManager, AsyncMock, RiskGovernor]:
    broker = broker or _broker_mock()
    store = InMemoryRiskStateStore()
    governor = RiskGovernor(state_store=store, broker=broker, platform_capital=starting_equity)
    await governor.register_engine(ENGINE_ID, starting_equity)
    manager = SigmaOrderManager(
        broker=broker,
        governor=governor,
        capital_gate=SigmaCapitalGate(engine_equity=starting_equity),
        lifecycle=SigmaLifecycleAnalysis(),
        aar=SigmaAARLogging(),
    )
    return manager, broker, governor


# ────────────────────────────────────────────────────────────────────────────
# submit flow
# ────────────────────────────────────────────────────────────────────────────


async def test_submit_decision_runs_full_pipeline() -> None:
    manager, broker, governor = await _make_manager()
    decision = _decision()
    placed_orders = [
        _placed_order(
            client_order_id="AAPL_1700000000_tier1",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            broker_order_id="alp-1",
            order_class=OrderClass.BRACKET,
            take_profit_limit_price=Decimal("184.00"),
            stop_loss_stop_price=Decimal("174.60"),
        ),
        _placed_order(
            client_order_id="AAPL_1700000000_tier2",
            side=OrderSide.SELL,
            order_type=OrderType.LIMIT,
            broker_order_id="alp-2",
            time_in_force=TimeInForce.GTC,
        ),
    ]
    broker.submit_execution_decision.return_value = placed_orders

    result = await manager.submit_decision(decision, _assessment())

    assert result == placed_orders
    broker.submit_execution_decision.assert_awaited_once_with(decision)
    state = await governor._store.get(ENGINE_ID)  # noqa: SLF001 — read-only peek
    assert state is not None
    assert state.open_positions == 1


async def test_submit_blocked_when_capital_gate_rejects() -> None:
    manager, broker, _ = await _make_manager()
    # SigmaCapitalGate caps single-trade notional at $1,500. $1,501 trips it.
    big = _decision().model_copy(update={"notional_usd": Decimal("1501.00")})
    result = await manager.submit_decision(big, _assessment())
    assert result is None
    broker.submit_execution_decision.assert_not_awaited()


async def test_submit_blocked_when_governor_blocks_after_daily_loss() -> None:
    manager, broker, governor = await _make_manager()
    # Push the engine past the 5% daily loss floor.
    await governor.record_fill(ENGINE_ID, realized_pnl=Decimal("-501"), position_delta=0)
    result = await manager.submit_decision(_decision(), _assessment())
    assert result is None
    broker.submit_execution_decision.assert_not_awaited()


# ────────────────────────────────────────────────────────────────────────────
# Reconcile — tier 1 fill, tier 2 fill, hard stop
# ────────────────────────────────────────────────────────────────────────────


async def test_reconcile_logs_tier1_partial_aar_and_handles_fill() -> None:
    manager, broker, _ = await _make_manager()
    # Pre-seed manager with the assessment for this trade key, as if we'd
    # submitted earlier.
    manager._trade_assessments["AAPL_1700000000"] = _assessment()  # noqa: SLF001

    broker.list_recent_orders.return_value = [
        _placed_order(
            client_order_id="AAPL_1700000000_tier1",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            broker_order_id="alp-1",
            order_class=OrderClass.BRACKET,
            status=OrderStatus.FILLED,
            filled_qty=Decimal("4"),
            avg_fill_price=Decimal("184.00"),
            filled_at=datetime(2026, 5, 10, 14, 0, tzinfo=UTC),
        ),
        _placed_order(
            client_order_id="AAPL_1700000000_tier2",
            side=OrderSide.SELL,
            order_type=OrderType.LIMIT,
            broker_order_id="alp-2",
            status=OrderStatus.NEW,
        ),
    ]

    aars = await manager.reconcile(sizing_pct_of_engine_equity=Decimal("0.15"))

    assert len(aars) == 1
    assert aars[0].exit_reason is ExitReason.TIER1_MID_BAND
    assert aars[0].qty == Decimal("4")
    # Lifecycle assessment should now reflect tier1_filled=True with 4 shares left.
    assessment = manager._trade_assessments["AAPL_1700000000"]  # noqa: SLF001
    assert assessment.tier1_filled is True
    assert assessment.remaining_shares == 4


async def test_reconcile_logs_tier2_final_aar_with_combined_pnl() -> None:
    manager, broker, governor = await _make_manager()
    manager._trade_assessments["AAPL_1700000000"] = _assessment()  # noqa: SLF001
    await governor.record_fill(ENGINE_ID, realized_pnl=Decimal("0"), position_delta=1)

    broker.list_recent_orders.return_value = [
        _placed_order(
            client_order_id="AAPL_1700000000_tier1",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            broker_order_id="alp-1",
            order_class=OrderClass.BRACKET,
            status=OrderStatus.FILLED,
            filled_qty=Decimal("4"),
            avg_fill_price=Decimal("184.00"),
            filled_at=datetime(2026, 5, 10, 14, 0, tzinfo=UTC),
        ),
        _placed_order(
            client_order_id="AAPL_1700000000_tier2",
            side=OrderSide.SELL,
            order_type=OrderType.LIMIT,
            broker_order_id="alp-2",
            status=OrderStatus.FILLED,
            filled_qty=Decimal("4"),
            avg_fill_price=Decimal("188.00"),
            filled_at=datetime(2026, 5, 11, 18, 30, tzinfo=UTC),
        ),
    ]

    aars = await manager.reconcile(sizing_pct_of_engine_equity=Decimal("0.15"))

    # Two AARs in one call: the partial Tier 1 (because we hadn't logged it yet)
    # plus the final Tier 2.
    assert len(aars) == 2
    final = aars[-1]
    assert final.exit_reason is ExitReason.TIER2_OPPOSITE_BAND
    assert final.qty == Decimal("8")
    # 4 × (184−180) + 4 × (188−180) = 16 + 32 = 48.
    assert final.pnl_gross == Decimal("48.00")

    # Open-position counter should be back to zero after the final.
    state = await governor._store.get(ENGINE_ID)  # noqa: SLF001
    assert state is not None
    assert state.open_positions == 0


async def test_reconcile_cancels_tier2_when_tier1_cancelled_before_fill() -> None:
    """Hard stop fires before Tier 1 fills → bracket cancels Tier 1, leaving the
    standalone Tier 2 limit live. The order manager must cancel it."""
    manager, broker, _ = await _make_manager()
    manager._trade_assessments["AAPL_1700000000"] = _assessment()  # noqa: SLF001

    broker.list_recent_orders.return_value = [
        _placed_order(
            client_order_id="AAPL_1700000000_tier1",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            broker_order_id="alp-1",
            order_class=OrderClass.BRACKET,
            status=OrderStatus.CANCELED,
        ),
        _placed_order(
            client_order_id="AAPL_1700000000_tier2",
            side=OrderSide.SELL,
            order_type=OrderType.LIMIT,
            broker_order_id="alp-2",
            status=OrderStatus.NEW,
        ),
    ]

    aars = await manager.reconcile(sizing_pct_of_engine_equity=Decimal("0.15"))
    assert aars == []
    broker.cancel_order.assert_awaited_once_with("alp-2")


async def test_reconcile_is_idempotent_within_process() -> None:
    manager, broker, _ = await _make_manager()
    manager._trade_assessments["AAPL_1700000000"] = _assessment()  # noqa: SLF001

    broker.list_recent_orders.return_value = [
        _placed_order(
            client_order_id="AAPL_1700000000_tier1",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            broker_order_id="alp-1",
            order_class=OrderClass.BRACKET,
            status=OrderStatus.FILLED,
            filled_qty=Decimal("4"),
            avg_fill_price=Decimal("184.00"),
            filled_at=datetime(2026, 5, 10, 14, 0, tzinfo=UTC),
        ),
        _placed_order(
            client_order_id="AAPL_1700000000_tier2",
            side=OrderSide.SELL,
            order_type=OrderType.LIMIT,
            broker_order_id="alp-2",
            status=OrderStatus.NEW,
        ),
    ]

    first = await manager.reconcile(sizing_pct_of_engine_equity=Decimal("0.15"))
    second = await manager.reconcile(sizing_pct_of_engine_equity=Decimal("0.15"))
    assert len(first) == 1
    assert second == []
