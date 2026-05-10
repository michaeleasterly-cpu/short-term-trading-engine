"""Vector scheduler — daily cron entry point.

MVP scope: scan the universe for candidates, log them, submit Phase.ENTRY
candidates as Alpaca paper bracket orders, ping Healthchecks. Open-position
reconciliation, AAR persistence on close, and trail-stop re-evaluation are
TODO for the next iteration — Sigma's order_manager has the full shape and
Vector will mirror it once paper-trade volume justifies the lift.

Calling cadence: daily, weekday-only, Mon–Fri 22:00 UTC (see ``railway.json``).

Required env:
    DATABASE_URL                — Postgres URL for prices + fundamentals.
    ALPACA_KEY / ALPACA_SECRET  — paper credentials.
    HEALTHCHECKS_VECTOR_URL     — optional; success / start / fail pings.
    VECTOR_ENGINE_EQUITY        — optional; default 10000.
"""
from __future__ import annotations

import asyncio
import os
from collections import defaultdict
from datetime import UTC, date as date_t, datetime
from decimal import Decimal
from typing import Any

import httpx
import pandas as pd
import structlog

from tpcore.aar.writer import AARWriter
from tpcore.alpaca import AlpacaPaperBrokerAdapter
from tpcore.db import build_asyncpg_pool
from tpcore.fmp import FMPFundamentalsAdapter
from tpcore.fundamentals.cache import FundamentalsCache
from tpcore.outage import DataProviderOutage
from tpcore.parity import LivePaperParityHarness
from tpcore.risk.governor import RiskGovernor
from tpcore.risk.persistent_store import PostgresRiskStateStore

from vector.models import VECTOR_TEST_UNIVERSE, Phase
from vector.order_manager import VectorOrderManager
from vector.plugs.aar_logging import VectorAARLogging
from vector.plugs.capital_gate import VectorCapitalGate
from vector.plugs.execution_risk import VectorExecutionRisk
from vector.plugs.lifecycle_analysis import VectorLifecycleAnalysis
from vector.plugs.setup_detection import VectorSetupDetection

logger = structlog.get_logger(__name__)

ENGINE_ID = "vector"
SPY_SYMBOL = "SPY"
LOOKBACK_DAYS = 260  # enough for 200-SMA + headroom
_HEALTHCHECKS_ENV = "HEALTHCHECKS_VECTOR_URL"


class RunSummary:
    def __init__(
        self,
        *,
        as_of: date_t,
        n_candidates: int,
        n_submitted: int,
    ) -> None:
        self.as_of = as_of
        self.n_candidates = n_candidates
        self.n_submitted = n_submitted

    def __repr__(self) -> str:
        return (
            f"RunSummary(as_of={self.as_of}, n_candidates={self.n_candidates}, "
            f"n_submitted={self.n_submitted})"
        )


