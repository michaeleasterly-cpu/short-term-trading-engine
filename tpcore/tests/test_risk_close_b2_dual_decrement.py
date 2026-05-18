"""#251 Phase B2.1 — the REAL dual-decrement fix.

The trade-monitor stream (B1) already funnels its close through
``record_close(engine, trade_id=open_orders.trade_id, …)``. But the
per-trade order-manager reconcile loop still decremented the slot via an
unkeyed ``record_fill(position_delta=-1)`` — so the SAME OCO close was
counted TWICE (once by reconcile, once by the stream).

B2.1 routes the order-manager close through the SAME
``record_close`` arbiter with the SAME bare ``open_orders.trade_id``
key, so the ``risk_close_ledger`` ``(engine, trade_id)`` PK arbitrates
the real close to AT MOST one decrement.

The make-or-break correctness point: the key the order manager passes
MUST be byte-identical to what ``_persist_tier1_to_open_orders`` wrote
to ``platform.open_orders.trade_id`` (and therefore what the stream
passes as ``row.trade_id``). An ``engine-`` prefix would mean the two
keys never collide and the dual-decrement would survive silently — this
file proves that does not happen.

No DB / no ``data/`` — InMemoryRiskStateStore + AsyncMock broker only.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

# ── Reversion fixtures ──────────────────────────────────────────────────────
from reversion.models import Direction as RevDirection
from reversion.models import ExecutionDecision as RevDecision
from reversion.models import Phase as RevPhase
from reversion.models import PhaseAssessment as RevAssessment
from reversion.order_manager import ENGINE_ID as REV_ENGINE
from reversion.order_manager import ReversionOrderManager
from reversion.plugs.aar_logging import ReversionAARLogging
from reversion.plugs.capital_gate import ReversionCapitalGate
from reversion.plugs.lifecycle_analysis import ReversionLifecycleAnalysis
from tpcore.interfaces.broker import (
    Order,
    OrderClass,
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
)
from tpcore.order_ids import build_cid, parse_cid
from tpcore.risk.governor import InMemoryRiskStateStore, RiskGovernor

# ── Vector fixtures ─────────────────────────────────────────────────────────
from vector.models import ExecutionDecision as VecDecision
from vector.models import Phase as VecPhase
from vector.models import PhaseAssessment as VecAssessment
from vector.order_manager import ENGINE_ID as VEC_ENGINE
from vector.order_manager import VectorOrderManager
from vector.plugs.aar_logging import VectorAARLogging
from vector.plugs.capital_gate import VectorCapitalGate
from vector.plugs.lifecycle_analysis import VectorLifecycleAnalysis

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


_CONSTRUCTED = datetime(2026, 5, 9, 13, 30, tzinfo=UTC)
# Canonical reversion tier1 cid: re_RRR_<ts>_tier1 ; tier2: ..._tier2
REV_T1_CID = build_cid("reversion", "RRR", constructed_at=_CONSTRUCTED, tier="tier1")
REV_T2_CID = build_cid("reversion", "RRR", constructed_at=_CONSTRUCTED, tier="tier2")
# The bare trade_key _persist_tier1_to_open_orders writes to open_orders.trade_id:
REV_BARE_KEY = parse_cid(REV_T1_CID).trade_key or REV_T1_CID  # "RRR_<ts>"

VEC_CID = "vector_VVV_1700000000"  # vector parent cid == open_orders.trade_id


# ────────────────────────────────────────────────────────────────────────────
# Builders
# ────────────────────────────────────────────────────────────────────────────
async def _make_governor(engine: str) -> RiskGovernor:
    g = RiskGovernor(
        state_store=InMemoryRiskStateStore(),
        broker=AsyncMock(),
        platform_capital=Decimal("10000"),
    )
    await g.register_engine(engine, Decimal("10000"))
    return g


def _rev_assessment() -> RevAssessment:
    return RevAssessment(
        ticker="RRR",
        as_of=date(2026, 5, 9),
        direction=RevDirection.LONG,
        phase=RevPhase.ACTIVE,
        entry_price=Decimal("100.00"),
        stop_price=Decimal("92.00"),
        target_20ma=Decimal("105.00"),
        target_50ma=Decimal("108.00"),
    )


def _rev_decision() -> RevDecision:
    return RevDecision(
        ticker="RRR",
        direction=RevDirection.LONG,
        qty=20,
        tier1_qty=15,
        tier2_qty=5,
        notional_usd=Decimal("2000.00"),
        risk_amount_usd=Decimal("160.00"),
        order_payloads=[
            {
                "client_order_id": REV_T1_CID,
                "symbol": "RRR",
                "qty": 15,
                "side": "buy",
                "type": "limit",
                "time_in_force": "day",
                "order_class": "bracket",
                "take_profit": {"limit_price": "105.00"},
                "stop_loss": {"stop_price": "92.00"},
            },
            {
                "client_order_id": REV_T2_CID,
                "symbol": "RRR",
                "qty": 5,
                "side": "sell",
                "type": "limit",
                "time_in_force": "gtc",
            },
        ],
        constructed_at=_CONSTRUCTED,
    )


def _rev_order(cid: str, *, status: OrderStatus, **kw: object) -> Order:
    return Order(
        client_order_id=cid,
        broker_order_id="b-" + cid,
        symbol="RRR",
        side=OrderSide.BUY if cid.endswith("tier1") else OrderSide.SELL,
        qty=Decimal(str(kw.get("qty", "15"))),
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.DAY,
        order_class=OrderClass.BRACKET,
        status=status,
        filled_qty=Decimal(str(kw.get("filled_qty", "0"))),
        avg_fill_price=kw.get("avg_fill_price"),  # type: ignore[arg-type]
        filled_at=kw.get("filled_at"),  # type: ignore[arg-type]
        submitted_at=_CONSTRUCTED,
    )


async def _build_reversion_closed_om() -> tuple[ReversionOrderManager, RiskGovernor]:
    """A reversion OM whose next reconcile() closes one OCO trade."""
    gov = await _make_governor(REV_ENGINE)
    broker = AsyncMock()
    t1_placed = _rev_order(REV_T1_CID, status=OrderStatus.NEW)
    broker.submit_tier1_only = AsyncMock(return_value=t1_placed)
    t1_filled = _rev_order(
        REV_T1_CID,
        status=OrderStatus.FILLED,
        filled_qty="15",
        avg_fill_price=Decimal("100.00"),
        filled_at=datetime(2026, 5, 9, 13, 31, tzinfo=UTC),
    )
    t2_filled = _rev_order(
        REV_T2_CID,
        status=OrderStatus.FILLED,
        qty="5",
        filled_qty="5",
        avg_fill_price=Decimal("105.00"),
        filled_at=datetime(2026, 5, 14, 14, 0, tzinfo=UTC),
    )
    broker.list_recent_orders = AsyncMock(return_value=[t1_filled, t2_filled])
    aar_writer = AsyncMock()
    aar_writer.write_aar = AsyncMock(return_value=True)
    om = ReversionOrderManager(
        broker=broker,
        governor=gov,
        capital_gate=ReversionCapitalGate(),
        lifecycle=ReversionLifecycleAnalysis(),
        aar=ReversionAARLogging(),
        aar_writer=aar_writer,
    )
    await om.submit_decision(_rev_decision(), _rev_assessment())
    return om, gov


def _vec_assessment() -> VecAssessment:
    return VecAssessment(
        ticker="VVV",
        as_of=date(2026, 5, 9),
        phase=VecPhase.ENTRY,
        entry_price=Decimal("100.00"),
        stop_price=Decimal("93.00"),
        profit_target_price=Decimal("115.00"),
    )


def _vec_decision() -> VecDecision:
    return VecDecision(
        ticker="VVV",
        qty=20,
        notional_usd=Decimal("2000.00"),
        risk_amount_usd=Decimal("140.00"),
        vix_size_factor=Decimal("1.0"),
        order_payloads=[
            {
                "client_order_id": VEC_CID,
                "symbol": "VVV",
                "qty": 20,
                "side": "buy",
                "type": "market",
                "time_in_force": "day",
                "order_class": "bracket",
                "take_profit": {"limit_price": "115.00"},
                "stop_loss": {"stop_price": "93.00"},
            }
        ],
        constructed_at=_CONSTRUCTED,
    )


def _vec_parent(*, status: OrderStatus, **kw: object) -> Order:
    return Order(
        client_order_id=VEC_CID,
        broker_order_id="b-" + VEC_CID,
        symbol="VVV",
        side=OrderSide.BUY,
        qty=Decimal("20"),
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.DAY,
        order_class=OrderClass.BRACKET,
        status=status,
        filled_qty=Decimal(str(kw.get("filled_qty", "0"))),
        avg_fill_price=kw.get("avg_fill_price"),  # type: ignore[arg-type]
        filled_at=kw.get("filled_at"),  # type: ignore[arg-type]
        submitted_at=_CONSTRUCTED,
    )


def _vec_exit() -> Order:
    return Order(
        client_order_id=f"{VEC_CID}_tp",
        broker_order_id=f"b-{VEC_CID}_tp",
        symbol="VVV",
        side=OrderSide.SELL,
        qty=Decimal("20"),
        filled_qty=Decimal("20"),
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        status=OrderStatus.FILLED,
        avg_fill_price=Decimal("115.00"),
        filled_at=datetime(2026, 5, 14, 14, 0, tzinfo=UTC),
    )


async def _build_vector_closed_om() -> tuple[VectorOrderManager, RiskGovernor]:
    gov = await _make_governor(VEC_ENGINE)
    broker = AsyncMock()
    broker.submit_tier1_only = AsyncMock(
        return_value=_vec_parent(status=OrderStatus.NEW)
    )
    parent_filled = _vec_parent(
        status=OrderStatus.FILLED,
        filled_qty="20",
        avg_fill_price=Decimal("100.00"),
        filled_at=datetime(2026, 5, 9, 13, 31, tzinfo=UTC),
    )
    broker.list_recent_orders = AsyncMock(return_value=[parent_filled, _vec_exit()])
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
    await om.submit_decision(_vec_decision(), _vec_assessment())
    return om, gov


# ════════════════════════════════════════════════════════════════════════════
# 1. BYTE-IDENTITY (make-or-break)
#    The trade_id the OM passes to record_close MUST be byte-identical to the
#    bare key _persist_tier1_to_open_orders writes to open_orders.trade_id.
# ════════════════════════════════════════════════════════════════════════════
async def test_reversion_record_close_trade_id_is_byte_identical_to_open_orders() -> None:
    om, gov = await _build_reversion_closed_om()
    captured: list[str | None] = []
    real = gov.record_close

    async def _spy(engine_id: str, trade_id: str | None, *a: object, **kw: object) -> bool:
        captured.append(trade_id)
        return await real(engine_id, trade_id, *a, **kw)  # type: ignore[arg-type]

    gov.record_close = _spy  # type: ignore[method-assign]
    await om.reconcile(sizing_pct_of_engine_equity=Decimal("0.20"))

    assert len(captured) == 1, f"expected exactly one close, got {captured}"
    # What _persist_tier1_to_open_orders writes to open_orders.trade_id is
    # `parse_cid(tier1.client_order_id).trade_key or tier1.client_order_id`
    # (reversion/order_manager.py:146). The stream passes that as row.trade_id.
    expected = parse_cid(REV_T1_CID).trade_key or REV_T1_CID
    assert captured[0] == expected == REV_BARE_KEY
    # MUST be the bare key — NO engine- prefix (the exact trap).
    assert captured[0] is not None
    assert not captured[0].startswith("reversion-")
    assert not captured[0].startswith(f"{REV_ENGINE}-")


async def test_vector_record_close_trade_id_is_byte_identical_to_open_orders() -> None:
    om, gov = await _build_vector_closed_om()
    captured: list[str | None] = []
    real = gov.record_close

    async def _spy(engine_id: str, trade_id: str | None, *a: object, **kw: object) -> bool:
        captured.append(trade_id)
        return await real(engine_id, trade_id, *a, **kw)  # type: ignore[arg-type]

    gov.record_close = _spy  # type: ignore[method-assign]
    await om.reconcile(sizing_pct_of_engine_equity=Decimal("0.20"))

    assert len(captured) == 1, f"expected exactly one close, got {captured}"
    # _persist_tier1_to_open_orders writes trade_key=cid where
    # cid = tier1_order.client_order_id (vector/order_manager.py:143,147).
    assert captured[0] == VEC_CID
    assert captured[0] is not None
    assert not captured[0].startswith("vector-")
    assert not captured[0].startswith(f"{VEC_ENGINE}-")


# ════════════════════════════════════════════════════════════════════════════
# 2. THE REAL DUAL-DECREMENT — reconcile + stream for the SAME OCO close.
#    Net open_positions decrement must be EXACTLY 1 (pre-B2.1 it was 2).
# ════════════════════════════════════════════════════════════════════════════
async def _stream_close(gov: RiskGovernor, engine: str, trade_id: str, pnl: Decimal) -> None:
    """Mirror tpcore/trade_monitor.py:624-629 — store-level record_close
    with the SAME bare open_orders.trade_id (row.trade_id)."""
    await gov._store.record_close(engine, trade_id, pnl)  # noqa: SLF001 - mirrors store-level B1 stream call


@pytest.mark.parametrize("order", ["reconcile_then_stream", "stream_then_reconcile"])
async def test_reversion_reconcile_and_stream_net_decrement_is_one(order: str) -> None:
    om, gov = await _build_reversion_closed_om()
    assert (await gov.state_for(REV_ENGINE)).open_positions == 1  # the +1 open

    stream_pnl = Decimal("75.00")
    if order == "reconcile_then_stream":
        # Reconcile wins the ledger insert → the OM's aar.pnl_net applies.
        aars = await om.reconcile(sizing_pct_of_engine_equity=Decimal("0.20"))
        await _stream_close(gov, REV_ENGINE, REV_BARE_KEY, stream_pnl)
        expected_pnl = next(a for a in aars if a.pnl_net != Decimal("0")).pnl_net
    else:
        # Stream wins → its pnl applies; the OM reconcile drops the dup.
        await _stream_close(gov, REV_ENGINE, REV_BARE_KEY, stream_pnl)
        await om.reconcile(sizing_pct_of_engine_equity=Decimal("0.20"))
        expected_pnl = stream_pnl

    state = await gov.state_for(REV_ENGINE)
    # +1 open then ONE arbitrated close → exactly 0. The race loser drops
    # its duplicate decrement AND its duplicate pnl (idempotent single
    # application = correct).
    assert state.open_positions == 0
    assert state.daily_pnl == expected_pnl  # applied exactly ONCE, not 2x


async def test_reversion_concurrent_reconcile_and_stream_net_decrement_is_one() -> None:
    om, gov = await _build_reversion_closed_om()
    stream_pnl = Decimal("75.00")
    aars, _ = await asyncio.gather(
        om.reconcile(sizing_pct_of_engine_equity=Decimal("0.20")),
        _stream_close(gov, REV_ENGINE, REV_BARE_KEY, stream_pnl),
    )
    rev_pnl = next(a for a in aars if a.pnl_net != Decimal("0")).pnl_net
    state = await gov.state_for(REV_ENGINE)
    # Whichever path won the ledger insert applied its pnl EXACTLY once;
    # the loser dropped the duplicate. Either single value is correct —
    # what must NEVER happen is the doubled sum (the pre-B2.1 bug).
    assert state.open_positions == 0
    assert state.daily_pnl in (rev_pnl, stream_pnl)
    assert state.daily_pnl != rev_pnl + stream_pnl  # never double-applied


@pytest.mark.parametrize("order", ["reconcile_then_stream", "stream_then_reconcile"])
async def test_vector_reconcile_and_stream_net_decrement_is_one(order: str) -> None:
    om, gov = await _build_vector_closed_om()
    assert (await gov.state_for(VEC_ENGINE)).open_positions == 1
    stream_pnl = Decimal("300.00")

    if order == "reconcile_then_stream":
        # Reconcile wins the ledger insert → the OM's aar.pnl_net applies;
        # the stream is the race loser and correctly drops its duplicate.
        aars = await om.reconcile(sizing_pct_of_engine_equity=Decimal("0.20"))
        await _stream_close(gov, VEC_ENGINE, VEC_CID, stream_pnl)
        expected_pnl = aars[0].pnl_net
    else:
        # Stream wins → its pnl applies; the OM reconcile drops the dup.
        await _stream_close(gov, VEC_ENGINE, VEC_CID, stream_pnl)
        await om.reconcile(sizing_pct_of_engine_equity=Decimal("0.20"))
        expected_pnl = stream_pnl

    state = await gov.state_for(VEC_ENGINE)
    assert state.open_positions == 0  # slot freed exactly once
    assert state.daily_pnl == expected_pnl  # PnL applied exactly ONCE


async def test_pre_b2_1_unkeyed_record_fill_would_double_decrement() -> None:
    """Bite test: the OLD code shape (unkeyed record_fill(-1) in reconcile +
    record_close(-1) in the stream, NOT sharing a ledger key) double-counts
    the SAME real close. This proves the test catches the actual bug."""
    gov = await _make_governor(REV_ENGINE)
    await gov.record_fill(engine_id=REV_ENGINE, realized_pnl=Decimal("0"), position_delta=1)
    assert (await gov.state_for(REV_ENGINE)).open_positions == 1

    # OLD reconcile close: unkeyed record_fill(-1) (the pre-B2.1 line).
    await gov.record_fill(
        engine_id=REV_ENGINE, realized_pnl=Decimal("75.00"), position_delta=-1
    )
    # Stream close (B1) for the SAME real trade — a DIFFERENT decrement path.
    await gov._store.record_close(  # noqa: SLF001 - exercising the pre-fix race
        REV_ENGINE, REV_BARE_KEY, Decimal("75.00")
    )
    state = await gov.state_for(REV_ENGINE)
    # TWO independent decrements for ONE real close → slot wrongly emptied
    # AND pnl applied TWICE. open_positions clamps at 0 but pnl reveals
    # the double-count the keyed arbiter prevents.
    assert state.daily_pnl == Decimal("150.00")  # 75 applied TWICE — the bug


# ════════════════════════════════════════════════════════════════════════════
# 3. NEVER-FAIL-OPEN inherited — a null / errored key → skip, NO decrement,
#    so the slot stays occupied (over-count safe; never fail open).
# ════════════════════════════════════════════════════════════════════════════
async def test_null_trade_id_skips_decrement_no_fail_open() -> None:
    gov = await _make_governor(REV_ENGINE)
    await gov.record_fill(engine_id=REV_ENGINE, realized_pnl=Decimal("0"), position_delta=1)
    applied = await gov.record_close(REV_ENGINE, None, Decimal("10"))
    assert applied is False
    state = await gov.state_for(REV_ENGINE)
    assert state.open_positions == 1  # slot NOT freed — over-count, safe
    assert state.daily_pnl == Decimal("0")


async def test_duplicate_trade_id_decrements_at_most_once() -> None:
    gov = await _make_governor(REV_ENGINE)
    await gov.record_fill(engine_id=REV_ENGINE, realized_pnl=Decimal("0"), position_delta=1)
    first = await gov.record_close(REV_ENGINE, REV_BARE_KEY, Decimal("40"))
    second = await gov.record_close(REV_ENGINE, REV_BARE_KEY, Decimal("40"))
    assert first is True and second is False
    state = await gov.state_for(REV_ENGINE)
    assert state.open_positions == 0
    assert state.daily_pnl == Decimal("40")  # applied exactly once
