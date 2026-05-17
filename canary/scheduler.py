"""Canary scheduler — daily 1-share SPY round-trip through the REAL
pipeline (RiskGovernor + batch_gate + broker + AAR). Sole purpose:
give DA-1 authentic STARTUP/SHUTDOWN liveness + the AAR/forensics/
allocator-skip/digest chain real daily data. Sentinel-shaped batch
day-market (no OCO → NOT in pipeline_smoke_test.py).

Cadence
-------
`is_trading_day` early-return satisfies compliance grep #4 (belt-and-
suspenders alongside the dispatcher). On a trading day: sell any prior
held SPY share (write one realized AAR), then buy 1 SPY.

Non-graduating by construction — no write_credibility_score anywhere
in this package (spec §4b; documented deviation from CLAUDE.md
engine-build compliance shortlist).
"""
from __future__ import annotations

import argparse
import asyncio
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from datetime import date as date_t
from decimal import Decimal
from typing import Any

import structlog

from canary.models import CANARY_QTY, CANARY_TICKER
from canary.plugs.aar_logging import CanaryAARLogging
from canary.plugs.capital_gate import CanaryCapitalGate
from canary.plugs.execution_risk import CanaryExecutionRisk
from canary.plugs.setup_detection import CanarySetupDetection
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
from tpcore.order_ids import ENGINE_PREFIX, build_cid, is_engine_cid
from tpcore.risk.batch_gate import gate_batch_order
from tpcore.risk.governor import RiskGovernor
from tpcore.risk.limits_profile import limits_for
from tpcore.risk.persistent_store import PostgresRiskStateStore

logger = structlog.get_logger(__name__)
_PREFIX = ENGINE_PREFIX["canary"]  # "ca_"


@dataclass
class _Components:
    """Injectable seam so tests isolate the heavy broker/pool wiring.

    ``db_log`` is the DBLogHandler for this run.  ``run_once`` creates
    the real handler, calls ``await db_log.startup()`` FIRST inside
    ``try:``, then passes the same object here so ``comp.db_log is
    db_log`` in production.  Tests patch ``_run_components`` and return
    the same fake that ``DBLogHandler`` already yielded.
    """
    db_log: Any
    price: Decimal
    prior_qty: int
    aar_write: Callable[[Any], Any]
    place: Callable[[Any], Any]
    governor: Any


async def _run_components(pool, broker, governor, db_log) -> _Components:
    """Production wiring: latest SPY close, prior canary SPY holding,
    real AARWriter + broker.place_order. db_log is threaded in from the
    caller (run_once) — startup() has already been awaited on it before
    this function is called; the same object is returned as comp.db_log
    so subsequent signal()/order_submitted() calls hit it correctly."""
    from tpcore.aar import (
        AARWriter,  # noqa: PLC0415 — lazy: _run_components is skipped entirely on non-trading days
    )
    writer = AARWriter(pool)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT close FROM platform.prices_daily WHERE ticker=$1 "
            "ORDER BY date DESC LIMIT 1",
            CANARY_TICKER,
        )
    price = Decimal(str(row["close"])) if row else Decimal("0")
    positions = await broker.get_positions()
    # Prior canary SPY holding: a SPY position the canary itself opened.
    # Position has no engine_id; cross-reference recent orders by the
    # canary client_order_id prefix — the sentinel idiom
    # (_filter_to_engine_holdings / is_engine_cid in sentinel/scheduler.py).
    try:
        recent = await broker.list_recent_orders(limit=500)
    except Exception:  # noqa: BLE001 — no recent-orders source ⇒ treat as flat
        recent = []
    canary_symbols = {
        o.symbol for o in recent
        if is_engine_cid(getattr(o, "client_order_id", None), "canary")
    }
    prior_qty = sum(
        int(p.qty) for p in positions
        if p.symbol == CANARY_TICKER and p.symbol in canary_symbols
    )
    return _Components(
        db_log=db_log,
        price=price,
        prior_qty=prior_qty,
        aar_write=writer.write_aar,
        place=broker.place_order,
        governor=governor,
    )


def _order(side: OrderSide) -> Order:
    return Order(
        client_order_id=build_cid("canary", CANARY_TICKER),
        symbol=CANARY_TICKER,
        side=side,
        qty=Decimal(CANARY_QTY),
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.DAY,
        order_class=OrderClass.SIMPLE,
        engine_id="canary",
    )


async def _cancel_stale_canary_orders(broker) -> int:
    """Cancel open canary orders (client_order_id starts with ``ca_``).
    Mirrors SentinelScheduler._cancel_stale_sentinel_orders.

    Returns the number of orders cancelled. Silently degrades when the
    broker doesn't expose ``list_recent_orders``.
    """
    list_fn = getattr(broker, "list_recent_orders", None)
    if list_fn is None:
        return 0
    try:
        recent = await list_fn(limit=500)
    except Exception as exc:  # noqa: BLE001
        logger.warning("canary.scheduler.list_orders_failed", error=str(exc)[:200])
        return 0
    open_statuses = {"new", "partially_filled", "accepted", "pending_new"}
    cancelled = 0
    for o in recent:
        cid = (o.client_order_id or "").lower()
        if not cid.startswith(_PREFIX):
            continue
        status_val = getattr(o.status, "value", str(o.status)).lower()
        if status_val not in open_statuses or not o.broker_order_id:
            continue
        try:
            await broker.cancel_order(o.broker_order_id)
            cancelled += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "canary.scheduler.cancel_failed",
                broker_order_id=o.broker_order_id,
                client_order_id=o.client_order_id,
                error=str(exc)[:200],
            )
    if cancelled:
        logger.info("canary.scheduler.stale_orders_cancelled", n=cancelled)
    return cancelled


