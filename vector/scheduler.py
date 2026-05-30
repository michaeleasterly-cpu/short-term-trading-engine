"""Vector scheduler — daily cron entry point.

MVP scope: scan the universe for candidates, log them, submit Phase.ENTRY
candidates as Alpaca paper bracket orders. Open-position reconciliation,
AAR persistence on close, and trail-stop re-evaluation are TODO for the
next iteration — Sigma's order_manager has the full shape and Vector will
mirror it once paper-trade volume justifies the lift.

Calling cadence: daily, weekday-only, Mon–Fri 22:00 UTC (see ``railway.json``).

Required env:
    DATABASE_URL                — Postgres URL for prices + fundamentals.
    ALPACA_KEY / ALPACA_SECRET  — paper credentials.
    VECTOR_ENGINE_EQUITY        — optional; default 10000.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from datetime import date as date_t
from decimal import Decimal
from typing import Any

import pandas as pd
import structlog

from tpcore.aar.writer import AARWriter
from tpcore.alpaca import AlpacaPaperBrokerAdapter
from tpcore.data.postgres_data_adapter import PostgresDataAdapter
from tpcore.data.repositories import FundamentalsRepo, PricesRepo
from tpcore.db import build_asyncpg_pool
from tpcore.identity.dispatcher import IdentityDispatcher
from tpcore.logging import DBLogHandler
from tpcore.parity import LivePaperParityHarness
from tpcore.risk.governor import RiskGovernor
from tpcore.risk.persistent_store import PostgresRiskStateStore
from vector.models import Phase
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


async def _load_bars(pool, tickers: tuple[str, ...], lookback_end: date_t) -> dict[str, pd.DataFrame]:
    """Pull the last LOOKBACK_DAYS sessions for each ticker.

    Date lower bound: ``lookback_end - 400 calendar days`` ≈ 280 trading
    sessions, comfortably above LOOKBACK_DAYS=260. Without this bound
    the query pulls every historical bar for each ticker (30+ years on
    legacy names) and times out at the Supabase statement limit before
    Python ever gets to ``df.tail(LOOKBACK_DAYS)``.
    """
    from datetime import timedelta

    lookback_start = lookback_end - timedelta(days=400)
    dispatcher = IdentityDispatcher(pool)
    repo = PricesRepo(pool)
    cid_to_ticker: dict[str, str] = {}
    for t in tickers:
        cid = await dispatcher.ticker_to_classification_id(t)
        if cid is not None:
            cid_to_ticker[cid] = t
    by_ticker: dict[str, list[dict]] = defaultdict(list)
    if not cid_to_ticker:
        return by_ticker
    bars_by_cid = await repo.get_window_batch(
        list(cid_to_ticker),
        lookback_start,
        lookback_end,
    )
    for cid, bars in bars_by_cid.items():
        ticker = cid_to_ticker[cid]
        for b in sorted(bars, key=lambda x: x.date):
            by_ticker[ticker].append(
                {
                    "date": b.date,
                    "open": float(b.open),
                    "high": float(b.high),
                    "low": float(b.low),
                    "close": float(b.close),
                    "volume": int(b.volume),
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
    """Latest cached fundamentals snapshot per ticker, PIT-filtered to ``as_of``.

    Uses one batched SQL query instead of ``cache.get_quarterly_fundamentals``
    per ticker (which was 2 queries × N tickers = O(2N) round-trips, ~127s
    for the T1+T2 universe). The batched read pulls every PIT-eligible row
    for every requested ticker, then groups in Python: row 0 is "latest",
    rows 1..N are "history". Tickers with zero rows map to None.
    """
    from datetime import date as _date
    from decimal import Decimal as _Decimal

    dispatcher = IdentityDispatcher(pool)
    repo = FundamentalsRepo(pool)
    upper_tickers = [t.upper() for t in tickers]
    cid_to_ticker: dict[str, str] = {}
    for t in upper_tickers:
        cid = await dispatcher.ticker_to_classification_id(t)
        if cid is not None:
            cid_to_ticker[cid] = t
    filings_by_cid: dict[str, list] = {}
    if cid_to_ticker:
        filings_by_cid = await repo.get_window_batch(
            list(cid_to_ticker),
            _date(1900, 1, 1),
            as_of,
        )
    # Reassemble into the legacy (ticker, filing_date DESC) row order.
    rows = []
    for cid, filings in filings_by_cid.items():
        ticker = cid_to_ticker[cid]
        for f in sorted(filings, key=lambda x: x.filing_date, reverse=True):
            rows.append(
                {
                    "ticker": ticker,
                    "filing_date": f.filing_date,
                    "period_end_date": f.period_end_date,
                    "period_label": f.period_label,
                    "net_income": f.net_income,
                    "fcf": f.fcf,
                    "operating_cash_flow": f.operating_cash_flow,
                    "capex": f.capex,
                    "revenue": f.revenue,
                    "total_assets": f.total_assets,
                    "total_liabilities": f.total_liabilities,
                    "current_assets": f.current_assets,
                    "current_liabilities": f.current_liabilities,
                    "receivables": f.receivables,
                    "cash_and_equivalents": f.cash_and_equivalents,
                    "shares_outstanding": f.shares_outstanding,
                }
            )

    def _dec(v: Any) -> _Decimal | None:
        return _Decimal(str(v)) if v is not None else None

    def _row_to_dict(r) -> dict[str, Any]:
        return {
            "symbol": r["ticker"],
            "period": r["period_label"],
            "period_end_date": r["period_end_date"],
            "filing_date": r["filing_date"],
            "net_income": _dec(r["net_income"]),
            "revenue": _dec(r["revenue"]),
            "fcf": _dec(r["fcf"]),
            "operating_cash_flow": _dec(r["operating_cash_flow"]),
            "capex": _dec(r["capex"]),
            "total_assets": _dec(r["total_assets"]),
            "total_liabilities": _dec(r["total_liabilities"]),
            "current_assets": _dec(r["current_assets"]),
            "current_liabilities": _dec(r["current_liabilities"]),
            "receivables": _dec(r["receivables"]),
            "cash_and_equivalents": _dec(r["cash_and_equivalents"]),
            "shares_outstanding": _dec(r["shares_outstanding"]),
        }

    by_ticker: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_ticker.setdefault(r["ticker"], []).append(_row_to_dict(r))
    out: dict[str, dict[str, Any] | None] = {}
    for ticker in tickers:
        ticker_rows = by_ticker.get(ticker.upper(), [])
        if not ticker_rows:
            out[ticker] = None
            continue
        latest = dict(ticker_rows[0])
        latest["history"] = ticker_rows[1:]
        out[ticker] = latest
    return out


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
        # Daily bars come from platform.prices_daily — there is no live-API
        # fallback. Without DATABASE_URL the engine cannot honor backtest
        # parity; refuse to run rather than silently no-op.
        if not self._database_url:
            logger.critical(
                "vector.scheduler.no_database_pool",
                message=("No database pool available. Refusing to run without source-of-truth data."),
            )
            raise SystemExit(1)

        run_id = uuid.uuid4()
        started_at = time.monotonic()
        exit_code = 0
        pool = await build_asyncpg_pool(self._database_url)
        broker = self._injected_broker or AlpacaPaperBrokerAdapter()
        db_log = DBLogHandler(pool, ENGINE_ID, run_id)
        await db_log.startup(commit_sha=os.getenv("RAILWAY_GIT_COMMIT_SHA") or os.getenv("GIT_COMMIT_SHA"))
        try:
            # Wire risk governor + AAR writer + (optional) parity harness.
            governor = RiskGovernor(
                state_store=PostgresRiskStateStore(pool),
                broker=broker,
                platform_capital=self._engine_equity,
                pool=pool,
            )
            await governor.register_engine(ENGINE_ID, self._engine_equity)

            # Kill-switch short-circuit: refuse to scan or submit when frozen.
            current_state = await governor.state_for(ENGINE_ID)
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
            for aar in new_aars:
                await db_log.fill_confirmed(
                    aar.ticker,
                    fill_price=str(aar.exit_price),
                    pnl=str(aar.pnl_net),
                )

            scan_started = time.monotonic()
            data = PostgresDataAdapter(pool)
            # Vector pre-fetches bars + fundamentals upfront, so the
            # universe must be small enough to fit under Supabase's
            # statement timeout. The credibility backtests scored on
            # T1+T2 (~1,200 tickers); use the same universe live.
            # 2026-05-30: scoped to EngineProfile.allowed_asset_classes
            # (default stock+adr+reit+etf — excludes SPAC*/CEF/preferred
            # which fundamentals models misweight).
            from tpcore.engine_profile import profile_for
            _profile = profile_for("vector")
            universe = tuple(await data.get_universe_by_liquidity_tier(
                max_tier=2,
                asset_class_in=(
                    _profile.allowed_asset_classes if _profile else None
                ),
            ))
            logger.info(
                "vector.scheduler.universe_loaded",
                as_of=str(as_of),
                universe_size=len(universe),
                source="liquidity_tiers<=2",
            )
            tickers = universe + (SPY_SYMBOL,)
            bars = await _load_bars(pool, tickers, as_of)
            spy_panel = bars.pop(SPY_SYMBOL, None)
            fundamentals = await _load_fundamentals(pool, universe, as_of)

            setup = VectorSetupDetection(universe=universe)
            lifecycle = VectorLifecycleAnalysis()
            execution = VectorExecutionRisk()

            candidates = setup.scan(
                as_of=as_of,
                bars_by_ticker=bars,
                fundamentals_by_ticker=fundamentals,
                spy_panel=spy_panel,
                vix_value=None,  # MVP — VIX feed deferred; ExecutionRisk treats None as low-VIX
            )
            scan_ms = int((time.monotonic() - scan_started) * 1000)
            logger.info("vector.scheduler.scan_done", n_candidates=len(candidates), as_of=str(as_of))
            await db_log.scan_complete(len(candidates), scan_ms)

            account = await broker.get_account()
            submitted = 0
            for cand in candidates:
                assessment = lifecycle.assess(cand)
                if assessment.phase is not Phase.ENTRY:
                    continue
                # Attach scan-time filter diagnostics so the operator can
                # see at the SIGNAL row how many tickers were filtered at
                # each gate today. exclude_none keeps the payload sparse —
                # only the gates Vector populates land in the JSON.
                _diag = (
                    cand.filter_diagnostics.model_dump(exclude_none=True)
                    if cand.filter_diagnostics is not None
                    else None
                )
                await db_log.signal(
                    cand.ticker,
                    score=float(cand.swing_score),
                    extra_data=({"filter_diagnostics": _diag} if _diag else None),
                )
                state = await governor.state_for(ENGINE_ID)
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
                    await db_log.order_submitted(decision.ticker, decision.qty)

            logger.info(
                "vector.scheduler.run_done",
                as_of=str(as_of),
                n_candidates=len(candidates),
                submitted=submitted,
                new_aars=len(new_aars),
            )
            return RunSummary(as_of=as_of, n_candidates=len(candidates), n_submitted=submitted)
        except Exception as exc:
            exit_code = 1
            await db_log.error(exc, context="scheduler_crash")
            raise
        finally:
            duration_ms = int((time.monotonic() - started_at) * 1000)
            await db_log.shutdown(duration_ms, exit_code)
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
        live_broker = AlpacaPaperBrokerAdapter(api_key=live_key, api_secret=live_secret, paper=False)
        return LivePaperParityHarness(paper_broker, live_broker, pool)


async def _amain() -> int:
    equity = Decimal(os.getenv("VECTOR_ENGINE_EQUITY", "10000"))
    try:
        scheduler = VectorScheduler(engine_equity=equity)
        summary = await scheduler.run_once()
    except Exception as exc:
        logger.exception("vector.scheduler.run_failed", error=str(exc))
        return 1

    logger.info(
        "vector.scheduler.summary",
        as_of=summary.as_of.isoformat(),
        n_candidates=summary.n_candidates,
        n_submitted=summary.n_submitted,
    )
    return 0


def main() -> None:  # pragma: no cover
    raise SystemExit(asyncio.run(_amain()))


__all__ = ["RunSummary", "VectorScheduler", "main"]


if __name__ == "__main__":  # pragma: no cover
    main()
