"""Catalyst scheduler — daily entry point (insider-cluster swing engine).

Composes the 5 plugs + Alpaca broker + Risk Governor + Postgres logging
into a single :func:`run_once` invocation the ``engine-service`` daemon
fires on the ``DATA_OPERATIONS_COMPLETE`` event.

Cadence
-------
DAILY trading-day boundary. The Python dispatcher
(``ops/engine_dispatch.py``) gates cadence via
:func:`tpcore.engine_profile.should_fire`. We keep the
``is_trading_day`` early-return in the scheduler too (belt-and-suspenders
per compliance grep #4 — same pattern as canary/sentinel).

Live trading path
-----------------
The scheduler never imports ``catalyst.backtest`` (the Lab seam lives
there). The live path reads its config from module-level constants —
``catalyst.models`` and ``catalyst.plugs.*`` only.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from datetime import date as date_t
from decimal import Decimal

import pandas as pd
import structlog

from catalyst.models import (
    CATALYST_CLUSTER_WINDOW_DAYS,
    CATALYST_TEST_UNIVERSE,
    SMA_TREND_PERIOD,
)
from catalyst.plugs.aar_logging import CatalystAARLogging  # noqa: F401 — readiness wiring
from catalyst.plugs.capital_gate import CatalystCapitalGate
from catalyst.plugs.execution_risk import CatalystExecutionRisk
from catalyst.plugs.lifecycle_analysis import CatalystLifecycleAnalysis  # noqa: F401 — readiness wiring
from catalyst.plugs.setup_detection import CatalystSetupDetection
from tpcore.alpaca import AlpacaPaperBrokerAdapter
from tpcore.calendar import is_trading_day
from tpcore.data.repositories import InsiderRepo, PricesRepo
from tpcore.db import build_asyncpg_pool
from tpcore.identity.dispatcher import IdentityDispatcher
from tpcore.interfaces.broker import (
    Order,
    OrderClass,
    OrderSide,
    OrderType,
    TimeInForce,
)
from tpcore.logging import DBLogHandler
from tpcore.order_ids import ENGINE_PREFIX
from tpcore.order_management.stale_order_cancel import cancel_stale_orders
from tpcore.risk.governor import RiskGovernor
from tpcore.risk.limits_profile import limits_for
from tpcore.risk.persistent_store import PostgresRiskStateStore

logger = structlog.get_logger(__name__)

_PREFIX = ENGINE_PREFIX["catalyst"]  # "ct_"
_PRICE_LOOKBACK_DAYS = SMA_TREND_PERIOD + 30  # enough bars for the 50-SMA + headroom
_INSIDER_LOOKBACK_DAYS = CATALYST_CLUSTER_WINDOW_DAYS + 5  # small headroom


async def _fetch_insider_rows(
    pool,
    *,
    universe: Iterable[str],
    start: date_t,
    end: date_t,
) -> pd.DataFrame:
    """Pull Form-4 insider rows for ``universe`` over ``[start, end]``.

    Returns a DataFrame with columns
    ``{ticker, filing_date, insider_name, transaction_type, value}``.
    Empty DataFrame on no rows (the caller still emits the SIGNAL with
    a zero-candidate FilterDiagnostics).
    """
    dispatcher = IdentityDispatcher(pool)
    repo = InsiderRepo(pool)

    cid_to_ticker: dict[str, str] = {}
    for t in universe:
        cid = await dispatcher.ticker_to_classification_id(t)
        if cid is not None:
            cid_to_ticker[cid] = t

    if not cid_to_ticker:
        return pd.DataFrame(columns=["ticker", "filing_date", "insider_name", "transaction_type", "value"])

    txns_by_cid = await repo.get_window_batch(list(cid_to_ticker), start, end)
    rows: list[dict] = []
    for cid, txns in txns_by_cid.items():
        ticker = cid_to_ticker[cid]
        for txn in txns:
            rows.append(
                {
                    "ticker": ticker,
                    "filing_date": txn.filing_date,
                    "insider_name": txn.insider_name,
                    "transaction_type": txn.transaction_type,
                    "value": float(txn.value),
                }
            )
    if not rows:
        return pd.DataFrame(columns=["ticker", "filing_date", "insider_name", "transaction_type", "value"])
    return pd.DataFrame(rows)


async def _fetch_prices(
    pool,
    *,
    universe: Iterable[str],
    start: date_t,
    end: date_t,
) -> dict[str, pd.DataFrame]:
    """Pull per-ticker close+volume panels over ``[start, end]``.

    Edge adapter: ticker universe in, ticker-keyed DataFrame out.
    Dispatches ticker → classification_id and queries PricesRepo by
    classification_id (post-v2.2 prices_daily.classification_id column
    is 100% populated).
    """
    dispatcher = IdentityDispatcher(pool)
    repo = PricesRepo(pool)

    cid_to_ticker: dict[str, str] = {}
    for t in universe:
        cid = await dispatcher.ticker_to_classification_id(t)
        if cid is not None:
            cid_to_ticker[cid] = t

    if not cid_to_ticker:
        return {}

    bars_by_cid = await repo.get_window_batch(list(cid_to_ticker), start, end)
    out: dict[str, pd.DataFrame] = {}
    for cid, bars in bars_by_cid.items():
        if not bars:
            continue
        ticker = cid_to_ticker[cid]
        sorted_bars = sorted(bars, key=lambda b: b.date)
        idx = pd.DatetimeIndex([pd.Timestamp(b.date) for b in sorted_bars])
        out[ticker] = pd.DataFrame(
            {
                "close": [float(b.close) for b in sorted_bars],
                "volume": [b.volume for b in sorted_bars],
            },
            index=idx,
        )
    return out


def _build_bracket_order(payload: dict) -> Order:
    """Map the execution-risk plug's payload dict to a typed broker Order."""
    return Order(
        client_order_id=payload["client_order_id"],
        symbol=payload["symbol"],
        side=OrderSide.BUY,
        qty=Decimal(payload["qty"]),
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.DAY,
        order_class=OrderClass.BRACKET,
        engine_id="catalyst",
        take_profit_limit_price=Decimal(payload["take_profit"]["limit_price"]),
        stop_loss_stop_price=Decimal(payload["stop_loss"]["stop_price"]),
    )


