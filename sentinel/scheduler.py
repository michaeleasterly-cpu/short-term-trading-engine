"""Sentinel scheduler — daily entry point.

Wires the five Sentinel plugs + Alpaca broker + Risk Governor + Postgres
into a single :meth:`SentinelScheduler.run_once` invocation that an
external scheduler (launchd, cron, manual ``python -m
sentinel.scheduler``) can call.

Cadence
-------
Sentinel checks the Bear Score *daily* but rebalances *only* on state
transitions (DORMANT/EXITED → WATCH, WATCH → ACTIVE, ACTIVE → FADING,
FADING → EXITED). The scheduler is safe to call every trading day —
it'll no-op on a no-transition day rather than submitting churn orders.

Responsibilities each run
-------------------------
1. Build pool + broker + Risk Governor + DBLogHandler.
2. Pull a rolling 90-day window of Bear Score breakdowns + states.
3. Take today's state. Compare phase to yesterday's phase.
4. If today's phase requires an order (entry, fade-step, exit): build
   :class:`SentinelDecision` and submit market orders for each delta.
5. Log SIGNAL + ORDER_SUBMITTED to ``platform.application_log``.
6. Skip order submission entirely under ``--dry-run``.

Out of scope (deferred to a follow-up)
--------------------------------------
* SQQQ position-age tracking (the 5-day max hold rule) is computed
  from ``sqqq_days_held`` in :class:`SentinelState`, but the scheduler
  doesn't yet auto-rotate to TLT/cash when the 5-day cap is reached
  beyond simply de-eligibility-flagging — the execution diff handles
  the exit on the next state-driven rebalance.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from datetime import date as date_t
from decimal import Decimal

import structlog

from sentinel.models import (
    BASKET_WEIGHTS_DEFAULT,
)
from sentinel.plugs.aar_logging import SentinelAARLogging  # noqa: F401  (engine-readiness wiring)
from sentinel.plugs.capital_gate import SentinelCapitalGate
from sentinel.plugs.execution_risk import SentinelExecutionRisk
from sentinel.plugs.lifecycle_analysis import SentinelLifecycleAnalysis
from sentinel.plugs.setup_detection import (
    SentinelSetupDetection,
    fetch_spy_close,
)
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
from tpcore.order_ids import ENGINE_PREFIX, build_cid, is_engine_cid
from tpcore.risk.governor import RiskGovernor
from tpcore.risk.persistent_store import PostgresRiskStateStore

logger = structlog.get_logger(__name__)

ENGINE_ORDER_PREFIX = ENGINE_PREFIX["sentinel"]  # "sn_"
LOOKBACK_DAYS = 90  # rolling window for state replay


def _filter_to_engine_holdings(positions, recent_orders, engine: str) -> dict[str, int]:
    """Filter broker positions to those originated by ``engine``."""
    engine_symbols = {
        o.symbol for o in recent_orders
        if is_engine_cid(getattr(o, "client_order_id", None), engine)
    }
    return {
        p.symbol: int(p.qty)
        for p in positions
        if int(p.qty) > 0 and p.symbol in engine_symbols
    }


class SentinelScheduler:
    """One-shot orchestration of a Sentinel daily check + (if needed) rebalance."""

    def __init__(
        self,
        *,
        platform_equity_usd: Decimal = Decimal("100000"),
        graduated: bool = False,
        submit_orders: bool = True,
    ) -> None:
        self._platform_equity = platform_equity_usd
        self._graduated = graduated
        self._submit = submit_orders

    async def run_once(self, as_of: date_t | None = None) -> dict[str, object]:
        as_of = as_of or datetime.now(UTC).date()
        db_url = os.getenv("DATABASE_URL")
        if not db_url:
            raise RuntimeError("DATABASE_URL not set — cannot run sentinel.scheduler")

        pool = await build_asyncpg_pool(db_url)
        run_id = uuid.uuid4()
        db_log = DBLogHandler(pool=pool, engine="sentinel", run_id=run_id)
        try:
            broker = AlpacaPaperBrokerAdapter()
            state_store = PostgresRiskStateStore(pool=pool)
            governor = RiskGovernor(state_store=state_store, broker=broker, pool=pool)

            # Kill-switch pre-flight — same pattern as Momentum.
            risk_state = await governor.state_for("sentinel")
            if risk_state and risk_state.kill_switch_active:
                logger.critical(
                    "sentinel.scheduler.kill_switch_active",
                    as_of=as_of.isoformat(),
                    reason=risk_state.kill_switch_reason,
                )
                return {"as_of": as_of.isoformat(), "action": "kill_switch_halt"}

            # Replay phases over a rolling window so we don't need to
            # persist daily state — derivable from breakdowns + SPY.
            start = as_of - timedelta(days=LOOKBACK_DAYS)
            setup = SentinelSetupDetection()
            breakdowns = await setup.compute_for_range(pool, start=start, end=as_of)
            if as_of not in breakdowns:
                logger.warning(
                    "sentinel.scheduler.no_breakdown_for_today",
                    as_of=as_of.isoformat(), available=len(breakdowns),
                )
                return {"as_of": as_of.isoformat(), "action": "no_data"}
            spy = await fetch_spy_close(pool, start=start, end=as_of)
            lifecycle = SentinelLifecycleAnalysis()
            states = lifecycle.walk_states(breakdowns, spy_close=spy)
            today = states[as_of]

            # Get today's ETF closes for sizing.
            prices_today = await _latest_prices(pool, as_of, list(BASKET_WEIGHTS_DEFAULT.keys()))
            if not prices_today:
                logger.warning("sentinel.scheduler.no_prices", as_of=as_of.isoformat())
                return {"as_of": as_of.isoformat(), "action": "no_prices"}

            # Current Sentinel holdings (broker-side, attributed by cid).
            positions = await broker.get_positions()
            recent_orders = await broker.list_recent_orders(limit=500)
            current_holdings = _filter_to_engine_holdings(positions, recent_orders, "sentinel")

            execution = SentinelExecutionRisk(graduated=self._graduated)
            decision = execution.build_decision(
                as_of=as_of, state=today,
                equity_usd=self._platform_equity,
                prices=prices_today,
                current_holdings=current_holdings,
            )

            # Capital gate — sanity check the buy notional.
            gate = SentinelCapitalGate(graduated=self._graduated)
            buy_notional = sum(
                o.notional_usd for o in decision.orders if o.side == "buy"
            ) or Decimal("0")
            if not gate.check_rebalance(buy_notional, self._platform_equity):
                return {"as_of": as_of.isoformat(), "action": "gate_rejected",
                        "buy_notional": str(buy_notional)}

            # No orders to submit → log + return.
            if not decision.orders:
                logger.info(
                    "sentinel.scheduler.no_orders",
                    phase=today.phase.value, bear_score=today.bear_score,
                )
                return {"as_of": as_of.isoformat(), "action": "no_orders",
                        "phase": today.phase.value, "bear_score": today.bear_score}

            # Signal events — one per target so the dashboard sees them.
            for tgt in decision.targets:
                await db_log.signal(
                    tgt.ticker, score=float(today.bear_score), direction="LONG",
                )

            submitted: list[str] = []
            failed: list[tuple[str, str]] = []
            sells = [o for o in decision.orders if o.side == "sell"]
            buys = [o for o in decision.orders if o.side == "buy"]
            for order in sells + buys:
                if not self._submit:
                    logger.info("sentinel.scheduler.dry_run_skip",
                                ticker=order.ticker, qty=order.qty, side=order.side)
                    continue
                try:
                    placed = await broker.place_order(self._build_order(order))
                except Exception as exc:  # noqa: BLE001
                    failed.append((order.ticker, str(exc)[:200]))
                    logger.error("sentinel.scheduler.order_failed",
                                 ticker=order.ticker, error=str(exc)[:200])
                    continue
                if placed.broker_order_id:
                    submitted.append(placed.broker_order_id)
                await db_log.order_submitted(
                    order.ticker, quantity=order.qty,
                    order_id=placed.broker_order_id,
                )

            return {
                "as_of": as_of.isoformat(),
                "action": "rebalanced",
                "phase": today.phase.value,
                "bear_score": today.bear_score,
                "submitted_orders": submitted,
                "failures": failed,
                "missing_etfs": list(decision.missing_etfs),
                "n_targets": len(decision.targets),
                "n_orders": len(decision.orders),
                "fade_factor": str(today.fade_factor),
            }
        finally:
            await pool.close()

    @staticmethod
    def _build_order(order):
        return Order(
            client_order_id=build_cid("sentinel", order.ticker),
            symbol=order.ticker,
            side=OrderSide.BUY if order.side == "buy" else OrderSide.SELL,
            qty=Decimal(order.qty),
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.DAY,
            order_class=OrderClass.SIMPLE,
            engine_id="sentinel",
        )


async def _latest_prices(pool, as_of: date_t, tickers: list[str]) -> dict[str, Decimal]:
    """Most-recent close at or before ``as_of`` per ticker (Decimal)."""
    sql = """
        SELECT DISTINCT ON (ticker) ticker, close
        FROM platform.prices_daily
        WHERE ticker = ANY($1) AND date <= $2
        ORDER BY ticker, date DESC
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, tickers, as_of)
    return {r["ticker"]: Decimal(str(r["close"])) for r in rows}


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--as-of", type=date_t.fromisoformat, default=None)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--platform-equity", type=Decimal, default=Decimal("100000"))
    p.add_argument("--graduated", action="store_true")
    return p.parse_args(argv)


async def amain(args: argparse.Namespace) -> int:
    sched = SentinelScheduler(
        platform_equity_usd=args.platform_equity,
        graduated=args.graduated,
        submit_orders=not args.dry_run,
    )
    summary = await sched.run_once(as_of=args.as_of)
    print(summary)
    return 0


def main() -> None:  # pragma: no cover — CLI shim
    raise SystemExit(asyncio.run(amain(_parse_args())))


if __name__ == "__main__":  # pragma: no cover
    main()
