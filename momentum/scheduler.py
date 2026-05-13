"""Momentum scheduler — daily entry point.

Wires the five Momentum plugs + Alpaca broker + Risk Governor + Postgres
into a single :meth:`MomentumScheduler.run_once` invocation that an external
scheduler (cron, systemd timer, manual ``python -m momentum.scheduler``) can
call.

Cadence
-------
Momentum rebalances *monthly*, on the first trading day of each calendar
month. The scheduler is safe to call every trading day — it'll quietly
no-op on non-rebalance days. This matches the operational model of the
other engines (call every session, plug decides whether to act).

Responsibilities each run
-------------------------
1. Build asyncpg pool + Alpaca broker + Risk Governor.
2. Lifecycle plug: is today a rebalance day?
3. If yes:
    a. Setup plug ranks the universe → list of candidates.
    b. Pull current Alpaca portfolio.
    c. Execution-Risk plug builds the target portfolio + order batch.
    d. Capital gate sanity-checks total buy notional vs allocated equity.
    e. Submit each market order via the broker, in this order:
       all SELLs first (free up cash) → all BUYs (deploy it).
    f. Log each submitted order to ``platform.application_log``.
4. If no: log a single 'no rebalance today' line and exit cleanly.

What this scheduler does NOT do (deliberately)
----------------------------------------------
* No bracket orders. Momentum doesn't use per-name stops — risk is managed
  by the monthly rebalance discipline.
* No trade-monitor handoff. There are no Tier 2 legs to submit reactively.
* No per-fill AAR write. AARs are written when a position is CLOSED on a
  subsequent rebalance — see :class:`MomentumAARLogging`.

Dry-run mode
------------
Pass ``--dry-run`` (or construct with ``submit_orders=False``) to compute
the rebalance plan without submitting any orders. Useful for paper-trading
preflight and for the CI smoke test.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from datetime import UTC, date as date_t, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import structlog

from momentum.models import RebalanceDecision
from momentum.plugs.aar_logging import MomentumAARLogging
from momentum.plugs.capital_gate import MomentumCapitalGate
from momentum.plugs.execution_risk import MomentumExecutionRisk
from momentum.plugs.lifecycle_analysis import MomentumLifecycleAnalysis
from momentum.plugs.setup_detection import MomentumSetupDetection
from tpcore.alpaca import AlpacaPaperBrokerAdapter
from tpcore.db import build_asyncpg_pool
from tpcore.interfaces.broker import (
    Order,
    OrderClass,
    OrderSide,
    OrderType,
    TimeInForce,
)
from tpcore.logging import DBLogHandler
from tpcore.risk.governor import RiskGovernor
from tpcore.risk.persistent_store import PostgresRiskStateStore

if TYPE_CHECKING:  # pragma: no cover
    pass

logger = structlog.get_logger(__name__)


class RunSummary:
    """Result of one ``run_once`` invocation — printable + JSON-serialisable."""

    def __init__(
        self,
        *,
        as_of: date_t,
        is_rebalance_day: bool,
        decision: RebalanceDecision | None,
        submitted_order_ids: list[str],
        dry_run: bool,
    ) -> None:
        self.as_of = as_of
        self.is_rebalance_day = is_rebalance_day
        self.decision = decision
        self.submitted_order_ids = submitted_order_ids
        self.dry_run = dry_run

    def __repr__(self) -> str:
        if not self.is_rebalance_day:
            return f"RunSummary(as_of={self.as_of}, action=no_rebalance)"
        d = self.decision
        if d is None:
            return f"RunSummary(as_of={self.as_of}, action=rebalance, decision=None)"
        return (
            f"RunSummary(as_of={self.as_of}, action=rebalance, dry_run={self.dry_run}, "
            f"targets={len(d.targets)}, orders={len(d.orders)}, "
            f"open={d.n_open}/close={d.n_close}/inc={d.n_increase}/dec={d.n_decrease}/hold={d.n_hold}, "
            f"submitted={len(self.submitted_order_ids)})"
        )


class MomentumScheduler:
    """One-shot orchestration of a full Momentum rebalance cycle."""

    def __init__(
        self,
        *,
        engine_equity_usd: Decimal = Decimal("10000"),
        submit_orders: bool = True,
        force_rebalance: bool = False,
    ) -> None:
        self._engine_equity = engine_equity_usd
        self._submit = submit_orders
        self._force_rebalance = force_rebalance

    async def run_once(self, as_of: date_t | None = None) -> RunSummary:
        as_of = as_of or datetime.now(UTC).date()
        db_url = os.getenv("DATABASE_URL")
        if not db_url:
            raise RuntimeError("DATABASE_URL not set — cannot run momentum.scheduler")

        pool = await build_asyncpg_pool(db_url)
        # Phase 2.5 #2 — wire DBLogHandler so SIGNAL / ORDER_SUBMITTED events
        # land in platform.application_log for the tip-sheet's "Recent
        # signals" section to find them.
        run_id = uuid.uuid4()
        db_log = DBLogHandler(pool=pool, engine="momentum", run_id=run_id)
        try:
            broker = AlpacaPaperBrokerAdapter()
            state_store = PostgresRiskStateStore(pool=pool)
            governor = RiskGovernor(
                state_store=state_store, broker=broker, pool=pool,
            )

            # Plug 2 — is today a rebalance day? --force-rebalance overrides
            # for the initial paper-trading kickoff (and for emergency
            # mid-month rebalances, which are rare and operator-judgement).
            lifecycle = MomentumLifecycleAnalysis()
            plan = await lifecycle.assess(pool, as_of)
            if not plan.is_rebalance_day and not self._force_rebalance:
                logger.info(
                    "momentum.scheduler.no_rebalance",
                    as_of=as_of.isoformat(),
                    reason=plan.reason,
                )
                return RunSummary(
                    as_of=as_of, is_rebalance_day=False,
                    decision=None, submitted_order_ids=[], dry_run=not self._submit,
                )
            if not plan.is_rebalance_day and self._force_rebalance:
                logger.warning(
                    "momentum.scheduler.force_rebalance_override",
                    as_of=as_of.isoformat(),
                    natural_reason=plan.reason,
                )

            # Plug 1 — rank candidates.
            setup = MomentumSetupDetection()
            candidates = await setup.scan(pool, as_of)

            # Pull current Alpaca holdings.
            account = await broker.get_account()
            equity = account.equity if account.equity > 0 else self._engine_equity
            positions = await broker.get_positions()
            current_holdings = {p.symbol: int(p.qty) for p in positions if int(p.qty) > 0}

            # Plug 3 — build rebalance decision.
            execution = MomentumExecutionRisk(governor=governor)
            decision = await execution.build_decision(
                candidates=candidates,
                equity_usd=equity,
                current_holdings=current_holdings,
                as_of=as_of,
            )

            # Emit one SIGNAL event per target so the tip-sheet's signals
            # section can correlate today's ranking to actual rebalance
            # output. Done BEFORE the capital gate so even a gate-rejected
            # rebalance leaves an audit trail of what the engine would have
            # done.
            for tgt in decision.targets:
                await db_log.signal(
                    tgt.ticker, score=float(tgt.momentum_score), direction="LONG",
                )

            # Plug 4 — capital gate.
            gate = MomentumCapitalGate(engine_equity_usd=equity)
            if decision.orders and not gate.check_rebalance(decision.total_buy_notional_usd):
                logger.warning(
                    "momentum.scheduler.gate_rejected_rebalance",
                    buy_notional=str(decision.total_buy_notional_usd),
                    equity=str(equity),
                )
                return RunSummary(
                    as_of=as_of, is_rebalance_day=True,
                    decision=decision, submitted_order_ids=[], dry_run=not self._submit,
                )

            # Cancel any of our own stale open orders before submitting new
            # ones. Otherwise positions remain "held_for_orders" and a fresh
            # sell will be rejected with `available=0`. We identify our orders
            # by the `mo_` client_order_id prefix the execution plug stamps.
            if self._submit:
                await self._cancel_stale_momentum_orders(broker)

            # Submit orders — sells first, then buys. Per-order try/except so
            # one rejection doesn't abort the whole rebalance.
            submitted: list[str] = []
            failed: list[tuple[str, str]] = []
            sells = [o for o in decision.orders if o.side == "sell"]
            buys = [o for o in decision.orders if o.side == "buy"]

            for order in sells + buys:
                if not self._submit:
                    logger.info(
                        "momentum.scheduler.dry_run_skip",
                        ticker=order.ticker, action=order.action.value,
                        qty=order.qty, side=order.side,
                    )
                    continue
                try:
                    placed = await broker.place_order(self._payload_to_order(order))
                except Exception as exc:  # noqa: BLE001
                    failed.append((order.ticker, str(exc)[:200]))
                    logger.error(
                        "momentum.scheduler.order_failed",
                        ticker=order.ticker, action=order.action.value,
                        qty=order.qty, side=order.side, error=str(exc)[:200],
                    )
                    continue
                if placed.broker_order_id is not None:
                    submitted.append(placed.broker_order_id)
                logger.info(
                    "momentum.scheduler.order_submitted",
                    ticker=order.ticker, action=order.action.value,
                    qty=order.qty, side=order.side,
                    broker_order_id=placed.broker_order_id,
                )
                await db_log.order_submitted(
                    order.ticker, quantity=order.qty,
                    order_id=placed.broker_order_id,
                )
            if failed:
                logger.warning(
                    "momentum.scheduler.partial_rebalance",
                    n_submitted=len(submitted), n_failed=len(failed),
                    failures=failed[:10],
                )

            return RunSummary(
                as_of=as_of, is_rebalance_day=True,
                decision=decision, submitted_order_ids=submitted, dry_run=not self._submit,
            )
        finally:
            await pool.close()

    @staticmethod
    async def _cancel_stale_momentum_orders(broker) -> int:
        """Cancel any open orders we own (client_order_id starts with ``mo_``)
        so positions held_for_orders are released before the new rebalance.

        Returns the number of orders cancelled. Silently degrades when the
        broker doesn't expose ``list_recent_orders`` (non-Alpaca brokers)."""
        list_fn = getattr(broker, "list_recent_orders", None)
        if list_fn is None:
            return 0
        try:
            recent = await list_fn(limit=500)
        except Exception as exc:  # noqa: BLE001
            logger.warning("momentum.scheduler.list_orders_failed", error=str(exc)[:200])
            return 0
        # Open statuses worth cancelling: NEW, PARTIALLY_FILLED (cancel
        # cancels the remainder). Already-filled / cancelled / rejected are
        # terminal and left alone.
        open_statuses = {"new", "partially_filled", "accepted", "pending_new"}
        cancelled = 0
        for o in recent:
            cid = (o.client_order_id or "").lower()
            if not cid.startswith("mo_"):
                continue
            status_val = getattr(o.status, "value", str(o.status)).lower()
            if status_val not in open_statuses:
                continue
            if not o.broker_order_id:
                continue
            try:
                await broker.cancel_order(o.broker_order_id)
                cancelled += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "momentum.scheduler.cancel_failed",
                    broker_order_id=o.broker_order_id,
                    client_order_id=o.client_order_id, error=str(exc)[:200],
                )
        if cancelled:
            logger.info("momentum.scheduler.stale_orders_cancelled", n=cancelled)
        return cancelled

    @staticmethod
    def _payload_to_order(order):
        """Build a :class:`tpcore.interfaces.broker.Order` from a
        :class:`momentum.models.RebalanceOrder`. Day-market only."""
        payload = order.order_payload
        return Order(
            client_order_id=payload["client_order_id"],
            symbol=payload["symbol"],
            side=OrderSide.BUY if payload["side"] == "buy" else OrderSide.SELL,
            qty=Decimal(payload["qty"]),
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.DAY,
            order_class=OrderClass.SIMPLE,
            engine_id="momentum",
        )


