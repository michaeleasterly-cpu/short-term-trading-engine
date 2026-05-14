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

import json
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import structlog

from reversion.models import Direction, ExecutionDecision, PhaseAssessment
from reversion.plugs.aar_logging import ReversionAARLogging
from reversion.plugs.capital_gate import ReversionCapitalGate
from reversion.plugs.lifecycle_analysis import ReversionLifecycleAnalysis
from tpcore.aar.models import AfterActionReport
from tpcore.aar.writer import AARWriter
from tpcore.interfaces.broker import (
    BrokerExecutionInterface,
    Order,
    OrderSide,
    OrderStatus,
)
from tpcore.order_ids import parse_cid
from tpcore.parity import LivePaperParityHarness
from tpcore.risk.governor import RiskDecision, RiskGovernor

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)

ENGINE_ID = "reversion"


def _trade_key(client_order_id: str) -> str:
    """Trade-pairing key via :func:`tpcore.order_ids.parse_cid`.

    Accepts canonical ``rv_<TICKER>_<TS>_tierN`` and legacy
    ``<TICKER>_<TS>_tierN`` formats so in-flight orders from before the
    prefix migration still reconcile correctly.
    """
    parsed = parse_cid(client_order_id)
    return parsed.trade_key or client_order_id


def _tier(client_order_id: str) -> str | None:
    """Tier label or None if not a tier-bracket cid (filters out cross-engine orders)."""
    return parse_cid(client_order_id).tier


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
        pool: asyncpg.Pool | None = None,
    ) -> None:
        self._broker = broker
        self._governor = governor
        self._capital_gate = capital_gate
        self._lifecycle = lifecycle
        self._aar = aar
        self._aar_writer = aar_writer
        self._parity = parity_harness
        # asyncpg pool for platform.open_orders persistence — required for
        # the trade monitor to find Tier 1 rows. See sigma/order_manager.py.
        self._pool = pool or (aar_writer.pool if aar_writer is not None else None)
        self._trade_assessments: dict[str, PhaseAssessment] = {}
        self._tier1_logged: set[str] = set()
        self._tier2_logged: set[str] = set()

    async def submit_decision(
        self,
        decision: ExecutionDecision,
        assessment: PhaseAssessment,
    ) -> list[Order] | None:
        engine_state = await self._governor.state_for(ENGINE_ID)
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
        # already what RiskGovernor.check_trade enforces. Edge: Tier 1's
        # 20-MA target — the closer mean-revert target, conservative.
        side = OrderSide.BUY if decision.direction is Direction.LONG else OrderSide.SELL
        if assessment.entry_price > 0:
            if decision.direction is Direction.LONG:
                expected_edge = (
                    (assessment.target_20ma - assessment.entry_price)
                    / assessment.entry_price
                )
            else:
                expected_edge = (
                    (assessment.entry_price - assessment.target_20ma)
                    / assessment.entry_price
                )
        else:
            expected_edge = Decimal("0")
        check = await self._governor.check_trade(
            engine_id=ENGINE_ID,
            size=decision.notional_usd,
            direction=side,
            ticker=decision.ticker,
            expected_edge_pct=expected_edge,
        )
        if check.decision is RiskDecision.BLOCK:
            logger.warning(
                "reversion.order_manager.governor_blocked",
                ticker=decision.ticker,
                reason=check.reason,
            )
            return None

        # Submit only the Tier 1 bracket; trade monitor handles Tier 2 after fill.
        tier1_payload = decision.order_payloads[0]
        tier1_order = await self._broker.submit_tier1_only(
            ticker=decision.ticker,
            qty=decision.tier1_qty,
            side=tier1_payload["side"],
            take_profit_price=assessment.target_20ma,
            stop_loss_price=assessment.stop_price,
            client_order_id=str(tier1_payload["client_order_id"]),
            engine_id=ENGINE_ID,
        )
        placed = [tier1_order]

        trade_key = _trade_key(tier1_order.client_order_id)
        await self._persist_tier1_to_open_orders(
            tier1_order=tier1_order,
            trade_key=trade_key,
            decision=decision,
            assessment=assessment,
        )
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
            qty=decision.tier1_qty,
            tier2_pending=decision.tier2_qty,
            notional=str(decision.notional_usd),
            broker_order_id=tier1_order.broker_order_id,
        )

        # Parity harness — non-blocking, mirrors Sigma + Vector.
        if self._parity is not None:
            try:
                await self._parity.submit_pair(tier1_order)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "reversion.order_manager.parity_harness_failed",
                    client_order_id=tier1_order.client_order_id,
                    error=str(exc),
                )

        return placed

    async def _persist_tier1_to_open_orders(
        self,
        *,
        tier1_order: Order,
        trade_key: str,
        decision: ExecutionDecision,
        assessment: PhaseAssessment,
    ) -> None:
        """Insert the Tier 1 row that the trade monitor will react to.

        Mirror of ``sigma.order_manager._persist_tier1_to_open_orders``.
        """
        if self._pool is None:
            logger.warning(
                "reversion.order_manager.no_pool_persistence_skipped",
                trade_key=trade_key,
                broker_order_id=tier1_order.broker_order_id,
            )
            return
        payload = json.dumps(
            {
                "decision": decision.model_dump(mode="json"),
                "assessment": assessment.model_dump(mode="json"),
            },
            default=str,
        )
        sql = """
            INSERT INTO platform.open_orders
                (engine, trade_id, ticker, order_type,
                 alpaca_order_id, status, decision_data)
            VALUES ($1, $2, $3, 'tier1', $4, 'pending', $5::jsonb)
            ON CONFLICT (engine, trade_id, order_type)
            DO UPDATE SET
                alpaca_order_id = EXCLUDED.alpaca_order_id,
                decision_data = EXCLUDED.decision_data,
                updated_at = now()
        """
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    sql,
                    ENGINE_ID,
                    trade_key,
                    decision.ticker,
                    tier1_order.broker_order_id,
                    payload,
                )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "reversion.order_manager.open_orders_persist_failed",
                trade_key=trade_key,
                error=str(exc),
            )

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
