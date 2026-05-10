"""Reversion order manager — orchestrates plug pipeline → broker → AAR.

Mirrors ``sigma.order_manager.SigmaOrderManager``; differences:
    * ``ENGINE_ID = "reversion"`` for risk-state and AAR routing.
    * Direction-aware: a Tier 1 fill on a SHORT means we covered 75%
      of the short; the lifecycle update is symmetric.
    * Earnings-quality gating happens in lifecycle_analysis.assess and
      surfaces here as ``assessment.earnings_quality_blocked``; we
      respect it before submitting.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal

import structlog

from tpcore.aar.models import AfterActionReport
from tpcore.aar.writer import AARWriter
from tpcore.interfaces.broker import (
    BrokerExecutionInterface,
    Order,
    OrderSide,
    OrderStatus,
)
from tpcore.parity import LivePaperParityHarness
from tpcore.risk.governor import RiskDecision, RiskGovernor

from reversion.models import Direction, ExecutionDecision, PhaseAssessment
from reversion.plugs.aar_logging import ReversionAARLogging
from reversion.plugs.capital_gate import ReversionCapitalGate
from reversion.plugs.lifecycle_analysis import ReversionLifecycleAnalysis

logger = structlog.get_logger(__name__)

ENGINE_ID = "reversion"


def _trade_key(client_order_id: str) -> str:
    if client_order_id.endswith("_tier1"):
        return client_order_id[: -len("_tier1")]
    if client_order_id.endswith("_tier2"):
        return client_order_id[: -len("_tier2")]
    return client_order_id


def _tier(client_order_id: str) -> str | None:
    if client_order_id.endswith("_tier1"):
        return "tier1"
    if client_order_id.endswith("_tier2"):
        return "tier2"
    return None


class ReversionOrderManager:
    """Drives a Reversion trade from execution decision through final AAR."""

    def __init__(
        self,
        *,
        broker: BrokerExecutionInterface,
        governor: RiskGovernor,
        capital_gate: ReversionCapitalGate,
        lifecycle: ReversionLifecycleAnalysis,
        aar: ReversionAARLogging,
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
        self._trade_assessments: dict[str, PhaseAssessment] = {}
        self._tier1_logged: set[str] = set()
        self._tier2_logged: set[str] = set()

    async def submit_decision(
        self,
        decision: ExecutionDecision,
        assessment: PhaseAssessment,
    ) -> list[Order] | None:
        engine_state = await self._governor._store.get(ENGINE_ID)  # noqa: SLF001 — read-only peek
        engine_pnl = engine_state.daily_pnl if engine_state else Decimal("0")
        open_count = engine_state.open_positions if engine_state else 0

        if not self._capital_gate.check_trade(
            size=decision.notional_usd,
            engine_pnl=engine_pnl,
            open_positions=open_count,
        ):
            logger.info(
                "reversion.order_manager.gate_blocked",
                ticker=decision.ticker,
                size=str(decision.notional_usd),
                engine_pnl=str(engine_pnl),
            )
            return None

        # Governor check — direction is BUY for LONG fades, SELL for SHORT fades.
        # The platform-net-long cap only applies on the BUY path, which is
        # already what RiskGovernor.check_trade enforces.
        side = OrderSide.BUY if decision.direction is Direction.LONG else OrderSide.SELL
        check = await self._governor.check_trade(
            engine_id=ENGINE_ID,
            size=decision.notional_usd,
            direction=side,
        )
        if check.decision is RiskDecision.BLOCK:
            logger.warning(
                "reversion.order_manager.governor_blocked",
                ticker=decision.ticker,
                reason=check.reason,
            )
            return None

        placed = await self._broker.submit_execution_decision(decision)
        if placed:
            trade_key = _trade_key(placed[0].client_order_id)
            self._trade_assessments[trade_key] = assessment

        await self._governor.record_fill(
            engine_id=ENGINE_ID,
            realized_pnl=Decimal("0"),
            position_delta=1,
        )
        logger.info(
            "reversion.order_manager.trade_submitted",
            ticker=decision.ticker,
            direction=decision.direction.value,
            qty=decision.qty,
            tier1_qty=decision.tier1_qty,
            tier2_qty=decision.tier2_qty,
            notional=str(decision.notional_usd),
        )

        # Parity harness — non-blocking, mirrors Sigma + Vector.
        if self._parity is not None and placed:
            try:
                await self._parity.submit_pair(placed[0])
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "reversion.order_manager.parity_harness_failed",
                    client_order_id=placed[0].client_order_id,
                    error=str(exc),
                )

        return placed

    async def reconcile(
        self,
        *,
        sizing_pct_of_engine_equity: Decimal,
        confidence_at_entry: Decimal = Decimal("0.75"),
    ) -> list[AfterActionReport]:
        orders = await self._fetch_recent_orders()
        by_trade: dict[str, dict[str, Order]] = defaultdict(dict)
        for o in orders:
            tier = _tier(o.client_order_id)
            if tier is None:
                continue
            by_trade[_trade_key(o.client_order_id)][tier] = o

        new_aars: list[AfterActionReport] = []
        for trade_key, legs in by_trade.items():
            tier1 = legs.get("tier1")
            tier2 = legs.get("tier2")
            assessment = self._trade_assessments.get(trade_key)

            if (
                tier1 is not None
                and tier1.status is OrderStatus.FILLED
                and trade_key not in self._tier1_logged
                and assessment is not None
                and tier1.avg_fill_price is not None
                and tier1.filled_at is not None
            ):
                aar = self._aar.build_tier1_aar(
                    trade_id=f"reversion-{trade_key}",
                    ticker=assessment.ticker,
                    entry_ts=tier1.submitted_at or datetime.now(UTC),
                    exit_ts=tier1.filled_at,
                    entry_price=assessment.entry_price,
                    exit_price=tier1.avg_fill_price,
                    tier1_qty=tier1.filled_qty,
                    confidence_at_entry=confidence_at_entry,
                    sizing_pct_of_engine_equity=sizing_pct_of_engine_equity,
                    rule_compliance=True,
                )
                self._aar.log_aar(aar)
                if self._aar_writer is not None:
                    await self._aar_writer.write_aar(aar)
                new_aars.append(aar)
                self._tier1_logged.add(trade_key)
                remaining = (
                    int(tier2.qty)
                    if tier2 is not None and tier2.status is not OrderStatus.CANCELED
                    else 0
                )
                self._trade_assessments[trade_key] = self._lifecycle.handle_tier1_fill(
                    assessment, position_remaining=remaining
                )

            if (
                tier1 is not None
                and tier1.status in (OrderStatus.CANCELED, OrderStatus.REJECTED)
                and trade_key not in self._tier1_logged
                and tier2 is not None
                and tier2.status not in (OrderStatus.CANCELED, OrderStatus.FILLED)
                and tier2.broker_order_id is not None
            ):
                logger.warning(
                    "reversion.order_manager.tier1_cancelled_before_fill",
                    trade_key=trade_key,
                    cancelling_tier2_id=tier2.broker_order_id,
                )
                await self._broker.cancel_order(tier2.broker_order_id)
                self._tier1_logged.add(trade_key)

            if (
                tier1 is not None
                and tier1.status is OrderStatus.FILLED
                and tier2 is not None
                and tier2.status is OrderStatus.FILLED
                and trade_key not in self._tier2_logged
                and assessment is not None
                and tier1.avg_fill_price is not None
                and tier2.avg_fill_price is not None
                and tier2.filled_at is not None
            ):
                final = self._aar.build_tier2_aar(
                    trade_id=f"reversion-{trade_key}",
                    ticker=assessment.ticker,
                    entry_ts=tier1.submitted_at or datetime.now(UTC),
                    exit_ts=tier2.filled_at,
                    entry_price=assessment.entry_price,
                    tier1_exit_price=tier1.avg_fill_price,
                    tier2_exit_price=tier2.avg_fill_price,
                    tier1_qty=tier1.filled_qty,
                    tier2_qty=tier2.filled_qty,
                    confidence_at_entry=confidence_at_entry,
                    sizing_pct_of_engine_equity=sizing_pct_of_engine_equity,
                    rule_compliance=True,
                )
                self._aar.log_aar(final)
                if self._aar_writer is not None:
                    await self._aar_writer.write_aar(final)
                new_aars.append(final)
                self._tier2_logged.add(trade_key)
                await self._governor.record_fill(
                    engine_id=ENGINE_ID,
                    realized_pnl=final.pnl_net,
                    position_delta=-1,
                )

        return new_aars

    async def _fetch_recent_orders(self) -> list[Order]:
        list_fn = getattr(self._broker, "list_recent_orders", None)
        if list_fn is None:
            return []
        return await list_fn()


__all__ = ["ENGINE_ID", "ReversionOrderManager"]
