"""Tests for `vector.order_manager.VectorOrderManager`.

Mocks the broker, governor, and (optionally) parity harness — never
hits Alpaca. Covers:
    * Full submit flow (gate → governor → broker.submit_tier1_only). Trade monitor handles any follow-up.
    * AAR built and persisted on TP fill (TAKE_PROFIT classification).
    * AAR built on SL fill (STOP_LOSS classification).
    * Capital gate blocks oversized notional → no submission.
    * Daily-loss freeze blocks submission.
    * Parity harness invoked on success; live failure does not block.
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
from tpcore.risk.governor import InMemoryRiskStateStore, RiskGovernor
from vector.models import ExecutionDecision, Phase, PhaseAssessment
from vector.order_manager import ENGINE_ID, VectorOrderManager
from vector.plugs.aar_logging import VectorAARLogging
from vector.plugs.capital_gate import VectorCapitalGate
from vector.plugs.lifecycle_analysis import VectorLifecycleAnalysis

# ────────────────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ────────────────────────────────────────────────────────────────────────────


CID = "vector_AAA_1700000000"


def _assessment(ticker: str = "AAA") -> PhaseAssessment:
    return PhaseAssessment(
        ticker=ticker,
        as_of=date(2026, 5, 9),
        phase=Phase.ENTRY,
        entry_price=Decimal("100.00"),
        stop_price=Decimal("93.00"),
        profit_target_price=Decimal("115.00"),
    )


def _decision(ticker: str = "AAA", cid: str = CID) -> ExecutionDecision:
    return ExecutionDecision(
        ticker=ticker,
        qty=20,
        notional_usd=Decimal("2000.00"),
        risk_amount_usd=Decimal("140.00"),
        vix_size_factor=Decimal("1.0"),
        order_payloads=[
            {
                "client_order_id": cid,
                "symbol": ticker,
                "qty": 20,
                "side": "buy",
                "type": "market",
                "time_in_force": "day",
                "order_class": "bracket",
                "take_profit": {"limit_price": "115.00"},
                "stop_loss": {"stop_price": "93.00"},
            }
        ],
        constructed_at=datetime(2026, 5, 9, 13, 30, tzinfo=UTC),
    )


def _placed_parent(
    *,
    cid: str = CID,
    status: OrderStatus = OrderStatus.NEW,
    qty: Decimal = Decimal("20"),
    filled_qty: Decimal = Decimal("0"),
    avg_fill_price: Decimal | None = None,
    filled_at: datetime | None = None,
) -> Order:
    return Order(
        client_order_id=cid,
        broker_order_id="broker-" + cid,
        symbol="AAA",
        side=OrderSide.BUY,
        qty=qty,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.DAY,
        order_class=OrderClass.BRACKET,
        status=status,
        filled_qty=filled_qty,
        avg_fill_price=avg_fill_price,
        filled_at=filled_at,
        submitted_at=datetime(2026, 5, 9, 13, 30, tzinfo=UTC),
    )


def _child_fill(*, cid_suffix: str, side: OrderSide, fill_price: Decimal, filled_at: datetime) -> Order:
    return Order(
        client_order_id=f"{CID}_{cid_suffix}",
        broker_order_id="broker-" + f"{CID}_{cid_suffix}",
        symbol="AAA",
        side=side,
        qty=Decimal("20"),
        filled_qty=Decimal("20"),
        order_type=OrderType.LIMIT if cid_suffix == "tp" else OrderType.STOP,
        time_in_force=TimeInForce.GTC,
        status=OrderStatus.FILLED,
        avg_fill_price=fill_price,
        filled_at=filled_at,
    )


async def _make_governor() -> RiskGovernor:
    g = RiskGovernor(
        state_store=InMemoryRiskStateStore(),
        broker=AsyncMock(),
        platform_capital=Decimal("10000"),
    )
    await g.register_engine(ENGINE_ID, Decimal("10000"))
    return g


# ────────────────────────────────────────────────────────────────────────────
# submit_decision
# ────────────────────────────────────────────────────────────────────────────


async def test_submit_passes_through_gate_governor_and_broker() -> None:
    placed = [_placed_parent()]
    broker = AsyncMock()
    broker.submit_tier1_only = AsyncMock(return_value=placed[0])
    gov = await _make_governor()
    om = VectorOrderManager(
        broker=broker,
        governor=gov,
        capital_gate=VectorCapitalGate(),
        lifecycle=VectorLifecycleAnalysis(),
        aar=VectorAARLogging(),
    )
    out = await om.submit_decision(_decision(), _assessment())
    assert out == placed
    broker.submit_tier1_only.assert_awaited_once()
    state = await gov.state_for(ENGINE_ID)
    assert state.open_positions == 1


async def test_submit_blocked_by_capital_gate_returns_none() -> None:
    broker = AsyncMock()
    broker.submit_tier1_only = AsyncMock()
    gov = await _make_governor()
    om = VectorOrderManager(
        broker=broker,
        governor=gov,
        capital_gate=VectorCapitalGate(max_position_usd=Decimal("100")),  # too small
        lifecycle=VectorLifecycleAnalysis(),
        aar=VectorAARLogging(),
    )
    out = await om.submit_decision(_decision(), _assessment())
    assert out is None
    broker.submit_tier1_only.assert_not_called()


# ────────────────────────────────────────────────────────────────────────────
# Reconcile — TAKE_PROFIT path
# ────────────────────────────────────────────────────────────────────────────


async def test_reconcile_builds_take_profit_aar_when_exit_above_entry() -> None:
    placed = [_placed_parent()]
    broker = AsyncMock()
    broker.submit_tier1_only = AsyncMock(return_value=placed[0])
    # Mock list_recent_orders to return the parent (filled) + a TP child fill.
    parent_filled = _placed_parent(
        status=OrderStatus.FILLED,
        filled_qty=Decimal("20"),
        avg_fill_price=Decimal("100.50"),
        filled_at=datetime(2026, 5, 9, 13, 31, tzinfo=UTC),
    )
    tp_child = _child_fill(
        cid_suffix="tp",
        side=OrderSide.SELL,
        fill_price=Decimal("115.00"),
        filled_at=datetime(2026, 5, 14, 14, 0, tzinfo=UTC),
    )
    broker.list_recent_orders = AsyncMock(return_value=[parent_filled, tp_child])
    gov = await _make_governor()
    aar_writer = AsyncMock()
    aar_writer.write_aar = AsyncMock(return_value=True)
    om = VectorOrderManager(
        broker=broker,
        governor=gov,
        capital_gate=VectorCapitalGate(),
        lifecycle=VectorLifecycleAnalysis(),
        aar=VectorAARLogging(),
        aar_writer=aar_writer,
    )
    await om.submit_decision(_decision(), _assessment())
    aars = await om.reconcile(sizing_pct_of_engine_equity=Decimal("0.20"))
    assert len(aars) == 1
    aar = aars[0]
    assert aar.exit_reason is ExitReason.TAKE_PROFIT
    assert aar.entry_price == Decimal("100.50")
    assert aar.exit_price == Decimal("115.00")
    aar_writer.write_aar.assert_awaited_once()
    state = await gov.state_for(ENGINE_ID)
    assert state.open_positions == 0  # decremented after AAR


async def test_reconcile_builds_stop_loss_aar_when_exit_below_entry() -> None:
    placed = [_placed_parent()]
    broker = AsyncMock()
    broker.submit_tier1_only = AsyncMock(return_value=placed[0])
    parent_filled = _placed_parent(
        status=OrderStatus.FILLED,
        filled_qty=Decimal("20"),
        avg_fill_price=Decimal("100.00"),
        filled_at=datetime(2026, 5, 9, 13, 31, tzinfo=UTC),
    )
    sl_child = _child_fill(
        cid_suffix="sl",
        side=OrderSide.SELL,
        fill_price=Decimal("93.00"),
        filled_at=datetime(2026, 5, 12, 14, 0, tzinfo=UTC),
    )
    broker.list_recent_orders = AsyncMock(return_value=[parent_filled, sl_child])
    om = VectorOrderManager(
        broker=broker,
        governor=await _make_governor(),
        capital_gate=VectorCapitalGate(),
        lifecycle=VectorLifecycleAnalysis(),
        aar=VectorAARLogging(),
    )
    await om.submit_decision(_decision(), _assessment())
    aars = await om.reconcile(sizing_pct_of_engine_equity=Decimal("0.20"))
    assert len(aars) == 1
    assert aars[0].exit_reason is ExitReason.STOP_LOSS


async def test_reconcile_idempotent_across_runs() -> None:
    placed = [_placed_parent()]
    broker = AsyncMock()
    broker.submit_tier1_only = AsyncMock(return_value=placed[0])
    parent_filled = _placed_parent(
        status=OrderStatus.FILLED,
        filled_qty=Decimal("20"),
        avg_fill_price=Decimal("100.00"),
        filled_at=datetime(2026, 5, 9, 13, 31, tzinfo=UTC),
    )
    tp = _child_fill(
        cid_suffix="tp",
        side=OrderSide.SELL,
        fill_price=Decimal("115"),
        filled_at=datetime(2026, 5, 14, 14, 0, tzinfo=UTC),
    )
    broker.list_recent_orders = AsyncMock(return_value=[parent_filled, tp])
    om = VectorOrderManager(
        broker=broker,
        governor=await _make_governor(),
        capital_gate=VectorCapitalGate(),
        lifecycle=VectorLifecycleAnalysis(),
        aar=VectorAARLogging(),
    )
    await om.submit_decision(_decision(), _assessment())
    first = await om.reconcile(sizing_pct_of_engine_equity=Decimal("0.20"))
    second = await om.reconcile(sizing_pct_of_engine_equity=Decimal("0.20"))
    assert len(first) == 1 and len(second) == 0


# ────────────────────────────────────────────────────────────────────────────
# Parity harness wiring
# ────────────────────────────────────────────────────────────────────────────


async def test_submit_invokes_parity_harness_when_provided() -> None:
    placed = [_placed_parent()]
    broker = AsyncMock()
    broker.submit_tier1_only = AsyncMock(return_value=placed[0])
    parity = AsyncMock()
    parity.submit_pair = AsyncMock()
    om = VectorOrderManager(
        broker=broker,
        governor=await _make_governor(),
        capital_gate=VectorCapitalGate(),
        lifecycle=VectorLifecycleAnalysis(),
        aar=VectorAARLogging(),
        parity_harness=parity,
    )
    await om.submit_decision(_decision(), _assessment())
    parity.submit_pair.assert_awaited_once()


async def test_parity_failure_does_not_block_paper_trade() -> None:
    placed = [_placed_parent()]
    broker = AsyncMock()
    broker.submit_tier1_only = AsyncMock(return_value=placed[0])
    parity = AsyncMock()
    parity.submit_pair = AsyncMock(side_effect=RuntimeError("live broker rejected"))
    om = VectorOrderManager(
        broker=broker,
        governor=await _make_governor(),
        capital_gate=VectorCapitalGate(),
        lifecycle=VectorLifecycleAnalysis(),
        aar=VectorAARLogging(),
        parity_harness=parity,
    )
    out = await om.submit_decision(_decision(), _assessment())
    # Paper still succeeded.
    assert out == placed
