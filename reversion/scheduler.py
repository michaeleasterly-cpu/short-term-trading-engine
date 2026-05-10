"""Reversion scheduler — daily entry point.

Mirror of ``sigma.scheduler.SigmaScheduler``. Defaults to LONG-only
(``allow_shorts=False``) for paper safety — Alpaca paper short-borrow
availability is per-symbol and unstable. Toggle via the ``ALLOW_SHORTS``
env var or the constructor.

Calling cadence: per ``MASTER_PLAN.md §4.2`` Reversion is daily-timeframe;
fire once per session a few minutes after close.
"""
from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from datetime import date as date_t
from decimal import Decimal
from typing import Any

import structlog

from reversion.models import Phase
from reversion.order_manager import ENGINE_ID, ReversionOrderManager
from reversion.plugs.aar_logging import ReversionAARLogging
from reversion.plugs.capital_gate import ReversionCapitalGate
from reversion.plugs.execution_risk import ReversionExecutionRisk
from reversion.plugs.lifecycle_analysis import ReversionLifecycleAnalysis
from reversion.plugs.setup_detection import ReversionSetupDetection
from tpcore.aar.models import AfterActionReport
from tpcore.aar.writer import AARWriter
from tpcore.alpaca import AlpacaPaperBrokerAdapter
from tpcore.data.postgres_data_adapter import PostgresDataAdapter
from tpcore.db import build_asyncpg_pool
from tpcore.fmp import FMPFundamentalsAdapter
from tpcore.fundamentals.cache import FundamentalsCache
from tpcore.interfaces.data import DataProviderInterface
from tpcore.outage import DataProviderOutage
from tpcore.parity import LivePaperParityHarness
from tpcore.risk.governor import (
    InMemoryRiskStateStore,
    RiskGovernor,
    RiskStateStore,
)
from tpcore.risk.persistent_store import PostgresRiskStateStore

logger = structlog.get_logger(__name__)


class RunSummary:
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


