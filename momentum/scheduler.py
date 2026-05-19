"""Momentum scheduler — daily entry point.

Wires the five Momentum plugs + Alpaca broker + Risk Governor + Postgres
into a single :meth:`MomentumScheduler.run_once` invocation that an external
scheduler (cron, systemd timer, manual ``python -m momentum.scheduler``) can
call.

Cadence
-------
Momentum rebalances *monthly*, on the first trading day of each calendar
month. That cadence boundary is enforced **exactly once** — by the
Python dispatcher (``ops/engine_dispatch.py``) via
``tpcore.engine_profile.should_fire`` (momentum profile =
``MONTHLY_FIRST_TRADING_DAY``). The scheduler itself NO LONGER carries
an internal cadence gate (the old ``lifecycle.assess`` /
``plan.is_rebalance_day`` early-return was redundant double-gating and
was deleted on 2026-05-17). If ``run_once`` is invoked at all, it
rebalances. Direct manual ``python -m momentum.scheduler`` runs are an
operator decision — ``--force-rebalance`` is kept as the documented
escape-hatch flag for those (it now has no internal cadence to bypass).

Responsibilities each run
-------------------------
1. Build asyncpg pool + Alpaca broker + Risk Governor.
2. Setup plug ranks the universe → list of candidates.
3. Pull current Alpaca portfolio.
4. Execution-Risk plug builds the target portfolio + order batch.
5. Capital gate sanity-checks total buy notional vs allocated equity.
6. Submit each market order via the broker, in this order:
   all SELLs first (free up cash) → all BUYs (deploy it).
7. Log each submitted order to ``platform.application_log``.

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
import time
import uuid
from datetime import UTC, datetime
from datetime import date as date_t
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import structlog

from momentum.models import RebalanceDecision
from momentum.plugs.capital_gate import (
    DRAWDOWN_BREAKER_LOOKBACK_DAYS,
    MomentumCapitalGate,
)
from momentum.plugs.execution_risk import MomentumExecutionRisk
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
from tpcore.order_ids import build_close_id
from tpcore.order_management.stale_order_cancel import cancel_stale_orders
from tpcore.risk.batch_gate import gate_batch_order
from tpcore.risk.governor import RiskGovernor
from tpcore.risk.limits_profile import limits_for
from tpcore.risk.persistent_store import PostgresRiskStateStore

if TYPE_CHECKING:  # pragma: no cover
    pass

logger = structlog.get_logger(__name__)

# Every momentum order carries this client_order_id prefix (stamped in
# ``momentum.plugs.execution_risk``). Used to attribute account positions
# back to momentum so the rebalance only diffs against its own book —
# never against sigma/reversion/vector holdings.
ENGINE_ORDER_PREFIX = "mo_"


def _filter_to_engine_holdings(
    positions: Any,
    recent_orders: Any,
    prefix: str,
) -> dict[str, int]:
    """Filter broker positions to those originated by this engine.

    A position is "ours" iff at least one recent order on that symbol
    is attributable to this engine per :func:`tpcore.order_ids.is_engine_cid`.
    Sub-account isolation isn't available at the broker, so this
    client-side attribution is the contract that keeps engines from
    stepping on each other (see the 2026-05-14 YUMC incident).

    The ``prefix`` arg is kept for backward compatibility but the
    attribution check now uses the central registry — passing ``mo_``
    targets the ``momentum`` engine specifically, including its legacy
    forms if any are ever added to the registry.

    Pure function so the rebalance scheduler can be regression-tested
    without spinning a real broker. Returns ``{symbol: qty}`` for
    positions with qty > 0.
    """
    from tpcore.order_ids import ENGINE_PREFIX, is_engine_cid

    # Reverse-lookup engine from the prefix arg so callers don't have to
    # change. Defaults to legacy prefix-startswith if the registry has no
    # match — preserves current behavior for any future caller.
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
        # Unknown prefix — fall back to literal startswith.
        engine_symbols = {
            o.symbol for o in recent_orders
            if (getattr(o, "client_order_id", None) or "").startswith(prefix)
        }
    return {
        p.symbol: int(p.qty)
        for p in positions
        if int(p.qty) > 0 and p.symbol in engine_symbols
    }


async def _fetch_peak_equity(pool, *, lookback_days: int) -> float | None:
    """Read the highest EQUITY_SNAPSHOT for momentum in the lookback window.

    Returns None when no snapshots are on record (first run / fresh DB) —
    callers should treat that as 'no peak yet, no breaker'."""
    sql = """
        SELECT data
        FROM platform.application_log
        WHERE engine = 'momentum'
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
        # STARTUP / SHUTDOWN bookend the run. tpcore.engine_profile.should_fire
        # keys its "already ran this cadence window" idempotency off a STARTUP
        # row in platform.application_log — without these the event-driven
        # dispatcher (Sub-project B) would re-fire a MONTHLY engine on every
        # readiness event in its first-trading-day window. Mirrors the
        # reversion/vector/sentinel schedulers' bookend idiom exactly.
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
            # Register momentum with the governor so check_trade has a
            # RiskState row + the basket-sized position cap (limits_for
            # gives momentum max_open_positions=200; the global 8-pos
            # default would otherwise block the whole decile). Idempotent —
            # won't clobber an existing state, only (re)records limits.
            await governor.register_engine(
                "momentum",
                self._engine_equity,
                limits=limits_for("momentum"),
            )

            # Kill-switch pre-flight (F3 of 2026-05-14 audit). Mirrors
            # the sigma/reversion/vector pattern so a platform-wide
            # emergency kill halts Momentum's monthly rebalance too.
            # Momentum's own gates (MomentumCapitalGate.check_drawdown +
            # check_rebalance) are engine-local — they can't see this
            # flag. Without this guard, the operator's mental model
            # "kill switch halts all engines" was false for Momentum.
            current_state = await governor.state_for("momentum")
            if current_state and current_state.kill_switch_active:
                logger.critical(
                    "momentum.scheduler.kill_switch_active",
                    as_of=as_of.isoformat(),
                    reason=current_state.kill_switch_reason,
                )
                return RunSummary(
                    as_of=as_of, is_rebalance_day=False,
                    decision=None, submitted_order_ids=[], dry_run=not self._submit,
                )

            # Cadence is NOT gated here. The MONTHLY_FIRST_TRADING_DAY
            # boundary is enforced exactly once — by the Python dispatcher
            # (ops/engine_dispatch.py) via tpcore.engine_profile.should_fire
            # (operator directive 2026-05-17, event-driven engine services).
            # The old in-scheduler lifecycle.assess / plan.is_rebalance_day
            # early-return was redundant double-gating and has been deleted
            # so engine_profile is the SOLE cadence authority. If this
            # scheduler is invoked at all, it rebalances. --force-rebalance
            # remains for direct manual `python -m momentum.scheduler` runs
            # (it now has no internal cadence to bypass, but stays a
            # documented accepted operator escape hatch).

            # Plug 1 — rank candidates.
            setup = MomentumSetupDetection()
            candidates = await setup.scan(pool, as_of)

            # Pull current Alpaca holdings.
            account = await broker.get_account()
            equity = account.equity if account.equity > 0 else self._engine_equity
            positions = await broker.get_positions()
            # Filter to momentum-owned positions only. Without this filter
            # the rebalance diffs against the WHOLE account and emits sell
            # orders for any non-momentum position whose ticker isn't in
            # today's target list — i.e., it'd liquidate sigma/reversion/
            # vector's holdings (the YUMC tier1 incident, 2026-05-14).
            # Attribution: a position is ours iff a recent order on that
            # symbol carries the ``mo_`` client_order_id prefix.
            recent_orders = await broker.list_recent_orders(limit=500)
            current_holdings = _filter_to_engine_holdings(
                positions=positions,
                recent_orders=recent_orders,
                prefix=ENGINE_ORDER_PREFIX,
            )

            # Snapshot equity to platform.application_log + check drawdown
            # circuit breaker. Done BEFORE setup/execution so a tripped
            # breaker short-circuits the whole rebalance cleanly.
            await db_log.log(
                "EQUITY_SNAPSHOT",
                f"equity snapshot ${equity}",
                severity="INFO",
                data={"equity": float(equity), "n_positions": len(positions)},
            )
            peak_equity = await _fetch_peak_equity(pool, lookback_days=DRAWDOWN_BREAKER_LOOKBACK_DAYS)
            if not MomentumCapitalGate.check_drawdown(equity, peak_equity):
                logger.warning(
                    "momentum.scheduler.drawdown_breaker",
                    current_equity=str(equity), peak_equity=str(peak_equity),
                )
                return RunSummary(
                    as_of=as_of, is_rebalance_day=True,
                    decision=None, submitted_order_ids=[], dry_run=not self._submit,
                )

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
            # done. Each signal carries the scan-time FilterDiagnostics as
            # extra_data — same instance attached to every candidate by
            # MomentumSetupDetection.scan(), so we lift it off the first
            # candidate and pass to every signal.
            _scan_diag = (
                candidates[0].filter_diagnostics if candidates
                and candidates[0].filter_diagnostics is not None else None
            )
            _diag_dict = (
                _scan_diag.model_dump(exclude_none=True) if _scan_diag is not None else None
            )
            for tgt in decision.targets:
                await db_log.signal(
                    tgt.ticker, score=float(tgt.momentum_score), direction="LONG",
                    extra_data=({"filter_diagnostics": _diag_dict} if _diag_dict else None),
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
                # Every submitted name passes the shared batch gate so the
                # RiskGovernor (loss caps, kill switch, position cap,
                # exposure) is enforced per order — not just the global
                # kill-switch pre-flight above. A BLOCKed name is skipped;
                # the rest of the rebalance still proceeds.
                side = OrderSide.SELL if order in sells else OrderSide.BUY
                gated = await gate_batch_order(
                    governor, "momentum",
                    ticker=order.ticker,
                    notional=Decimal(str(order.notional_usd)),
                    direction=side,
                )
                if not gated:
                    failed.append((order.ticker, "governor_blocked"))
                    logger.warning(
                        "momentum.scheduler.governor_blocked",
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
                if side is OrderSide.SELL:
                    # Free the governor slot via the idempotent close
                    # arbiter (#251 B1). The trade-monitor stream may also
                    # record this same close; both funnel through
                    # record_close keyed by the stable per-(engine,ticker,
                    # rebalance-date) close-id so the risk_close_ledger PK
                    # ensures the slot decrements AT MOST once — never the
                    # old dual-decrement under-drift. Realized P&L stays
                    # 0 here (reconciled via the AAR/trade_monitor path;
                    # adding it here would double-count). A record_close
                    # error must NOT abort the rebalance loop.
                    try:
                        await governor.record_close(
                            engine_id="momentum",
                            trade_id=build_close_id("momentum", order.ticker, as_of),
                            realized_pnl=Decimal("0"),
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.error(
                            "momentum.scheduler.record_close_failed",
                            ticker=order.ticker, error=str(exc)[:200],
                        )
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
        except Exception as exc:
            exit_code = 1
            await db_log.error(exc, context="scheduler_crash")
            raise
        finally:
            # SHUTDOWN must always fire — including the no-rebalance /
            # kill-switch / drawdown early-return paths and the exception
            # path — so the dispatcher's idempotency sees the cycle closed.
            # Order matters: log SHUTDOWN (needs the pool) THEN close it.
            duration_ms = int((time.monotonic() - started_at) * 1000)
            await db_log.shutdown(duration_ms=duration_ms, exit_code=exit_code)
            await pool.close()

    @staticmethod
    async def _cancel_stale_momentum_orders(broker) -> int:
        """Cancel any open orders we own (client_order_id starts with ``mo_``)
        so positions held_for_orders are released before the new rebalance.

        Thin delegate to the shared
        :func:`tpcore.order_management.stale_order_cancel.cancel_stale_orders`
        (Lean P5 #1) — behavior (cancelled-ID set, count, structlog event
        names) is byte-equivalent to the prior inlined implementation.
        Returns the number of orders cancelled. Silently degrades when the
        broker doesn't expose ``list_recent_orders`` (non-Alpaca brokers)."""
        return await cancel_stale_orders(
            broker,
            order_prefix=ENGINE_ORDER_PREFIX,
            log_namespace="momentum.scheduler",
        )

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
            "Operator escape hatch for direct manual `python -m "
            "momentum.scheduler` invocation. Cadence (MONTHLY first trading "
            "day) is enforced by the dispatcher via tpcore.engine_profile — "
            "the scheduler no longer has an internal cadence gate to bypass. "
            "This flag remains an accepted, documented no-op-compatible flag "
            "for manual runs and parity with the other engines."
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