# ────────────────────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────────────────────


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument(
        "--as-of",
        type=date_t.fromisoformat,
        default=None,
        help="Override the as-of date (ISO format); defaults to today (UTC).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute the rebalance plan but don't submit orders. Prints the decision.",
    )
    p.add_argument(
        "--engine-equity",
        type=Decimal,
        default=Decimal("10000"),
        help="Engine equity in USD (default $10,000). Used as fallback when broker query fails.",
    )
    p.add_argument(
        "--force-rebalance",
        action="store_true",
        help=(
            "Override the 'is first trading day of month' check and rebalance "
            "regardless. Used for the initial paper-trading kickoff and rare "
            "emergency mid-month rebalances."
        ),
    )
    return p.parse_args(argv)


async def amain(args: argparse.Namespace) -> int:
    sched = MomentumScheduler(
        engine_equity_usd=args.engine_equity,
        submit_orders=not args.dry_run,
        force_rebalance=args.force_rebalance,
    )
    summary = await sched.run_once(as_of=args.as_of)
    print(summary)
    if summary.decision is not None:
        print()
        for tgt in summary.decision.targets[:10]:
            print(f"  target  {tgt.ticker:<6}  {tgt.target_shares:>4} sh  ${tgt.target_notional_usd}  score={tgt.momentum_score:+.4f}")
        if len(summary.decision.targets) > 10:
            print(f"  … ({len(summary.decision.targets) - 10} more targets)")
        print()
        for o in summary.decision.orders[:10]:
            print(f"  order   {o.ticker:<6}  {o.action.value:<8}  {o.side} {o.qty:>4} sh  ${o.notional_usd}")
        if len(summary.decision.orders) > 10:
            print(f"  … ({len(summary.decision.orders) - 10} more orders)")
    return 0


def main() -> None:  # pragma: no cover - CLI shim
    raise SystemExit(asyncio.run(amain(_parse_args())))


if __name__ == "__main__":  # pragma: no cover
    main()
