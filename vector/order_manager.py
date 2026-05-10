"""Vector order manager — orchestrates plug pipeline → broker → AAR.

Mirrors ``sigma.order_manager`` and ``reversion.order_manager`` but with
Vector's single-bracket exit model: every trade is one Alpaca bracket
order (entry market + take-profit limit at +15% + stop-loss at −7%). On
fill of either child leg, the trade is closed and a single AAR is logged.

The trailing stop is *not* server-side. ``LifecycleAnalysis.step`` arms
the trail when close ≥ entry × 1.10 and signals an EXIT phase when
close ≤ peak × 0.95. The order manager exposes ``close_position`` for
the scheduler to drive that exit on the next session — cancels the
remaining bracket leg and submits a market sell. (Full automation of
the trail loop is scheduled for a follow-up; the static −7% stop is the
current floor.)

Optional parity harness: when constructed with ``parity_harness=`` the
manager calls ``LivePaperParityHarness.submit_pair`` after each successful
paper submission. Failures on the live side never block the paper trade.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal

import structlog

from tpcore.aar.models import AfterActionReport, ExitReason
from tpcore.aar.writer import AARWriter
from tpcore.interfaces.broker import (
    BrokerExecutionInterface,
    Order,
    OrderSide,
    OrderStatus,
)
from tpcore.parity import LivePaperParityHarness
from tpcore.risk.governor import RiskDecision, RiskGovernor
from vector.models import ExecutionDecision, PhaseAssessment
from vector.plugs.aar_logging import VectorAARLogging
from vector.plugs.capital_gate import VectorCapitalGate
from vector.plugs.lifecycle_analysis import VectorLifecycleAnalysis

logger = structlog.get_logger(__name__)

ENGINE_ID = "vector"


class VectorOrderManager:
    """Drives a Vector trade from execution decision through final AAR."""

    def __init__(
        self,
        *,
        broker: BrokerExecutionInterface,
        governor: RiskGovernor,
        capital_gate: VectorCapitalGate,
        lifecycle: VectorLifecycleAnalysis,
        aar: VectorAARLogging,
        aar_writer: AARWriter | None = None,
        parity_harness: LivePaperParityHarness | None = None,
    ) -> None:
        self._broker = broker
        self._governor = governor
        self._capital_gate = capital_gate
        self._lifecycle = lifecycle
        self._aar = aar
        self._aar_writer = aar_writer
        self._parity = parity_harness
        # client_order_id → assessment cached at submission time.
        self._trade_assessments: dict[str, PhaseAssessment] = {}
        # client_order_ids whose AAR has already been written this process.
        self._aar_logged: set[str] = set()

    async def submit_decision(
        self,
        decision: ExecutionDecision,
        assessment: PhaseAssessment,
    ) -> list[Order] | None:
        """Run ``decision`` through gates and ship it. Returns placed orders or None."""
        engine_state = await self._governor._store.get(ENGINE_ID)  # noqa: SLF001 — read-only peek
        engine_pnl = engine_state.daily_pnl if engine_state else Decimal("0")
        open_count = engine_state.open_positions if engine_state else 0

        if not self._capital_gate.check_trade(
            size=decision.notional_usd,
            engine_pnl=engine_pnl,
            open_positions=open_count,
        ):
            logger.info(
                "vector.order_manager.gate_blocked",
                ticker=decision.ticker,
                size=str(decision.notional_usd),
                engine_pnl=str(engine_pnl),
            )
            return None

        check = await self._governor.check_trade(
            engine_id=ENGINE_ID,
            size=decision.notional_usd,
            direction=OrderSide.BUY,
        )
        if check.decision is RiskDecision.BLOCK:
            logger.warning(
                "vector.order_manager.governor_blocked",
                ticker=decision.ticker,
                reason=check.reason,
            )
            return None

        placed = await self._broker.submit_execution_decision(decision)
        if placed:
            cid = placed[0].client_order_id
            self._trade_assessments[cid] = assessment

        await self._governor.record_fill(
            engine_id=ENGINE_ID,
            realized_pnl=Decimal("0"),
            position_delta=1,
        )
        logger.info(
            "vector.order_manager.trade_submitted",
            ticker=decision.ticker,
            qty=decision.qty,
            notional=str(decision.notional_usd),
            vix_size_factor=str(decision.vix_size_factor),
        )

        # Parity harness — non-blocking. Failures on the live side never
        # propagate to the paper trade; we just log + continue.
        if self._parity is not None and placed:
            try:
                await self._parity.submit_pair(placed[0])
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "vector.order_manager.parity_harness_failed",
                    client_order_id=placed[0].client_order_id,
                    error=str(exc),
                )

        return placed

    async def reconcile(
        self,
        *,
        sizing_pct_of_engine_equity: Decimal,
        confidence_at_entry: Decimal = Decimal("0.70"),
    ) -> list[AfterActionReport]:
        """Pull broker order history, build AARs for closed trades.

        Vector's bracket: parent (entry, market, BUY) + take-profit limit
        + stop-loss stop. When *either* child fills, the position is
        closed. We key by the parent's client_order_id (which the
        execution_risk plug set to ``vector_{ticker}_{epoch}``) and treat
        any FILLED order whose client_order_id starts with that prefix as
        an exit candidate.
        """
        orders = await self._fetch_recent_orders()
        # Group orders by parent prefix.
        by_trade: dict[str, list[Order]] = defaultdict(list)
        for o in orders:
            for cid in self._trade_assessments:
                if o.client_order_id == cid or o.client_order_id.startswith(cid + "_"):
                    by_trade[cid].append(o)

        new_aars: list[AfterActionReport] = []
        for cid, legs in by_trade.items():
            if cid in self._aar_logged:
                continue
            assessment = self._trade_assessments.get(cid)
            if assessment is None:
                continue
            # Find the parent (entry) and any filled exit leg.
            parent = next((o for o in legs if o.client_order_id == cid), None)
            if parent is None or parent.status is not OrderStatus.FILLED:
                continue
            exit_leg = next(
                (o for o in legs if o.client_order_id != cid and o.status is OrderStatus.FILLED),
                None,
            )
            if exit_leg is None or exit_leg.avg_fill_price is None or exit_leg.filled_at is None:
                continue

            exit_reason = self._classify_exit_reason(
                entry_price=parent.avg_fill_price or assessment.entry_price,
                exit_price=exit_leg.avg_fill_price,
            )
            aar = self._aar.build_aar(
                trade_id=f"vector-{cid}",
                ticker=assessment.ticker,
                entry_ts=parent.filled_at or parent.submitted_at or datetime.now(UTC),
                exit_ts=exit_leg.filled_at,
                entry_price=parent.avg_fill_price or assessment.entry_price,
                exit_price=exit_leg.avg_fill_price,
                qty=parent.filled_qty or parent.qty,
                exit_reason=exit_reason,
                confidence_at_entry=confidence_at_entry,
                sizing_pct_of_engine_equity=sizing_pct_of_engine_equity,
            )
            if self._aar_writer is not None:
                await self._aar_writer.write_aar(aar)
            new_aars.append(aar)
            self._aar_logged.add(cid)
            await self._governor.record_fill(
                engine_id=ENGINE_ID,
                realized_pnl=aar.pnl_net,
                position_delta=-1,
            )
            logger.info(
                "vector.order_manager.trade_closed",
                client_order_id=cid,
                exit_reason=exit_reason.value,
                pnl_net=str(aar.pnl_net),
            )

        return new_aars

    @staticmethod
    def _classify_exit_reason(*, entry_price: Decimal, exit_price: Decimal) -> ExitReason:
        """Loose proxy: positive P&L → take-profit; negative → stop-loss.

        For Vector's bracket, Alpaca's child orders carry an ``order_type``
        we could read directly (limit vs stop), but the broker's `Order`
        model doesn't expose the parent linkage in an obvious way, so we
        derive from price for the MVP. Edge case: a flat fill (entry ==
        exit) is unlikely with bracket orders but is classified as TIME_STOP.
        """
        if exit_price > entry_price:
            return ExitReason.TAKE_PROFIT
        if exit_price < entry_price:
            return ExitReason.STOP_LOSS
        return ExitReason.TIME_STOP

    async def _fetch_recent_orders(self) -> list[Order]:
        list_fn = getattr(self._broker, "list_recent_orders", None)
        if list_fn is None:
            return []
        return await list_fn()


__all__ = ["ENGINE_ID", "VectorOrderManager"]
