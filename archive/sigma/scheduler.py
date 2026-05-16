"""Sigma scheduler — daily entry point.

Wires the five plugs, broker adapter, data adapter, risk governor, and
Postgres persistence into a single ``run_once`` invocation that an external
scheduler (cron, systemd timer, GitHub Actions, etc.) can call.

Responsibilities each run:
    1. Open an asyncpg connection pool (when ``DATABASE_URL`` is set) so
       risk state and AARs persist to ``platform.*``. Without ``DATABASE_URL``
       the run uses in-memory state and skips DB writes — useful for dry runs.
    2. Reconcile open trades with the broker — fire Tier 1 / Tier 2 / hard-stop
       events and persist any AARs (idempotent across runs).
    3. Run setup detection on the configured universe.
    4. For every ACTIVE-phase candidate, build an ``ExecutionDecision`` and
       hand it to ``SigmaOrderManager``, which gates → governs → submits.
    5. Close the pool before exit. Railway's cron worker policy is "exit
       cleanly" — leaking pool slots blocks the next scheduled fire.

Calling cadence: per ``MASTER_PLAN.md §4.1`` Sigma is a daily-timeframe
strategy. The scheduler is meant to fire once per session — typically a few
minutes after close so closing prints have settled. Intra-day fills are
picked up on the *next* run; that's a deliberate trade-off — Sigma's
lifecycle is days, not minutes.
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid
from datetime import UTC, datetime, timedelta
from datetime import date as date_t
from decimal import Decimal
from typing import Any

import structlog

from sigma.models import (
    SPY_REGIME_DRAWDOWN_LOOKBACK_DAYS,
    SPY_REGIME_DRAWDOWN_PCT,
    SPY_REGIME_REALIZED_VOL_THRESHOLD,
    SPY_REGIME_REBOUND_LOOKBACK_DAYS,
    SPY_REGIME_VOL_LOOKBACK_DAYS,
    Phase,
)
from sigma.order_manager import ENGINE_ID, SigmaOrderManager
from sigma.plugs.aar_logging import SigmaAARLogging
from sigma.plugs.capital_gate import SigmaCapitalGate
from sigma.plugs.execution_risk import SigmaExecutionRisk
from sigma.plugs.lifecycle_analysis import SigmaLifecycleAnalysis
from sigma.plugs.setup_detection import SigmaSetupDetection
from tpcore.aar.models import AfterActionReport
from tpcore.aar.writer import AARWriter
from tpcore.alpaca import AlpacaPaperBrokerAdapter
from tpcore.data.postgres_data_adapter import PostgresDataAdapter
from tpcore.db import build_asyncpg_pool
from tpcore.fmp import FMPFundamentalsAdapter
from tpcore.fundamentals.cache import FundamentalsCache
from tpcore.interfaces.data import DataProviderInterface
from tpcore.logging import DBLogHandler
from tpcore.outage import DataProviderOutage
from tpcore.parity import LivePaperParityHarness
from tpcore.risk.governor import (
    InMemoryRiskStateStore,
    RiskGovernor,
)
from tpcore.risk.persistent_store import PostgresRiskStateStore

logger = structlog.get_logger(__name__)


class RunSummary:
    """Lightweight summary of a single ``run_once`` invocation."""

    def __init__(
        self,
        *,
        as_of: date_t,
        n_candidates: int,
        n_submitted: int,
        aars: list[AfterActionReport],
    ) -> None:
        self.as_of = as_of
        self.n_candidates = n_candidates
        self.n_submitted = n_submitted
        self.aars = aars

    def __repr__(self) -> str:
        return (
            f"RunSummary(as_of={self.as_of}, n_candidates={self.n_candidates}, "
            f"n_submitted={self.n_submitted}, n_aars={len(self.aars)})"
        )


async def _spy_regime_blocks_entries(pool, as_of: date_t) -> tuple[bool, dict | None]:
    """Market-level regime gate (added 2026-05-15 per param-sweep finding).

    Returns ``(True, payload)`` when Sigma should suppress all entries for
    this session. ``payload`` carries the diagnostic numbers for logging.
    Returns ``(False, None)`` when the regime is permissive.

    Blocks under either condition (logical OR):

    * **Momentum-crash recovery**: SPY is ≥ ``SPY_REGIME_DRAWDOWN_PCT``
      below its ``SPY_REGIME_DRAWDOWN_LOOKBACK_DAYS``-day high, AND its
      ``SPY_REGIME_REBOUND_LOOKBACK_DAYS``-day return is strictly
      positive (rebounding). Range scalping in V-shaped recoveries was
      the trade that produced the −0.84 Sharpe walk-forward window.
    * **Elevated volatility**: SPY's ``SPY_REGIME_VOL_LOOKBACK_DAYS``-day
      annualized realized volatility exceeds
      ``SPY_REGIME_REALIZED_VOL_THRESHOLD``. High vol expands the
      Bollinger band faster than we can scalp it.

    Degrades gracefully on data shortage — if fewer than
    ``SPY_REGIME_DRAWDOWN_LOOKBACK_DAYS`` SPY bars are available
    (rare; happens during fresh DB / backfill in progress), returns
    ``(False, None)`` so the engine isn't blocked indefinitely by
    a data outage.
    """
    import math
    from decimal import Decimal

    lookback_days = max(
        SPY_REGIME_DRAWDOWN_LOOKBACK_DAYS,
        SPY_REGIME_VOL_LOOKBACK_DAYS + 1,
    )
    sql = """
        SELECT date, close
        FROM platform.prices_daily
        WHERE ticker = 'SPY'
          AND date <= $1
        ORDER BY date DESC
        LIMIT $2
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, as_of, lookback_days)
    if len(rows) < SPY_REGIME_DRAWDOWN_LOOKBACK_DAYS:
        return False, None

    # Rows are DESCENDING (newest first); flip for chronological work.
    closes = [Decimal(str(r["close"])) for r in reversed(rows)]
    latest = closes[-1]

    # Drawdown from 60-day high.
    high_60 = max(closes[-SPY_REGIME_DRAWDOWN_LOOKBACK_DAYS:])
    drawdown_pct = (high_60 - latest) / high_60 if high_60 > 0 else Decimal("0")

    # 5-day return.
    ret_5d = (
        (latest - closes[-SPY_REGIME_REBOUND_LOOKBACK_DAYS - 1])
        / closes[-SPY_REGIME_REBOUND_LOOKBACK_DAYS - 1]
        if len(closes) > SPY_REGIME_REBOUND_LOOKBACK_DAYS and closes[-SPY_REGIME_REBOUND_LOOKBACK_DAYS - 1] > 0
        else Decimal("0")
    )

    # 20-day annualized realized volatility (stdev of daily log returns ×
    # √252). Uses float for the log/sqrt path; the threshold compare
    # rounds via Decimal.
    vol_closes = closes[-SPY_REGIME_VOL_LOOKBACK_DAYS - 1:]
    log_returns = [
        math.log(float(vol_closes[i] / vol_closes[i - 1]))
        for i in range(1, len(vol_closes))
        if vol_closes[i - 1] > 0
    ]
    if log_returns:
        mean = sum(log_returns) / len(log_returns)
        var = sum((r - mean) ** 2 for r in log_returns) / max(1, len(log_returns) - 1)
        realized_vol_annual = Decimal(str(math.sqrt(var) * math.sqrt(252)))
    else:
        realized_vol_annual = Decimal("0")

    drawdown_recovery = (
        drawdown_pct >= SPY_REGIME_DRAWDOWN_PCT and ret_5d > 0
    )
    high_vol = realized_vol_annual > SPY_REGIME_REALIZED_VOL_THRESHOLD

    if drawdown_recovery or high_vol:
        return True, {
            "spy_last_close": str(latest),
            "spy_60d_high": str(high_60),
            "drawdown_pct": str(drawdown_pct.quantize(Decimal("0.0001"))),
            "ret_5d": str(ret_5d.quantize(Decimal("0.0001"))),
            "realized_vol_annual": str(realized_vol_annual.quantize(Decimal("0.0001"))),
            "trigger_drawdown_recovery": drawdown_recovery,
            "trigger_high_vol": high_vol,
        }
    return False, None


