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
from datetime import UTC, date as date_t, datetime
from decimal import Decimal
from typing import Any

import structlog

from tpcore.aar.models import AfterActionReport
from tpcore.aar.writer import AARWriter
from tpcore.alpaca import AlpacaDataAdapter, AlpacaPaperBrokerAdapter
from tpcore.db import build_asyncpg_pool
from tpcore.fmp import FMPFundamentalsAdapter
from tpcore.fundamentals.cache import FundamentalsCache
from tpcore.outage import DataProviderOutage
from tpcore.risk.governor import (
    InMemoryRiskStateStore,
    RiskGovernor,
    RiskStateStore,
)
from tpcore.risk.persistent_store import PostgresRiskStateStore

from sigma.models import Phase
from sigma.order_manager import ENGINE_ID, SigmaOrderManager
from sigma.plugs.aar_logging import SigmaAARLogging
from sigma.plugs.capital_gate import SigmaCapitalGate
from sigma.plugs.execution_risk import SigmaExecutionRisk
from sigma.plugs.lifecycle_analysis import SigmaLifecycleAnalysis
from sigma.plugs.setup_detection import SigmaSetupDetection

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
        data: AlpacaDataAdapter | None = None,
        risk_store: RiskStateStore | None = None,
        aar_writer: AARWriter | None = None,
    ) -> None:
        self._engine_equity = engine_equity
        self._platform_capital = platform_capital
        self._database_url = database_url if database_url is not None else os.getenv("DATABASE_URL")
        self._injected_broker = broker
        self._injected_data = data
        self._injected_risk_store = risk_store
        self._injected_aar_writer = aar_writer

    async def run_once(self, *, as_of: date_t | None = None) -> RunSummary:
        as_of = as_of or datetime.now(UTC).date()
        pool: Any | None = None
        owned_fundamentals_adapter: FMPFundamentalsAdapter | None = None
        try:
            # 0. Build pool (and DB-backed deps) iff DATABASE_URL is set and
            #    no explicit risk_store/aar_writer were injected.
            if self._injected_risk_store is None and self._injected_aar_writer is None:
                if self._database_url:
                    pool = await build_asyncpg_pool(self._database_url)
                    logger.info("sigma.scheduler.pool_open")

            broker = self._injected_broker or AlpacaPaperBrokerAdapter()
            data = self._injected_data or AlpacaDataAdapter()
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
            logger.info("sigma.scheduler.run_start", as_of=as_of.isoformat(), persistent=pool is not None)

            # Kill-switch short-circuit: refuse to scan or submit when the
            # engine is frozen. The platform-wide check_trade() inside the
            # order manager would also block submission, but stopping here
            # avoids wasted FMP / DB / API calls during a freeze.
            current_state = await risk_store.get(ENGINE_ID)
            if current_state and current_state.kill_switch_active:
                logger.critical(
                    "sigma.scheduler.kill_switch_active",
                    engine=ENGINE_ID,
                    reason=current_state.kill_switch_reason or "unspecified",
                )
                return RunSummary(as_of=as_of, n_candidates=0, n_submitted=0, aars=[])

            # Optional fundamentals cache for informational data-quality
            # attachment. Only enabled when a DB pool is open AND FMP_API_KEY
            # is set — otherwise candidates simply lack the optional field.
            fundamentals_provider, owned_fundamentals_adapter = (
                self._build_fundamentals_provider(pool)
            )

            setup = SigmaSetupDetection(data=data, fundamentals=fundamentals_provider)
            lifecycle = SigmaLifecycleAnalysis()
            execution = SigmaExecutionRisk()
            sigma_aar = SigmaAARLogging()
            gate = SigmaCapitalGate(engine_equity=self._engine_equity)
            order_manager = SigmaOrderManager(
                broker=broker,
                governor=governor,
                capital_gate=gate,
                lifecycle=lifecycle,
                aar=sigma_aar,
                aar_writer=aar_writer,
            )

            # 1. Reconcile first so the open-position counter is fresh
            #    before we decide on new entries. Sigma's pre-grad cap is
            #    $1,500 of $10k engine equity → 15% per trade for sizing.
            new_aars = await order_manager.reconcile(
                sizing_pct_of_engine_equity=Decimal("0.15"),
            )

            # 2. Scan for new setups.
            candidates = await setup.scan(as_of=as_of)
            logger.info("sigma.scheduler.scan_done", n_candidates=len(candidates))

            submitted = 0
            account = await broker.get_account()
            for cand in candidates:
                assessment = lifecycle.assess(cand)
                if assessment.phase is not Phase.ACTIVE:
                    continue
                state = await risk_store.get(ENGINE_ID)
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
        finally:
            if owned_fundamentals_adapter is not None:
                await owned_fundamentals_adapter.aclose()
            if pool is not None:
                await pool.close()
                logger.info("sigma.scheduler.pool_closed")

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


async def _ping_healthcheck(suffix: str = "") -> None:
    """Best-effort ping to ``HEALTHCHECKS_PING_URL`` (Healthchecks.io style).

    ``suffix`` is one of ``""`` (success), ``"/start"``, or ``"/fail"``. Any
    failure here is swallowed — monitoring outages must never affect the
    trading cycle.
    """
    url = os.getenv("HEALTHCHECKS_PING_URL")
    if not url:
        return
    import httpx

    target = url.rstrip("/") + suffix
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.get(target)
    except Exception as exc:  # pragma: no cover - network-best-effort
        logger.warning("sigma.scheduler.healthcheck_ping_failed", suffix=suffix, error=str(exc))


async def _amain() -> int:
    """Async entry for ``python -m sigma.scheduler``. Returns shell exit code."""
    equity = Decimal(os.getenv("SIGMA_ENGINE_EQUITY", "10000"))
    platform_capital = Decimal(os.getenv("PLATFORM_CAPITAL", str(equity)))

    await _ping_healthcheck("/start")
    try:
        scheduler = SigmaScheduler(engine_equity=equity, platform_capital=platform_capital)
        summary = await scheduler.run_once()
    except Exception as exc:
        logger.exception("sigma.scheduler.run_failed", error=str(exc))
        await _ping_healthcheck("/fail")
        return 1

    logger.info(
        "sigma.scheduler.summary",
        as_of=summary.as_of.isoformat(),
        n_candidates=summary.n_candidates,
        n_submitted=summary.n_submitted,
        n_aars=len(summary.aars),
    )
    await _ping_healthcheck("")  # success
    return 0


def main() -> None:  # pragma: no cover - CLI shim
    raise SystemExit(asyncio.run(_amain()))


__all__ = ["RunSummary", "SigmaScheduler", "main"]


if __name__ == "__main__":  # pragma: no cover
    main()