async def _cancel_stale_catalyst_orders(broker) -> int:
    """Cancel any open catalyst broker orders (client_order_id ``ct_``).

    Lean P5.4b: delegates to the shared
    ``tpcore.order_management.stale_order_cancel.cancel_stale_orders``,
    same as canary/sentinel."""
    return await cancel_stale_orders(
        broker,
        order_prefix=_PREFIX,
        log_namespace="catalyst.scheduler",
    )


async def run_once(
    as_of: date_t | None = None,
    *,
    dry_run: bool = False,
    platform_equity_usd: Decimal = Decimal("100000"),
) -> dict[str, object]:
    """Single-pass daily run of the catalyst engine. Idempotent."""
    as_of = as_of or datetime.now(UTC).date()
    as_of_dt = datetime.combine(as_of, datetime.min.time(), tzinfo=UTC)
    if not is_trading_day(as_of_dt):
        logger.info("catalyst.scheduler.non_trading_day", as_of=as_of.isoformat())
        return {"as_of": as_of.isoformat(), "action": "non_trading_day"}

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise RuntimeError("DATABASE_URL not set — cannot run catalyst.scheduler")

    pool = await build_asyncpg_pool(db_url)
    run_id = uuid.uuid4()
    db_log = DBLogHandler(pool=pool, engine="catalyst", run_id=run_id)
    started = datetime.now(UTC)
    exit_code = 0
    try:
        await db_log.startup()
        broker = AlpacaPaperBrokerAdapter()
        state_store = PostgresRiskStateStore(pool=pool)
        governor = RiskGovernor(state_store=state_store, broker=broker, pool=pool)
        await governor.register_engine(
            "catalyst",
            platform_equity_usd,
            limits=limits_for("catalyst"),
        )

        # Kill-switch pre-flight — same pattern as sentinel/canary.
        risk_state = await governor.state_for("catalyst")
        if risk_state and risk_state.kill_switch_active:
            logger.critical(
                "catalyst.scheduler.kill_switch_active",
                as_of=as_of.isoformat(),
                reason=getattr(risk_state, "kill_switch_reason", None),
            )
            return {"as_of": as_of.isoformat(), "action": "kill_switch_halt"}

        universe = CATALYST_TEST_UNIVERSE
        insider_rows = await _fetch_insider_rows(
            pool,
            universe=universe,
            start=as_of - timedelta(days=_INSIDER_LOOKBACK_DAYS),
            end=as_of,
        )
        prices_by_ticker = await _fetch_prices(
            pool,
            universe=universe,
            start=as_of - timedelta(days=_PRICE_LOOKBACK_DAYS),
            end=as_of,
        )

        setup = CatalystSetupDetection()
        candidates, diag = setup.detect(
            as_of=as_of,
            universe=universe,
            insider_rows=insider_rows,
            prices_by_ticker=prices_by_ticker,
        )

        # Lift FilterDiagnostics onto every SIGNAL event so the dashboard
        # can render per-gate pass/block counters (compliance grep #2).
        diag_dict = diag.model_dump(exclude_none=True)
        for cand in candidates:
            await db_log.signal(
                cand.ticker,
                score=float(cand.cluster_density),
                direction="LONG",
                extra_data={
                    "filter_diagnostics": diag_dict,
                    "cluster_distinct_insiders": cand.cluster.distinct_insiders,
                    "cluster_aggregate_usd": str(cand.cluster.aggregate_value_usd),
                },
            )
        if not candidates:
            # Still emit ONE SIGNAL row carrying the diagnostics so the
            # operator can see "why no setups today?" — mirrors momentum.
            await db_log.signal(
                "_NONE",
                score=0.0,
                direction="NONE",
                extra_data={"filter_diagnostics": diag_dict},
            )
            return {"as_of": as_of.isoformat(), "action": "no_candidates", "filter_diagnostics": diag_dict}

        # Engine-local capital gate per candidate.
        cap_gate = CatalystCapitalGate(engine_equity=platform_equity_usd)
        execution = CatalystExecutionRisk()

        if not dry_run:
            await _cancel_stale_catalyst_orders(broker)

        submitted: list[str] = []
        blocked: list[tuple[str, str]] = []
        for cand in sorted(candidates, key=lambda c: c.cluster_density, reverse=True):
            decision = execution.decide(cand, engine_equity_usd=platform_equity_usd)
            if decision is None:
                blocked.append((cand.ticker, "sizing_zero"))
                continue
            if not cap_gate.check_trade(
                size=decision.notional_usd,
                engine_pnl=Decimal("0"),
            ):
                blocked.append((cand.ticker, "capital_gate"))
                continue
            check = await governor.check_trade(
                "catalyst",
                decision.notional_usd,
                OrderSide.BUY,
                ticker=cand.ticker,
            )
            if not check.allowed:
                blocked.append((cand.ticker, f"governor:{check.reason}"))
                logger.warning("catalyst.scheduler.governor_blocked", ticker=cand.ticker, reason=check.reason)
                continue
            if dry_run:
                logger.info("catalyst.scheduler.dry_run_skip", ticker=cand.ticker, qty=decision.qty)
                submitted.append(cand.ticker)
                continue
            order = _build_bracket_order(decision.order_payloads[0])
            try:
                placed = await broker.place_order(order)
            except Exception as exc:  # noqa: BLE001
                blocked.append((cand.ticker, f"order_failed:{exc!s}"[:200]))
                logger.error("catalyst.scheduler.order_failed", ticker=cand.ticker, error=str(exc)[:200])
                continue
            await governor.record_fill(
                engine_id="catalyst",
                realized_pnl=Decimal("0"),
                position_delta=1,
            )
            await db_log.order_submitted(
                cand.ticker,
                quantity=decision.qty,
                order_id=placed.broker_order_id,
            )
            if placed.broker_order_id:
                submitted.append(placed.broker_order_id)

        return {
            "as_of": as_of.isoformat(),
            "action": "scanned",
            "submitted": submitted,
            "blocked": blocked,
            "candidates": [c.ticker for c in candidates],
            "filter_diagnostics": diag_dict,
        }
    except Exception:
        exit_code = 1
        raise
    finally:
        duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
        try:
            await db_log.shutdown(duration_ms=duration_ms, exit_code=exit_code)
        except Exception as exc:  # noqa: BLE001 — best-effort shutdown event
            logger.warning("catalyst.scheduler.shutdown_log_failed", error=str(exc)[:200])
        await pool.close()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--as-of", type=date_t.fromisoformat, default=None)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--platform-equity", type=Decimal, default=Decimal("100000"))
    return p.parse_args(argv)


async def amain(args: argparse.Namespace) -> int:
    summary = await run_once(
        as_of=args.as_of,
        dry_run=args.dry_run,
        platform_equity_usd=args.platform_equity,
    )
    print(summary)
    return 0


def main() -> None:  # pragma: no cover — CLI shim
    raise SystemExit(asyncio.run(amain(_parse_args())))


if __name__ == "__main__":  # pragma: no cover
    main()