async def run_once(
    as_of: date_t | None = None,
    dry_run: bool = False,
) -> dict[str, object]:
    as_of = as_of or datetime.now(UTC).date()
    as_of_dt = datetime.combine(as_of, datetime.min.time(), tzinfo=UTC)
    if not is_trading_day(as_of_dt):
        logger.info("canary.scheduler.non_trading_day", as_of=as_of.isoformat())
        return {"as_of": as_of.isoformat(), "action": "non_trading_day"}

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise RuntimeError("DATABASE_URL not set — cannot run canary.scheduler")

    pool = await build_asyncpg_pool(db_url)
    run_id = uuid.uuid4()
    db_log = DBLogHandler(pool=pool, engine="canary", run_id=run_id)
    started = datetime.now(UTC)
    exit_code = 0
    # comp is used in finally for shutdown; assign None so finally always safe.
    comp: _Components | None = None
    try:
        # FIRST awaited statement — mirrors sentinel/scheduler.py lines
        # 132-133.  Guarantees DA-1 gets a STARTUP row even if any
        # subsequent setup step (broker/governor/components) raises.
        await db_log.startup()
        broker = AlpacaPaperBrokerAdapter()
        state_store = PostgresRiskStateStore(pool=pool)
        governor = RiskGovernor(state_store=state_store, broker=broker, pool=pool)
        await governor.register_engine(
            "canary", Decimal("10000"), limits=limits_for("canary")
        )
        # Kill-switch pre-flight — same pattern as Sentinel/Momentum.
        risk_state = await governor.state_for("canary")
        if risk_state and risk_state.kill_switch_active:
            logger.critical(
                "canary.scheduler.kill_switch_active",
                as_of=as_of.isoformat(),
                reason=getattr(risk_state, "kill_switch_reason", None),
            )
            return {"as_of": as_of.isoformat(), "action": "kill_switch_halt"}

        # _run_components threads db_log in so comp.db_log IS db_log.
        # startup() has already been called above; the same object is
        # returned so all subsequent signal()/order_submitted() calls
        # use the live handler.
        comp = await _run_components(pool, broker, governor, db_log)

        if comp.price <= 0:
            logger.warning("canary.scheduler.no_price", as_of=as_of.isoformat())
            return {"as_of": as_of.isoformat(), "action": "no_price"}

        # Signal — canary always fires; carry FilterDiagnostics for audit.
        sd = CanarySetupDetection()
        _sig, diag = sd.detect()
        await comp.db_log.signal(
            CANARY_TICKER,
            score=1.0,
            direction="LONG",
            extra_data={"filter_diagnostics": diag.model_dump(exclude_none=True)},
        )

        # Capital gate — sanity check notional stays within tiny cap.
        gate = CanaryCapitalGate()
        decision = CanaryExecutionRisk().decide(price=comp.price)
        if not gate.check_trade(
            size=decision.notional_usd, engine_pnl=Decimal("0"), open_positions=0
        ):
            return {"as_of": as_of.isoformat(), "action": "gate_rejected"}

        # Cancel any stale open canary orders before placing new ones.
        await _cancel_stale_canary_orders(broker)

        # SELL the prior held share (realize one AAR) then BUY 1 SPY.
        if comp.prior_qty > 0:
            if dry_run:
                logger.info(
                    "canary.scheduler.dry_run_skip",
                    side="sell",
                    ticker=CANARY_TICKER,
                )
            elif await gate_batch_order(
                comp.governor,
                "canary",
                ticker=CANARY_TICKER,
                notional=decision.notional_usd,
                direction=OrderSide.SELL,
            ):
                await comp.place(_order(OrderSide.SELL))
                await comp.governor.record_fill(
                    engine_id="canary",
                    realized_pnl=Decimal("0"),
                    position_delta=-1,
                )
                aar = CanaryAARLogging().build_aar(
                    trade_id=build_cid("canary", CANARY_TICKER),
                    entry_ts_iso=started.isoformat(),
                    exit_ts_iso=datetime.now(UTC).isoformat(),
                    entry_price=comp.price,
                    exit_price=comp.price,
                    qty=Decimal(CANARY_QTY),
                    engine_equity_usd=Decimal("10000"),
                )
                await comp.aar_write(aar)

        if dry_run:
            logger.info(
                "canary.scheduler.dry_run_skip",
                side="buy",
                ticker=CANARY_TICKER,
            )
        elif await gate_batch_order(
            comp.governor,
            "canary",
            ticker=CANARY_TICKER,
            notional=decision.notional_usd,
            direction=OrderSide.BUY,
        ):
            await comp.place(_order(OrderSide.BUY))

        return {"as_of": as_of.isoformat(), "action": "round_trip"}

    except Exception:
        exit_code = 1
        raise
    finally:
        duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
        _log = comp.db_log if comp is not None else db_log
        try:
            await _log.shutdown(duration_ms=duration_ms, exit_code=exit_code)
        except Exception as exc:  # noqa: BLE001 — best-effort shutdown event
            logger.warning(
                "canary.scheduler.shutdown_log_failed", error=str(exc)[:200]
            )
        await pool.close()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--as-of", type=date_t.fromisoformat, default=None)
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args(argv)


async def amain(args: argparse.Namespace) -> int:
    summary = await run_once(as_of=args.as_of, dry_run=args.dry_run)
    print(summary)
    return 0


def main() -> None:  # pragma: no cover — CLI shim
    raise SystemExit(asyncio.run(amain(_parse_args())))


if __name__ == "__main__":  # pragma: no cover
    main()