async def _load_bars(
    pool, tickers: tuple[str, ...], lookback_end: date_t
) -> dict[str, pd.DataFrame]:
    """Pull the last LOOKBACK_DAYS sessions for each ticker."""
    sql = """
        SELECT ticker, date, open, high, low, close, volume
        FROM platform.prices_daily
        WHERE ticker = ANY($1) AND date <= $2
        ORDER BY ticker, date
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, list(tickers), lookback_end)
    by_ticker: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_ticker[r["ticker"]].append(
            {
                "date": r["date"],
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "volume": int(r["volume"]),
            }
        )
    out: dict[str, pd.DataFrame] = {}
    for ticker, ticker_rows in by_ticker.items():
        df = pd.DataFrame(ticker_rows).set_index("date").sort_index()
        out[ticker] = df.tail(LOOKBACK_DAYS)
    return out


async def _load_fundamentals(
    pool, tickers: tuple[str, ...], as_of: date_t
) -> dict[str, dict[str, Any] | None]:
    """Latest cached fundamentals snapshot per ticker, PIT-filtered to ``as_of``."""
    fmp = None
    try:
        fmp = FMPFundamentalsAdapter()
    except DataProviderOutage:
        # No FMP_API_KEY — operate on cache only.
        pass
    cache = FundamentalsCache(pool, adapter=fmp)
    out: dict[str, dict[str, Any] | None] = {}
    for ticker in tickers:
        try:
            out[ticker] = await cache.get_quarterly_fundamentals(ticker, as_of_date=as_of)
        except Exception as exc:  # pragma: no cover - cache miss / no data
            logger.warning("vector.scheduler.fundamentals_miss", ticker=ticker, error=str(exc))
            out[ticker] = None
    if fmp is not None:
        await fmp.aclose()
    return out


async def _ping_healthcheck(suffix: str = "") -> None:
    url = os.getenv(_HEALTHCHECKS_ENV)
    if not url:
        return
    target = url.rstrip("/") + suffix
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.get(target)
    except Exception as exc:  # pragma: no cover
        logger.warning("vector.scheduler.healthcheck_ping_failed", suffix=suffix, error=str(exc))


class VectorScheduler:
    """One-shot orchestration of a Vector daily scan + bracket-order submission."""

    def __init__(
        self,
        *,
        engine_equity: Decimal = Decimal("10000"),
        database_url: str | None = None,
        broker: AlpacaPaperBrokerAdapter | None = None,
    ) -> None:
        self._engine_equity = engine_equity
        self._database_url = database_url if database_url is not None else os.getenv("DATABASE_URL")
        self._injected_broker = broker

    async def run_once(self, *, as_of: date_t | None = None) -> RunSummary:
        as_of = as_of or datetime.now(UTC).date()
        if not self._database_url:
            logger.warning("vector.scheduler.no_database_url")
            return RunSummary(as_of=as_of, n_candidates=0, n_submitted=0)

        pool = await build_asyncpg_pool(self._database_url)
        broker = self._injected_broker or AlpacaPaperBrokerAdapter()
        try:
            # Wire risk governor + AAR writer + (optional) parity harness.
            governor = RiskGovernor(
                state_store=PostgresRiskStateStore(pool),
                broker=broker,
                platform_capital=self._engine_equity,
            )
            await governor.register_engine(ENGINE_ID, self._engine_equity)

            # Kill-switch short-circuit: refuse to scan or submit when frozen.
            current_state = await governor._store.get(ENGINE_ID)  # noqa: SLF001 — read-only peek
            if current_state and current_state.kill_switch_active:
                logger.critical(
                    "vector.scheduler.kill_switch_active",
                    engine=ENGINE_ID,
                    reason=current_state.kill_switch_reason or "unspecified",
                )
                return RunSummary(as_of=as_of, n_candidates=0, n_submitted=0)

            aar_writer = AARWriter(pool)
            parity = self._build_parity_harness(pool, paper_broker=broker)

            order_manager = VectorOrderManager(
                broker=broker,
                governor=governor,
                capital_gate=VectorCapitalGate(engine_equity=self._engine_equity),
                lifecycle=VectorLifecycleAnalysis(),
                aar=VectorAARLogging(),
                aar_writer=aar_writer,
                parity_harness=parity,
            )

            # Reconcile any open trades first so the position counter is fresh.
            new_aars = await order_manager.reconcile(
                sizing_pct_of_engine_equity=Decimal("0.20"),
            )

            tickers = VECTOR_TEST_UNIVERSE + (SPY_SYMBOL,)
            bars = await _load_bars(pool, tickers, as_of)
            spy_panel = bars.pop(SPY_SYMBOL, None)
            fundamentals = await _load_fundamentals(pool, VECTOR_TEST_UNIVERSE, as_of)

            setup = VectorSetupDetection()
            lifecycle = VectorLifecycleAnalysis()
            execution = VectorExecutionRisk()

            candidates = setup.scan(
                as_of=as_of,
                bars_by_ticker=bars,
                fundamentals_by_ticker=fundamentals,
                spy_panel=spy_panel,
                vix_value=None,  # MVP — VIX feed deferred; ExecutionRisk treats None as low-VIX
            )
            logger.info("vector.scheduler.scan_done", n_candidates=len(candidates), as_of=str(as_of))

            account = await broker.get_account()
            submitted = 0
            for cand in candidates:
                assessment = lifecycle.assess(cand)
                if assessment.phase is not Phase.ENTRY:
                    continue
                state = await governor._store.get(ENGINE_ID)  # noqa: SLF001
                open_positions = state.open_positions if state else 0
                decision = execution.decide(
                    cand,
                    assessment,
                    account_equity=account.equity,
                    open_positions=open_positions,
                )
                if decision is None:
                    continue
                placed = await order_manager.submit_decision(decision, assessment)
                if placed:
                    submitted += 1

            logger.info(
                "vector.scheduler.run_done",
                as_of=str(as_of),
                n_candidates=len(candidates),
                submitted=submitted,
                new_aars=len(new_aars),
            )
            return RunSummary(as_of=as_of, n_candidates=len(candidates), n_submitted=submitted)
        finally:
            await pool.close()

    @staticmethod
    def _build_parity_harness(pool, *, paper_broker) -> LivePaperParityHarness | None:
        """Return a harness only when ``ENABLE_PARITY_HARNESS=true`` *and* live creds are present.

        Live credentials live in ``ALPACA_LIVE_KEY`` / ``ALPACA_LIVE_SECRET``;
        if either is missing we return None and skip parity for this run.
        """
        if os.getenv("ENABLE_PARITY_HARNESS", "false").lower() != "true":
            return None
        live_key = os.getenv("ALPACA_LIVE_KEY")
        live_secret = os.getenv("ALPACA_LIVE_SECRET")
        if not live_key or not live_secret:
            logger.info("vector.scheduler.parity_disabled_no_live_creds")
            return None
        live_broker = AlpacaPaperBrokerAdapter(
            api_key=live_key, api_secret=live_secret, paper=False
        )
        return LivePaperParityHarness(paper_broker, live_broker, pool)


async def _amain() -> int:
    equity = Decimal(os.getenv("VECTOR_ENGINE_EQUITY", "10000"))
    await _ping_healthcheck("/start")
    try:
        scheduler = VectorScheduler(engine_equity=equity)
        summary = await scheduler.run_once()
    except Exception as exc:
        logger.exception("vector.scheduler.run_failed", error=str(exc))
        await _ping_healthcheck("/fail")
        return 1

    logger.info(
        "vector.scheduler.summary",
        as_of=summary.as_of.isoformat(),
        n_candidates=summary.n_candidates,
        n_submitted=summary.n_submitted,
    )
    await _ping_healthcheck("")
    return 0


def main() -> None:  # pragma: no cover
    raise SystemExit(asyncio.run(_amain()))


__all__ = ["RunSummary", "VectorScheduler", "main"]


if __name__ == "__main__":  # pragma: no cover
    main()