class ReversionScheduler:
    """One-shot orchestration of a full Reversion trading cycle."""

    def __init__(
        self,
        *,
        engine_equity: Decimal = Decimal("10000"),
        platform_capital: Decimal = Decimal("10000"),
        allow_shorts: bool = False,
        database_url: str | None = None,
        broker: AlpacaPaperBrokerAdapter | None = None,
        data: DataProviderInterface | None = None,
        risk_store: RiskStateStore | None = None,
        aar_writer: AARWriter | None = None,
        fundamentals: FMPFundamentalsAdapter | None = None,
    ) -> None:
        self._engine_equity = engine_equity
        self._platform_capital = platform_capital
        self._allow_shorts = allow_shorts
        self._database_url = database_url if database_url is not None else os.getenv("DATABASE_URL")
        self._injected_broker = broker
        self._injected_data = data
        self._injected_risk_store = risk_store
        self._injected_aar_writer = aar_writer
        self._injected_fundamentals = fundamentals

    async def run_once(self, *, as_of: date_t | None = None) -> RunSummary:
        as_of = as_of or datetime.now(UTC).date()
        pool: Any | None = None
        try:
            if self._injected_risk_store is None and self._injected_aar_writer is None:
                if self._database_url:
                    pool = await build_asyncpg_pool(self._database_url)
                    logger.info("reversion.scheduler.pool_open")
            # Daily bars must come from platform.prices_daily — no live-API
            # fallback. Mirrors sigma.scheduler. Build pool here if it wasn't
            # already built for risk/aar.
            if self._injected_data is None and pool is None:
                if self._database_url:
                    pool = await build_asyncpg_pool(self._database_url)
                    logger.info("reversion.scheduler.pool_open_for_data")
                else:
                    logger.critical(
                        "reversion.scheduler.no_database_pool",
                        message=(
                            "No database pool available. Refusing to run "
                            "without source-of-truth data."
                        ),
                    )
                    raise SystemExit(1)

            broker = self._injected_broker or AlpacaPaperBrokerAdapter()
            data = self._injected_data or PostgresDataAdapter(pool)
            risk_store = self._injected_risk_store or (
                PostgresRiskStateStore(pool) if pool is not None else InMemoryRiskStateStore()
            )
            aar_writer = self._injected_aar_writer or (
                AARWriter(pool) if pool is not None else None
            )

            governor = RiskGovernor(
                state_store=risk_store,
                broker=broker,
                platform_capital=self._platform_capital,
            )
            await governor.register_engine(ENGINE_ID, self._engine_equity)
            logger.info(
                "reversion.scheduler.run_start",
                as_of=as_of.isoformat(),
                allow_shorts=self._allow_shorts,
                persistent=pool is not None,
            )

            # Kill-switch short-circuit: refuse to scan or submit when frozen.
            current_state = await risk_store.get(ENGINE_ID)
            if current_state and current_state.kill_switch_active:
                logger.critical(
                    "reversion.scheduler.kill_switch_active",
                    engine=ENGINE_ID,
                    reason=current_state.kill_switch_reason or "unspecified",
                )
                return RunSummary(as_of=as_of, n_candidates=0, n_submitted=0, aars=[])

            setup = ReversionSetupDetection(data=data)
            lifecycle = ReversionLifecycleAnalysis()
            execution = ReversionExecutionRisk()
            rev_aar = ReversionAARLogging()
            gate = ReversionCapitalGate(engine_equity=self._engine_equity)
            parity = self._build_parity_harness(pool, paper_broker=broker)
            order_manager = ReversionOrderManager(
                broker=broker,
                governor=governor,
                capital_gate=gate,
                lifecycle=lifecycle,
                aar=rev_aar,
                aar_writer=aar_writer,
                parity_harness=parity,
            )

            new_aars = await order_manager.reconcile(
                sizing_pct_of_engine_equity=Decimal("0.20"),
            )

            candidates = await setup.scan(as_of=as_of)
            logger.info("reversion.scheduler.scan_done", n_candidates=len(candidates))

            # Fundamentals provider — DB-cached when a pool is open, falls
            # back to direct FMP otherwise. The cache lets daily scheduler
            # runs hit FMP only on cache miss (new filings) instead of
            # every candidate, every day.
            fundamentals_provider, owned_adapter = await self._build_fundamentals_provider(pool)

            submitted = 0
            account = await broker.get_account()
            try:
                for cand in candidates:
                    fundamentals = await self._fetch_fundamentals(
                        fundamentals_provider, cand.ticker, cand.as_of
                    )
                    assessment = lifecycle.assess(cand, fundamentals=fundamentals)
                    if assessment.phase is not Phase.ACTIVE:
                        continue
                    state = await risk_store.get(ENGINE_ID)
                    open_positions = state.open_positions if state else 0
                    decision = execution.decide(
                        assessment,
                        account_capital=account.equity,
                        open_positions=open_positions,
                        allow_shorts=self._allow_shorts,
                    )
                    if decision is None:
                        continue
                    placed = await order_manager.submit_decision(decision, assessment)
                    if placed:
                        submitted += 1
            finally:
                if owned_adapter is not None:
                    await owned_adapter.aclose()

            logger.info(
                "reversion.scheduler.run_done",
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
        finally:
            if pool is not None:
                await pool.close()
                logger.info("reversion.scheduler.pool_closed")

    @staticmethod
    def _build_parity_harness(pool, *, paper_broker) -> LivePaperParityHarness | None:
        """Return a harness only when ``ENABLE_PARITY_HARNESS=true`` *and* live creds are present.

        Mirrors ``vector.scheduler._build_parity_harness`` and
        ``sigma.scheduler._build_parity_harness`` so all three engines share
        the same opt-in semantics. Live credentials live in
        ``ALPACA_LIVE_KEY`` / ``ALPACA_LIVE_SECRET``; if either is missing
        we return None and skip parity for this run.
        """
        if pool is None:
            return None
        if os.getenv("ENABLE_PARITY_HARNESS", "false").lower() != "true":
            return None
        live_key = os.getenv("ALPACA_LIVE_KEY")
        live_secret = os.getenv("ALPACA_LIVE_SECRET")
        if not live_key or not live_secret:
            logger.info("reversion.scheduler.parity_disabled_no_live_creds")
            return None
        live_broker = AlpacaPaperBrokerAdapter(
            api_key=live_key, api_secret=live_secret, paper=False
        )
        return LivePaperParityHarness(paper_broker, live_broker, pool)

    async def _build_fundamentals_provider(
        self, pool: Any | None
    ) -> tuple[Any | None, FMPFundamentalsAdapter | None]:
        """Return ``(provider, owned_adapter)``.

        ``provider`` is whatever exposes ``get_quarterly_fundamentals`` —
        a ``FundamentalsCache`` when a DB pool is available, else the raw
        adapter. ``owned_adapter`` is the adapter to close on exit (we
        own the lifecycle since we built it here); ``None`` if the caller
        injected a provider (lifecycle is theirs).
        """
        if self._injected_fundamentals is not None:
            return self._injected_fundamentals, None
        try:
            adapter = FMPFundamentalsAdapter()
        except DataProviderOutage as exc:
            logger.warning(
                "reversion.scheduler.fundamentals_unavailable",
                error=str(exc),
                note="every candidate will be blocked by the earnings-quality gate",
            )
            return None, None
        if pool is not None:
            cache = FundamentalsCache(pool, adapter=adapter)
            return cache, adapter
        return adapter, adapter

    @staticmethod
    async def _fetch_fundamentals(
        provider: Any | None, symbol: str, as_of: date_t
    ) -> dict | None:
        """Pull fundamentals for ``symbol``, returning ``None`` on outage.

        ``DataProviderOutage`` from the provider is logged and swallowed —
        a single bad symbol shouldn't kill the whole scan. The lifecycle
        plug treats ``None`` as "no data, no trade" and blocks just that
        candidate.
        """
        if provider is None:
            return None
        try:
            return await provider.get_quarterly_fundamentals(symbol, as_of_date=as_of)
        except DataProviderOutage as exc:
            logger.warning(
                "reversion.scheduler.fundamentals_outage",
                symbol=symbol,
                error=str(exc),
            )
            return None


async def _ping_healthcheck(suffix: str = "") -> None:
    """Best-effort ping to Reversion's Healthchecks.io check.

    Resolution order: ``REVERSION_HEALTHCHECKS_PING_URL`` (engine-specific,
    set on Railway as a separate variable) → ``HEALTHCHECKS_PING_URL``
    (shared fallback). Each engine should have its own check so a missed
    Reversion run isn't masked by a healthy Sigma run.
    """
    url = (
        os.getenv("REVERSION_HEALTHCHECKS_PING_URL")
        or os.getenv("HEALTHCHECKS_PING_URL_REVERSION")
        or os.getenv("HEALTHCHECKS_PING_URL")
    )
    if not url:
        return
    import httpx

    target = url.rstrip("/") + suffix
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.get(target)
    except Exception as exc:  # pragma: no cover - network-best-effort
        logger.warning("reversion.scheduler.healthcheck_ping_failed", suffix=suffix, error=str(exc))


async def _amain() -> int:
    equity = Decimal(os.getenv("REVERSION_ENGINE_EQUITY", "10000"))
    platform_capital = Decimal(os.getenv("PLATFORM_CAPITAL", str(equity)))
    allow_shorts = os.getenv("ALLOW_SHORTS", "false").lower() == "true"

    await _ping_healthcheck("/start")
    try:
        scheduler = ReversionScheduler(
            engine_equity=equity,
            platform_capital=platform_capital,
            allow_shorts=allow_shorts,
        )
        summary = await scheduler.run_once()
    except Exception as exc:
        logger.exception("reversion.scheduler.run_failed", error=str(exc))
        await _ping_healthcheck("/fail")
        return 1

    logger.info(
        "reversion.scheduler.summary",
        as_of=summary.as_of.isoformat(),
        n_candidates=summary.n_candidates,
        n_submitted=summary.n_submitted,
        n_aars=len(summary.aars),
    )
    await _ping_healthcheck("")
    return 0


def main() -> None:  # pragma: no cover - CLI shim
    raise SystemExit(asyncio.run(_amain()))


__all__ = ["RunSummary", "ReversionScheduler", "main"]


if __name__ == "__main__":  # pragma: no cover
    main()
