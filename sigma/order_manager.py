"""Sigma order manager — orchestrates plug pipeline → broker → AAR.

Responsibilities:
    1. ``submit_decision`` runs an ``ExecutionDecision`` through the local
       capital gate, the platform-wide ``RiskGovernor``, and finally the
       broker. On success it bumps the governor's ``open_positions`` counter.
    2. ``reconcile`` lists the broker's orders and decides what events
       happened since the last run — Tier 1 fill, Tier 2 fill, hard stop —
       then drives the lifecycle/AAR plugs accordingly.

State is held by the broker. Each Sigma trade carries a stable
``client_order_id`` prefix (``{ticker}_{ts}``) with ``_tier1`` / ``_tier2``
suffixes so the manager can group legs without a local DB.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal

import structlog

from tpcore.aar.models import AfterActionReport
from tpcore.interfaces.broker import (
    BrokerExecutionInterface,
    Order,
    OrderSide,
    OrderStatus,
)
from tpcore.risk.governor import RiskDecision, RiskGovernor

from sigma.models import ExecutionDecision, PhaseAssessment
from sigma.plugs.aar_logging import SigmaAARLogging
from sigma.plugs.capital_gate import SigmaCapitalGate
from sigma.plugs.lifecycle_analysis import SigmaLifecycleAnalysis

logger = structlog.get_logger(__name__)

ENGINE_ID = "sigma"


def _trade_key(client_order_id: str) -> str:
    """Strip the ``_tier1``/``_tier2`` suffix to get the shared trade prefix."""
    if client_order_id.endswith("_tier1"):
        return client_order_id[: -len("_tier1")]
    if client_order_id.endswith("_tier2"):
        return client_order_id[: -len("_tier2")]
    return client_order_id


def _tier(client_order_id: str) -> str | None:
    """Return ``"tier1"`` / ``"tier2"`` / ``None`` for non-Sigma orders."""
    if client_order_id.endswith("_tier1"):
        return "tier1"
    if client_order_id.endswith("_tier2"):
        return "tier2"
    return None


class SigmaOrderManager:
    """Drives a Sigma trade from execution decision through final AAR.

    Args:
        broker: anything implementing ``BrokerExecutionInterface``. Must
            additionally expose ``submit_execution_decision`` for the two-leg
            scale-out (``AlpacaPaperBrokerAdapter`` does).
        governor: cross-engine ``RiskGovernor``.
        capital_gate: per-engine sizing/equity gate.
        lifecycle: ``SigmaLifecycleAnalysis`` (used for ``handle_tier1_fill``).
        aar: ``SigmaAARLogging`` for partial/final AAR construction + logging.
    """

    def __init__(
        self,
        *,
        broker: BrokerExecutionInterface,
        governor: RiskGovernor,
        capital_gate: SigmaCapitalGate,
        lifecycle: SigmaLifecycleAnalysis,
        aar: SigmaAARLogging,
    ) -> None:
        self._broker = broker
        self._governor = governor
        self._capital_gate = capital_gate
        self._lifecycle = lifecycle
        self._aar = aar
        # ticker → PhaseAssessment for every trade we've placed this process.
        # The broker is the source of truth for orders; this is a side cache
        # so we can carry assessment context (entry, stop, mid/upper) into the
        # tier-handling flow without re-deriving it from prices.
        self._trade_assessments: dict[str, PhaseAssessment] = {}
        # set of trade_keys already AAR'd to make reconcile idempotent across runs.
        self._tier1_logged: set[str] = set()
        self._tier2_logged: set[str] = set()

    async def submit_decision(
        self,
        decision: ExecutionDecision,
        assessment: PhaseAssessment,
    ) -> list[Order] | None:
        """Run ``decision`` through gates and ship it. Returns the placed
        orders, or ``None`` if any gate blocked.
        """
        # Local capital gate first (cheap, no I/O).
        engine_state = await self._governor._store.get(ENGINE_ID)  # noqa: SLF001 — read-only peek
        engine_pnl = engine_state.daily_pnl if engine_state else Decimal("0")
        open_count = engine_state.open_positions if engine_state else 0

        if not self._capital_gate.check_trade(
            size=decision.notional_usd,
            engine_pnl=engine_pnl,
            open_positions=open_count,
        ):
            logger.info(
                "sigma.order_manager.gate_blocked",
                ticker=decision.ticker,
                size=str(decision.notional_usd),
                engine_pnl=str(engine_pnl),
            )
            return None

        # Platform-wide governor.
        check = await self._governor.check_trade(
            engine_id=ENGINE_ID,
            size=decision.notional_usd,
            direction=OrderSide.BUY,
        )
        if check.decision is RiskDecision.BLOCK:
            logger.warning(
                "sigma.order_manager.governor_blocked",
                ticker=decision.ticker,
                reason=check.reason,
            )
            return None

        placed = await self._broker.submit_execution_decision(decision)
        # Cache the assessment under the trade key so reconcile can build AARs.
        if placed:
            trade_key = _trade_key(placed[0].client_order_id)
            self._trade_assessments[trade_key] = assessment

        # Bump open-position counter once per *trade*, not per leg.
        await self._governor.record_fill(
            engine_id=ENGINE_ID,
            realized_pnl=Decimal("0"),
            position_delta=1,
        )
        logger.info(
            "sigma.order_manager.trade_submitted",
            ticker=decision.ticker,
            qty=decision.qty,
            tier1_qty=decision.tier1_qty,
            tier2_qty=decision.tier2_qty,
            notional=str(decision.notional_usd),
        )
        return placed

    async def reconcile(
        self,
        *,
        sizing_pct_of_engine_equity: Decimal,
        confidence_at_entry: Decimal = Decimal("0.80"),
    ) -> list[AfterActionReport]:
        """Pull broker order history, fire tier events, return any new AARs.

        The method is idempotent: repeated calls during the same process
        won't double-log AARs. Across processes the (engine, trade_id)
        unique constraint on ``platform.aar_events`` provides the same
        guarantee at the DB layer.
        """
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

            # Tier 1 fill → partial AAR + lifecycle handoff to remaining shares.
            if (
                tier1 is not None
                and tier1.status is OrderStatus.FILLED
                and trade_key not in self._tier1_logged
                and assessment is not None
                and tier1.avg_fill_price is not None
                and tier1.filled_at is not None
            ):
                aar = self._aar.build_tier1_aar(
                    trade_id=f"sigma-{trade_key}",
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
                new_aars.append(aar)
                self._tier1_logged.add(trade_key)
                # Update lifecycle state — phase stays ACTIVE while tier 2 runs.
                remaining = (
                    int(tier2.qty) if tier2 is not None and tier2.status is not OrderStatus.CANCELED else 0
                )
                self._trade_assessments[trade_key] = self._lifecycle.handle_tier1_fill(
                    assessment, position_remaining=remaining
                )

            # Hard-stop fired before Tier 1 → bracket SL filled, Tier 1 itself
            # appears as cancelled in some paths; cancel any open Tier 2 limit.
            if (
                tier1 is not None
                and tier1.status in (OrderStatus.CANCELED, OrderStatus.REJECTED)
                and trade_key not in self._tier1_logged
                and tier2 is not None
                and tier2.status not in (OrderStatus.CANCELED, OrderStatus.FILLED)
                and tier2.broker_order_id is not None
            ):
                logger.warning(
                    "sigma.order_manager.tier1_cancelled_before_fill",
                    trade_key=trade_key,
                    cancelling_tier2_id=tier2.broker_order_id,
                )
                await self._broker.cancel_order(tier2.broker_order_id)
                # Mark tier1 as logged so we don't keep retrying — final AAR
                # for the stop fill is the operator's responsibility for now.
                self._tier1_logged.add(trade_key)

            # Tier 2 fill → final AAR with combined P&L.
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
                    trade_id=f"sigma-{trade_key}",
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
                new_aars.append(final)
                self._tier2_logged.add(trade_key)
                await self._governor.record_fill(
                    engine_id=ENGINE_ID,
                    realized_pnl=final.pnl_net,
                    position_delta=-1,
                )

        return new_aars

    async def _fetch_recent_orders(self) -> list[Order]:
        """Return Orders the broker knows about, both open and recently closed.

        ``BrokerExecutionInterface`` doesn't have a ``list_orders`` method
        today, so we use ``getattr`` to opt in when the adapter exposes one.
        """
        list_fn = getattr(self._broker, "list_recent_orders", None)
        if list_fn is None:
            return []
        return await list_fn()


__all__ = ["SigmaOrderManager", "ENGINE_ID"]
