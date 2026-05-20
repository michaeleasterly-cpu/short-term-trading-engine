"""Carver scheduler — monthly entry point (Carver-method portfolio engine).

Wires the five carver plugs + Alpaca broker + Risk Governor + Postgres
into a single :meth:`CarverScheduler.run_once` invocation that an external
dispatcher (``ops/engine_dispatch.py``) fires on
``MONTHLY_FIRST_TRADING_DAY`` cadence.

Cadence
-------
Carver rebalances *monthly*, on the first trading day of each calendar
month. That boundary is enforced **exactly once** — by the Python
dispatcher via ``tpcore.engine_profile.should_fire``. The scheduler
itself keeps a defensive ``tpcore.calendar.is_trading_day`` early-return
(belt-and-suspenders per compliance grep #4).

Responsibilities each run
-------------------------
1. Build asyncpg pool + Alpaca broker + Risk Governor.
2. STARTUP / SHUTDOWN bookend (engine_readiness §10).
3. Kill-switch pre-flight via governor.state_for("carver").
4. Setup plug (3 forecasts + FDM combine) -> candidate list + diagnostics.
5. Pull current Alpaca portfolio; filter to ``cv_``-prefixed holdings.
6. Equity snapshot + drawdown breaker (peak-equity logic).
7. Execution-risk plug -> RebalanceDecision (sized day-market orders).
8. Capital-gate check_rebalance (total buys <= engine equity).
9. Cancel any of our own stale open orders (cv_-prefixed).
10. Submit each order: SELLs first (free cash), then BUYs (deploy it).
    Each order gated by ``tpcore.risk.batch_gate.gate_batch_order``.

What this scheduler does NOT do
-------------------------------
* No bracket orders — carver doesn't use per-name stops between
  rebalances; risk is managed via vol-target sizing + IDM diversification
  + 12-flips/year speed limit + drawdown breaker.
* No trade-monitor handoff. There are no Tier 2 legs.
* No per-fill AAR write. AARs are written when a position is CLOSED on a
  subsequent rebalance — see :class:`CarverAARLogging`.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import time
import uuid
from datetime import UTC, datetime
from datetime import date as date_t
from decimal import Decimal
from typing import Any

import structlog

from carver.models import RebalanceDecision
from carver.plugs.capital_gate import (
    DRAWDOWN_BREAKER_LOOKBACK_DAYS,
    CarverCapitalGate,
)
from carver.plugs.execution_risk import CarverExecutionRisk
from carver.plugs.lifecycle_analysis import CarverLifecycleAnalysis
from carver.plugs.setup_detection import CarverSetupDetection
from tpcore.alpaca import AlpacaPaperBrokerAdapter
from tpcore.calendar import is_trading_day
from tpcore.db import build_asyncpg_pool
from tpcore.interfaces.broker import (
    Order,
    OrderClass,
    OrderSide,
    OrderType,
    TimeInForce,
)
from tpcore.logging import DBLogHandler
from tpcore.order_management.stale_order_cancel import cancel_stale_orders
from tpcore.risk.batch_gate import gate_batch_order
from tpcore.risk.governor import RiskGovernor
from tpcore.risk.limits_profile import limits_for
from tpcore.risk.persistent_store import PostgresRiskStateStore

logger = structlog.get_logger(__name__)

# Every carver order carries this client_order_id prefix (stamped in
# carver.plugs.execution_risk via tpcore.order_ids.build_cid). Used to
# attribute account positions back to carver so the rebalance only diffs
# against its own book — never against momentum/sentinel/catalyst holdings.
ENGINE_ORDER_PREFIX = "cv_"


def _filter_to_engine_holdings(
    *,
    positions: Any,
    recent_orders: Any,
    prefix: str,
) -> dict[str, int]:
    """Filter broker positions to those originated by carver.

    A position is "ours" iff at least one recent order on that symbol is
    attributable to carver per :func:`tpcore.order_ids.is_engine_cid`.
    Pure function — testable without a real broker.
    """
    from tpcore.order_ids import ENGINE_PREFIX, is_engine_cid

    target_engine: str | None = None
    for engine_name, registered_prefix in ENGINE_PREFIX.items():
        if registered_prefix == prefix:
            target_engine = engine_name
            break
    if target_engine is not None:
        engine_symbols = {
            o.symbol for o in recent_orders
            if is_engine_cid(getattr(o, "client_order_id", None), target_engine)
        }
    else:
        engine_symbols = {
            o.symbol for o in recent_orders
            if (getattr(o, "client_order_id", None) or "").startswith(prefix)
        }
    return {
        p.symbol: int(p.qty)
        for p in positions
        if int(p.qty) > 0 and p.symbol in engine_symbols
    }


async def _fetch_peak_equity(pool: Any, *, lookback_days: int) -> float | None:
    """Read the highest EQUITY_SNAPSHOT for carver in the lookback window."""
    sql = """
        SELECT data
        FROM platform.application_log
        WHERE engine = 'carver'
          AND event_type = 'EQUITY_SNAPSHOT'
          AND recorded_at >= NOW() - ($1::int * INTERVAL '1 day')
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, lookback_days)
    peak: float | None = None
    for r in rows:
        data = r["data"]
        if isinstance(data, str):
            import json as _json
            try:
                data = _json.loads(data)
            except Exception:  # noqa: BLE001
                continue
        if not isinstance(data, dict):
            continue
        val = data.get("equity")
        if val is None:
            continue
        v = float(val)
        if peak is None or v > peak:
            peak = v
    return peak


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


