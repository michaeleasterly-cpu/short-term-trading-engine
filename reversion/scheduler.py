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
from datetime import UTC, date as date_t, datetime
from decimal import Decimal
from typing import Any

import structlog

from tpcore.aar.models import AfterActionReport
from tpcore.aar.writer import AARWriter
from tpcore.alpaca import AlpacaDataAdapter, AlpacaPaperBrokerAdapter
from tpcore.db import build_asyncpg_pool
from tpcore.fmp import FMPFundamentalsAdapter
from tpcore.outage import DataProviderOutage
from tpcore.risk.governor import (
    InMemoryRiskStateStore,
    RiskGovernor,
    RiskStateStore,
)
from tpcore.risk.persistent_store import PostgresRiskStateStore

from reversion.models import Phase
from reversion.order_manager import ENGINE_ID, ReversionOrderManager
from reversion.plugs.aar_logging import ReversionAARLogging
from reversion.plugs.capital_gate import ReversionCapitalGate
from reversion.plugs.execution_risk import ReversionExecutionRisk
from reversion.plugs.lifecycle_analysis import ReversionLifecycleAnalysis
from reversion.plugs.setup_detection import ReversionSetupDetection

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
        data: AlpacaDataAdapter | None = None,
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
            logger.info(
                "reversion.scheduler.run_start",
                as_of=as_of.isoformat(),
                allow_shorts=self._allow_shorts,
                persistent=pool is not None,
            )

            setup = ReversionSetupDetection(data=data)
            lifecycle = ReversionLifecycleAnalysis()
            execution = ReversionExecutionRisk()
            rev_aar = ReversionAARLogging()
            gate = ReversionCapitalGate(engine_equity=self._engine_equity)
            order_manager = ReversionOrderManager(
                broker=broker,
                governor=governor,
                capital_gate=gate,
                lifecycle=lifecycle,
                aar=rev_aar,
                aar_writer=aar_writer,
            )

            new_aars = await order_manager.reconcile(
                sizing_pct_of_engine_equity=Decimal("0.20"),
            )

            candidates = await setup.scan(as_of=as_of)
            logger.info("reversion.scheduler.scan_done", n_candidates=len(candidates))

            # Fundamentals adapter — required for the earnings-quality gate
            # per §4.2. Built lazily so a missing FMP_API_KEY in tests/dev
            # doesn't crash the import path; the adapter constructor itself
            # raises DataProviderOutage on missing key.
            fundamentals_adapter = self._injected_fundamentals
            owned_fundamentals = False
            if fundamentals_adapter is None:
                try:
                    fundamentals_adapter = FMPFundamentalsAdapter()
                    owned_fundamentals = True
                except DataProviderOutage as exc:
                    logger.warning(
                        "reversion.scheduler.fundamentals_unavailable",
                        error=str(exc),
                        note="every candidate will be blocked by the earnings-quality gate",
                    )
                    fundamentals_adapter = None

            submitted = 0
            account = await broker.get_account()
            try:
                for cand in candidates:
                    fundamentals = await self._fetch_fundamentals(
                        fundamentals_adapter, cand.ticker, cand.as_of
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
                if owned_fundamentals and fundamentals_adapter is not None:
                    await fundamentals_adapter.aclose()

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
    async def _fetch_fundamentals(
        adapter: FMPFundamentalsAdapter | None, symbol: str, as_of: date_t
    ) -> dict | None:
        """Pull fundamentals for ``symbol``, returning ``None`` on outage.

        ``DataProviderOutage`` from the adapter is logged and swallowed —
        a single bad symbol shouldn't kill the whole scan. The lifecycle
        plug treats ``None`` as "no data, no trade" and blocks just that
        candidate.
        """
        if adapter is None:
            return None
        try:
            return await adapter.get_quarterly_fundamentals(symbol, as_of_date=as_of)
        except DataProviderOutage as exc:
            logger.warning(
                "reversion.scheduler.fundamentals_outage",
                symbol=symbol,
                error=str(exc),
            )
            return None


async def _ping_healthcheck(suffix: str = "") -> None:
    """Best-effort ping to ``HEALTHCHECKS_PING_URL`` (Healthchecks.io style)."""
    url = os.getenv("HEALTHCHECKS_PING_URL_REVERSION") or os.getenv("HEALTHCHECKS_PING_URL")
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
