"""Sigma order manager — orchestrates plug pipeline → broker → AAR.

Responsibilities:
    1. ``submit_decision`` runs an ``ExecutionDecision`` through the local
       capital gate and the platform-wide ``RiskGovernor``, then submits
       only the Tier 1 bracket via ``broker.submit_tier1_only`` and
       persists ``decision`` + ``assessment`` to ``platform.open_orders``.
       Tier 2 is the trade monitor's responsibility — it watches Alpaca's
       ``trade_updates`` stream and submits Tier 2 reactively after the
       Tier 1 entry fills. See
       ``docs/superpowers/specs/2026-05-12-trade-monitor-design.md``.
    2. ``reconcile`` lists the broker's orders and decides what events
       happened since the last run — Tier 1 fill, Tier 2 fill, hard stop —
       then drives the lifecycle/AAR plugs accordingly.

State is held by the broker. Each Sigma trade carries a stable
``client_order_id`` prefix (``{ticker}_{ts}``) with ``_tier1`` / ``_tier2``
suffixes so the manager can group legs without a local DB.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import structlog

from sigma.models import ExecutionDecision, PhaseAssessment
from sigma.plugs.aar_logging import SigmaAARLogging
from sigma.plugs.capital_gate import SigmaCapitalGate
from sigma.plugs.lifecycle_analysis import SigmaLifecycleAnalysis
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

ENGINE_ID = "sigma"


def _trade_key(client_order_id: str) -> str:
    """Return the trade-pairing key for ``client_order_id``.

    Routes through :func:`tpcore.order_ids.parse_cid` so the same parse
    works on the canonical ``sg_<TICKER>_<TS>_tierN`` format and the
    legacy ``<TICKER>_<TS>_tierN`` format (in-flight orders from before
    the prefix migration). Returns the full cid unchanged if no tier
    suffix is present.
    """
    parsed = parse_cid(client_order_id)
    return parsed.trade_key or client_order_id


def _tier(client_order_id: str) -> str | None:
    """Return ``"tier1"``/``"tier2"`` or ``None`` if not a tier-bracket cid.

    Accepts both canonical (``sg_..._tier1``) and legacy
    (``<TICKER>_..._tier1``) formats via :func:`parse_cid`. Returns
    None for momentum/vector orders so the sigma reconcile loop
    silently ignores them.
    """
    return parse_cid(client_order_id).tier


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
        # the trade monitor to find Tier 1 rows. None falls back to the
        # aar_writer's pool when available; tests can pass None to skip.
        self._pool = pool or (aar_writer.pool if aar_writer is not None else None)
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
        engine_state = await self._governor.state_for(ENGINE_ID)
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

        # Platform-wide governor + cost gate. Edge is the conservative
        # Tier 1 target (mid-band exit), not the Tier 2 far target —
        # if even the close exit can't clear the round-trip cost, the
        # trade has no expected value.
        if assessment.entry_price > 0:
            expected_edge = (
                (assessment.take_profit_mid - assessment.entry_price)
                / assessment.entry_price
            )
        else:
            expected_edge = Decimal("0")
        check = await self._governor.check_trade(
            engine_id=ENGINE_ID,
            size=decision.notional_usd,
            direction=OrderSide.BUY,
            ticker=decision.ticker,
            expected_edge_pct=expected_edge,
        )
        if check.decision is RiskDecision.BLOCK:
            logger.warning(
                "sigma.order_manager.governor_blocked",
                ticker=decision.ticker,
                reason=check.reason,
            )
            return None

        # Submit only the Tier 1 bracket; the trade monitor handles Tier 2
        # reactively after the entry fill arrives on Alpaca's trade_updates.
        tier1_payload = decision.order_payloads[0]
        tier1_order = await self._broker.submit_tier1_only(
            ticker=decision.ticker,
            qty=decision.tier1_qty,
            side=tier1_payload["side"],
            take_profit_price=assessment.take_profit_mid,
            stop_loss_price=assessment.stop_price,
            client_order_id=str(tier1_payload["client_order_id"]),
            engine_id=ENGINE_ID,
        )
        placed = [tier1_order]

        # Persist for the trade monitor to pick up.
        trade_key = _trade_key(tier1_order.client_order_id)
        await self._persist_tier1_to_open_orders(
            tier1_order=tier1_order,
            trade_key=trade_key,
            decision=decision,
            assessment=assessment,
        )

        # Cache the assessment under the trade key so reconcile can build AARs.
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
            qty=decision.tier1_qty,
            tier2_pending=decision.tier2_qty,
            notional=str(decision.notional_usd),
            broker_order_id=tier1_order.broker_order_id,
        )

        # Parity harness — non-blocking. Tier 1 (the bracket entry) is the
        # informative leg; Tier 2 lives in the monitor.
        if self._parity is not None:
            try:
                await self._parity.submit_pair(tier1_order)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "sigma.order_manager.parity_harness_failed",
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

        Idempotent on ``(engine, trade_id, order_type)`` — re-running a
        submission overwrites the broker_order_id but keeps the same row,
        so the monitor sees the latest broker ack.
        """
        if self._pool is None:
            logger.warning(
                "sigma.order_manager.no_pool_persistence_skipped",
                trade_key=trade_key,
                broker_order_id=tier1_order.broker_order_id,
                note="trade monitor will not find this row — set pool or aar_writer with pool",
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
                "sigma.order_manager.open_orders_persist_failed",
                trade_key=trade_key,
                error=str(exc),
            )

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
                if self._aar_writer is not None:
                    await self._aar_writer.write_aar(aar)
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
        """Return Orders the broker knows about, both open and recently closed.

        ``BrokerExecutionInterface`` doesn't have a ``list_orders`` method
        today, so we use ``getattr`` to opt in when the adapter exposes one.
        """
        list_fn = getattr(self._broker, "list_recent_orders", None)
        if list_fn is None:
            return []
        return await list_fn()


__all__ = ["SigmaOrderManager", "ENGINE_ID"]