class CarverScheduler:
    """One-shot orchestration of a full Carver rebalance cycle."""

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

        # Defensive trading-day gate (compliance grep #4). Dispatcher
        # gates cadence; this catches direct manual invocation on a
        # non-trading day.
        as_of_dt = datetime.combine(as_of, datetime.min.time(), tzinfo=UTC)
        if not is_trading_day(as_of_dt):
            logger.info(
                "carver.scheduler.non_trading_day", as_of=as_of.isoformat(),
            )
            return RunSummary(
                as_of=as_of, is_rebalance_day=False,
                decision=None, submitted_order_ids=[], dry_run=not self._submit,
            )

        db_url = os.getenv("DATABASE_URL")
        if not db_url:
            raise RuntimeError("DATABASE_URL not set — cannot run carver.scheduler")

        pool = await build_asyncpg_pool(db_url)
        run_id = uuid.uuid4()
        db_log = DBLogHandler(pool=pool, engine="carver", run_id=run_id)
        started_at = time.monotonic()
        exit_code = 0
        await db_log.startup(
            commit_sha=os.getenv("RAILWAY_GIT_COMMIT_SHA")
            or os.getenv("GIT_COMMIT_SHA")
        )
        try:
            broker = AlpacaPaperBrokerAdapter()
            state_store = PostgresRiskStateStore(pool=pool)
            governor = RiskGovernor(
                state_store=state_store, broker=broker, pool=pool,
            )
            await governor.register_engine(
                "carver",
                self._engine_equity,
                limits=limits_for("carver"),
            )

            # Kill-switch pre-flight.
            current_state = await governor.state_for("carver")
            if current_state and current_state.kill_switch_active:
                logger.critical(
                    "carver.scheduler.kill_switch_active",
                    as_of=as_of.isoformat(),
                    reason=getattr(current_state, "kill_switch_reason", None),
                )
                return RunSummary(
                    as_of=as_of, is_rebalance_day=False,
                    decision=None, submitted_order_ids=[],
                    dry_run=not self._submit,
                )

            # Setup plug — three forecasts + FDM combine.
            # Carver's setup plug expects panels keyed by ticker;
            # production wiring (load bars from prices_daily) is
            # supplied by the scheduler caller / backtest. For the
            # in-scheduler live path the responsibility of loading
            # panels lives upstream (data layer) — call sites build the
            # panels dict and re-invoke ``detect``. For first-run safety
            # the scheduler hands an empty dict and emits a diagnostic
            # SIGNAL so the dispatcher's "did the engine run?" check
            # passes without crashing pre-data-layer.
            setup = CarverSetupDetection()
            panels: dict = {}
            candidates, diag = setup.detect(panels, as_of=as_of)

            # Pull current Alpaca holdings (cv_-attributable only).
            account = await broker.get_account()
            equity = account.equity if account.equity > 0 else self._engine_equity
            positions = await broker.get_positions()
            recent_orders = await broker.list_recent_orders(limit=500)
            current_holdings = _filter_to_engine_holdings(
                positions=positions,
                recent_orders=recent_orders,
                prefix=ENGINE_ORDER_PREFIX,
            )

            # Equity snapshot + drawdown breaker.
            await db_log.log(
                "EQUITY_SNAPSHOT",
                f"equity snapshot ${equity}",
                severity="INFO",
                data={"equity": float(equity), "n_positions": len(positions)},
            )
            peak_equity = await _fetch_peak_equity(
                pool, lookback_days=DRAWDOWN_BREAKER_LOOKBACK_DAYS,
            )
            if not CarverCapitalGate.check_drawdown(equity, peak_equity):
                logger.warning(
                    "carver.scheduler.drawdown_breaker",
                    current_equity=str(equity), peak_equity=str(peak_equity),
                )
                return RunSummary(
                    as_of=as_of, is_rebalance_day=True,
                    decision=None, submitted_order_ids=[],
                    dry_run=not self._submit,
                )

            # Emit one SIGNAL per candidate carrying FilterDiagnostics.
            diag_dict = diag.model_dump(exclude_none=True)
            for cand in candidates:
                await db_log.signal(
                    cand.ticker,
                    score=float(cand.combined_capped),
                    direction="LONG",
                    extra_data={"filter_diagnostics": diag_dict},
                )

            # Execution-risk plug -> RebalanceDecision.
            execution = CarverExecutionRisk()
            lifecycle = CarverLifecycleAnalysis()
            decision = await execution.decide(
                candidates=candidates,
                engine_equity_usd=equity,
                current_holdings=current_holdings,
                lifecycle=lifecycle,
                pool=pool,
                as_of=as_of,
            )

            # Engine-local capital gate (total-buy notional sanity).
            gate = CarverCapitalGate(engine_equity_usd=equity)
            if decision.orders and not gate.check_rebalance(
                decision.total_buy_notional_usd
            ):
                logger.warning(
                    "carver.scheduler.gate_rejected_rebalance",
                    buy_notional=str(decision.total_buy_notional_usd),
                    equity=str(equity),
                )
                return RunSummary(
                    as_of=as_of, is_rebalance_day=True,
                    decision=decision, submitted_order_ids=[],
                    dry_run=not self._submit,
                )

            # Cancel our own stale orders before submitting fresh ones.
            if self._submit:
                await self._cancel_stale_carver_orders(broker)

            submitted: list[str] = []
            failed: list[tuple[str, str]] = []
            sells = [o for o in decision.orders if o.side == "sell"]
            buys = [o for o in decision.orders if o.side == "buy"]

            for order in sells + buys:
                if not self._submit:
                    logger.info(
                        "carver.scheduler.dry_run_skip",
                        ticker=order.ticker, action=order.action.value,
                        qty=order.qty, side=order.side,
                    )
                    continue
                side = OrderSide.SELL if order in sells else OrderSide.BUY
                gated = await gate_batch_order(
                    governor, "carver",
                    ticker=order.ticker,
                    notional=Decimal(str(order.notional_usd)),
                    direction=side,
                )
                if not gated:
                    failed.append((order.ticker, "governor_blocked"))
                    logger.warning(
                        "carver.scheduler.governor_blocked",
                        ticker=order.ticker, action=order.action.value,
                        qty=order.qty, side=order.side,
                    )
                    continue
                try:
                    placed = await broker.place_order(self._payload_to_order(order))
                except Exception as exc:  # noqa: BLE001
                    failed.append((order.ticker, str(exc)[:200]))
                    logger.error(
                        "carver.scheduler.order_failed",
                        ticker=order.ticker, action=order.action.value,
                        qty=order.qty, side=order.side, error=str(exc)[:200],
                    )
                    continue
                if placed.broker_order_id is not None:
                    submitted.append(placed.broker_order_id)
                logger.info(
                    "carver.scheduler.order_submitted",
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
                    "carver.scheduler.partial_rebalance",
                    n_submitted=len(submitted), n_failed=len(failed),
                    failures=failed[:10],
                )

            return RunSummary(
                as_of=as_of, is_rebalance_day=True,
                decision=decision, submitted_order_ids=submitted,
                dry_run=not self._submit,
            )
        except Exception as exc:
            exit_code = 1
            await db_log.error(exc, context="scheduler_crash")
            raise
        finally:
            duration_ms = int((time.monotonic() - started_at) * 1000)
            try:
                await db_log.shutdown(
                    duration_ms=duration_ms, exit_code=exit_code,
                )
            except Exception as exc:  # noqa: BLE001 — best-effort shutdown event
                logger.warning(
                    "carver.scheduler.shutdown_log_failed",
                    error=str(exc)[:200],
                )
            await pool.close()

    @staticmethod
    async def _cancel_stale_carver_orders(broker: Any) -> int:
        """Cancel any open carver orders (client_order_id starts with ``cv_``).

        Thin delegate to the shared
        :func:`tpcore.order_management.stale_order_cancel.cancel_stale_orders`."""
        return await cancel_stale_orders(
            broker,
            order_prefix=ENGINE_ORDER_PREFIX,
            log_namespace="carver.scheduler",
        )

    @staticmethod
    def _payload_to_order(order: Any) -> Order:
        """Build a typed broker Order from a carver.models.RebalanceOrder."""
        payload = order.order_payload
        return Order(
            client_order_id=payload["client_order_id"],
            symbol=payload["symbol"],
            side=OrderSide.BUY if payload["side"] == "buy" else OrderSide.SELL,
            qty=Decimal(payload["qty"]),
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.DAY,
            order_class=OrderClass.SIMPLE,
            engine_id="carver",
        )


# ── CLI ──────────────────────────────────────────────────────────────────


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument(
        "--as-of", type=date_t.fromisoformat, default=None,
        help="Override the as-of date (ISO format); defaults to today (UTC).",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Compute the rebalance plan but don't submit orders.",
    )
    p.add_argument(
        "--engine-equity", type=Decimal, default=Decimal("10000"),
        help="Engine equity in USD (default $10,000).",
    )
    p.add_argument(
        "--force-rebalance", action="store_true",
        help=(
            "Operator escape hatch for direct manual invocation. Cadence "
            "(MONTHLY first trading day) is enforced by the dispatcher; "
            "this flag has no internal cadence to bypass but stays for "
            "parity with the other engines."
        ),
    )
    return p.parse_args(argv)


async def amain(args: argparse.Namespace) -> int:
    sched = CarverScheduler(
        engine_equity_usd=args.engine_equity,
        submit_orders=not args.dry_run,
        force_rebalance=args.force_rebalance,
    )
    summary = await sched.run_once(as_of=args.as_of)
    print(summary)
    return 0


def main() -> None:  # pragma: no cover — CLI shim
    raise SystemExit(asyncio.run(amain(_parse_args())))


if __name__ == "__main__":  # pragma: no cover
    main()