class SigmaScheduler:
    """One-shot orchestration of a full Sigma trading cycle.

    Construction is intentionally minimal — heavy resources (broker client,
    asyncpg pool) are built per-run inside ``run_once`` so the scheduler is
    safe to import in tests without side effects.
    """

    def __init__(
        self,
        *,
        engine_equity: Decimal = Decimal("10000"),
        platform_capital: Decimal = Decimal("10000"),
        database_url: str | None = None,
        broker: AlpacaPaperBrokerAdapter | None = None,
        data: DataProviderInterface | None = None,
        aar_writer: AARWriter | None = None,
    ) -> None:
        self._engine_equity = engine_equity
        self._platform_capital = platform_capital
        self._database_url = database_url if database_url is not None else os.getenv("DATABASE_URL")
        self._injected_broker = broker
        self._injected_data = data
        self._injected_aar_writer = aar_writer
        # risk_store no longer injectable — the governor's state_for()
        # public method (added 2026-05-14) removed the need for the
        # parallel `risk_store` reference the scheduler used to carry
        # solely to dodge the `governor._store` private-attr noqa.

    async def run_once(self, *, as_of: date_t | None = None) -> RunSummary:
        as_of = as_of or datetime.now(UTC).date()
        run_id = uuid.uuid4()
        started_at = time.monotonic()
        pool: Any | None = None
        db_log: DBLogHandler | None = None
        exit_code = 0
        owned_fundamentals_adapter: FMPFundamentalsAdapter | None = None
        try:
            # 0. Build pool (and DB-backed deps) iff DATABASE_URL is set and
            #    no explicit aar_writer was injected.
            if self._injected_aar_writer is None:
                if self._database_url:
                    pool = await build_asyncpg_pool(self._database_url)
                    logger.info("sigma.scheduler.pool_open")
            # Daily bars must come from platform.prices_daily — no live-API
            # fallback. If the caller didn't inject a data provider AND the
            # default adapter has no pool, refuse to run rather than silently
            # diverge from backtest. Build the pool here if it wasn't already
            # built for risk/aar (the case when both of those are injected).
            if self._injected_data is None and pool is None:
                if self._database_url:
                    pool = await build_asyncpg_pool(self._database_url)
                    logger.info("sigma.scheduler.pool_open_for_data")
                else:
                    logger.critical(
                        "sigma.scheduler.no_database_pool",
                        message=(
                            "No database pool available. Refusing to run "
                            "without source-of-truth data."
                        ),
                    )
                    raise SystemExit(1)

            broker = self._injected_broker or AlpacaPaperBrokerAdapter()
            data = self._injected_data or PostgresDataAdapter(pool)
            risk_store = (
                PostgresRiskStateStore(pool) if pool is not None else InMemoryRiskStateStore()
            )
            aar_writer = self._injected_aar_writer or (
                AARWriter(pool) if pool is not None else None
            )

            # Database-backed audit log — best-effort, never blocks the run.
            # Pool absence (test path with injected aar_writer) silently
            # skips DB logging; stdout structlog still records.
            if pool is not None:
                db_log = DBLogHandler(pool, ENGINE_ID, run_id)
                await db_log.startup(
                    commit_sha=os.getenv("RAILWAY_GIT_COMMIT_SHA")
                    or os.getenv("GIT_COMMIT_SHA")
                )

            governor = RiskGovernor(
                state_store=risk_store,
                broker=broker,
                platform_capital=self._platform_capital,
                pool=pool,
            )
            await governor.register_engine(ENGINE_ID, self._engine_equity)

            # Kill-switch short-circuit: refuse to scan or submit when the
            # engine is frozen. The platform-wide check_trade() inside the
            # order manager would also block submission, but stopping here
            # avoids wasted FMP / DB / API calls during a freeze.
            current_state = await governor.state_for(ENGINE_ID)
            if current_state and current_state.kill_switch_active:
                logger.critical(
                    "sigma.scheduler.kill_switch_active",
                    engine=ENGINE_ID,
                    reason=current_state.kill_switch_reason or "unspecified",
                )
                return RunSummary(as_of=as_of, n_candidates=0, n_submitted=0, aars=[])

            # SPY market-regime gate (added 2026-05-15 per param-sweep
            # finding). Suppresses entries when SPY is in a momentum-
            # crash recovery or high-vol regime — both produced the
            # walk-forward Sharpe-variance problem (+1.02 to −0.84).
            blocked, regime_payload = await _spy_regime_blocks_entries(pool, as_of)
            if blocked:
                logger.warning(
                    "sigma.scheduler.spy_regime_blocked",
                    engine=ENGINE_ID,
                    **regime_payload,
                )
                return RunSummary(as_of=as_of, n_candidates=0, n_submitted=0, aars=[])

            # Universe: T1+T2 only (~1,274 tickers) to match the
            # credibility backtest scope AND keep the batched fetch
            # well under Supabase's statement timeout. The old
            # all_active universe (~7,695) ran the per-ticker
            # ``get_daily_bars`` loop for 8+ minutes — the operator
            # killed it on 2026-05-14. Universal fix lives in
            # ``tpcore/data/batched_fetchers.py``.
            universe = tuple(await data.get_universe_by_liquidity_tier(max_tier=2))
            logger.info(
                "sigma.scheduler.run_start",
                as_of=as_of.isoformat(),
                persistent=pool is not None,
                universe_size=len(universe),
                source="liquidity_tiers<=2",
            )

            # Pre-fetch bars for the WHOLE universe in one batched
            # SQL — ``setup.scan()`` then runs over in-memory data
            # via PrefetchedBarsAdapter instead of N round-trips.
            from tpcore.data.batched_fetchers import (
                PrefetchedBarsAdapter,
                fetch_bars_batch,
            )

            bars_start = as_of - timedelta(days=120)  # ~85 sessions
            bars_by_ticker = await fetch_bars_batch(
                pool, universe, bars_start, as_of,
            )
            batched_data = PrefetchedBarsAdapter(
                bars_by_ticker, fallback=data,
            )

            # Optional fundamentals cache for informational data-quality
            # attachment. Only enabled when a DB pool is open AND FMP_API_KEY
            # is set — otherwise candidates simply lack the optional field.
            fundamentals_provider, owned_fundamentals_adapter = (
                self._build_fundamentals_provider(pool)
            )

            setup = SigmaSetupDetection(data=batched_data, universe=universe, fundamentals=fundamentals_provider)
            lifecycle = SigmaLifecycleAnalysis()
            execution = SigmaExecutionRisk()
            sigma_aar = SigmaAARLogging()
            gate = SigmaCapitalGate(engine_equity=self._engine_equity)
            parity = self._build_parity_harness(pool, paper_broker=broker)
            order_manager = SigmaOrderManager(
                broker=broker,
                governor=governor,
                capital_gate=gate,
                lifecycle=lifecycle,
                aar=sigma_aar,
                aar_writer=aar_writer,
                parity_harness=parity,
            )

            # 1. Reconcile first so the open-position counter is fresh
            #    before we decide on new entries. Sigma's pre-grad cap is
            #    $1,500 of $10k engine equity → 15% per trade for sizing.
            new_aars = await order_manager.reconcile(
                sizing_pct_of_engine_equity=Decimal("0.15"),
            )
            if db_log is not None:
                for aar in new_aars:
                    await db_log.fill_confirmed(
                        aar.ticker,
                        fill_price=str(aar.exit_price),
                        pnl=str(aar.pnl_net),
                    )

            # 2. Scan for new setups.
            scan_started = time.monotonic()
            candidates = await setup.scan(as_of=as_of)
            scan_ms = int((time.monotonic() - scan_started) * 1000)
            logger.info("sigma.scheduler.scan_done", n_candidates=len(candidates))
            if db_log is not None:
                await db_log.scan_complete(len(candidates), scan_ms)

            submitted = 0
            account = await broker.get_account()
            for cand in candidates:
                assessment = lifecycle.assess(cand)
                if assessment.phase is not Phase.ACTIVE:
                    continue
                if db_log is not None:
                    _diag = (
                        cand.filter_diagnostics.model_dump(exclude_none=True)
                        if cand.filter_diagnostics is not None else None
                    )
                    await db_log.signal(
                        cand.ticker, score=float(cand.sigma_score), direction="LONG",
                        extra_data=({"filter_diagnostics": _diag} if _diag else None),
                    )
                state = await governor.state_for(ENGINE_ID)
                open_positions = state.open_positions if state else 0
                decision = execution.decide(
                    assessment,
                    account_capital=account.equity,
                    open_positions=open_positions,
                )
                if decision is None:
                    continue
                placed = await order_manager.submit_decision(decision, assessment)
                if placed:
                    submitted += 1
                    if db_log is not None:
                        await db_log.order_submitted(decision.ticker, decision.qty)

            logger.info(
                "sigma.scheduler.run_done",
                as_of=as_of.isoformat(),
                n_candidates=len(candidates),
                submitted=submitted,
                new_aars=len(new_aars),
            )
            return RunSummary(
                as_of=as_of,
                n_candidates=len(candidates),
                n_submitted=submitted,
                aars=new_aars,
            )
        except Exception as exc:
            exit_code = 1
            if db_log is not None:
                await db_log.error(exc, context="scheduler_crash")
            raise
        finally:
            if db_log is not None:
                duration_ms = int((time.monotonic() - started_at) * 1000)
                await db_log.shutdown(duration_ms, exit_code)
            if owned_fundamentals_adapter is not None:
                await owned_fundamentals_adapter.aclose()
            if pool is not None:
                await pool.close()
                logger.info("sigma.scheduler.pool_closed")

    @staticmethod
    def _build_parity_harness(pool, *, paper_broker) -> LivePaperParityHarness | None:
        """Return a harness only when ``ENABLE_PARITY_HARNESS=true`` *and* live creds are present.

        Live credentials live in ``ALPACA_LIVE_KEY`` / ``ALPACA_LIVE_SECRET``;
        if either is missing we return None and skip parity for this run.
        Mirrors ``vector.scheduler._build_parity_harness`` so all engines
        share the same opt-in semantics.
        """
        if pool is None:
            return None
        if os.getenv("ENABLE_PARITY_HARNESS", "false").lower() != "true":
            return None
        live_key = os.getenv("ALPACA_LIVE_KEY")
        live_secret = os.getenv("ALPACA_LIVE_SECRET")
        if not live_key or not live_secret:
            logger.info("sigma.scheduler.parity_disabled_no_live_creds")
            return None
        live_broker = AlpacaPaperBrokerAdapter(
            api_key=live_key, api_secret=live_secret, paper=False
        )
        return LivePaperParityHarness(paper_broker, live_broker, pool)

    @staticmethod
    def _build_fundamentals_provider(
        pool: Any | None,
    ) -> tuple[Any | None, FMPFundamentalsAdapter | None]:
        """Returns ``(provider, owned_adapter)``. Provider is None when
        FMP_API_KEY isn't configured — Sigma never gates on this, so the
        absence is silently fine; candidates just won't have data_quality
        populated."""
        try:
            adapter = FMPFundamentalsAdapter()
        except DataProviderOutage:
            return None, None
        if pool is not None:
            return FundamentalsCache(pool, adapter=adapter), adapter
        return adapter, adapter


async def _amain() -> int:
    """Async entry for ``python -m sigma.scheduler``. Returns shell exit code."""
    equity = Decimal(os.getenv("SIGMA_ENGINE_EQUITY", "10000"))
    platform_capital = Decimal(os.getenv("PLATFORM_CAPITAL", str(equity)))

    try:
        scheduler = SigmaScheduler(engine_equity=equity, platform_capital=platform_capital)
        summary = await scheduler.run_once()
    except Exception as exc:
        logger.exception("sigma.scheduler.run_failed", error=str(exc))
        return 1

    logger.info(
        "sigma.scheduler.summary",
        as_of=summary.as_of.isoformat(),
        n_candidates=summary.n_candidates,
        n_submitted=summary.n_submitted,
        n_aars=len(summary.aars),
    )
    return 0


def main() -> None:  # pragma: no cover - CLI shim
    raise SystemExit(asyncio.run(_amain()))


__all__ = ["RunSummary", "SigmaScheduler", "main"]


if __name__ == "__main__":  # pragma: no cover
    main()
