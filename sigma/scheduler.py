"""Sigma scheduler — daily entry point.

Wires the five plugs, broker adapter, data adapter, and risk governor into
a single ``run_once`` invocation that an external scheduler (cron,
systemd timer, GitHub Actions, etc.) can call.

Responsibilities each run:
    1. Reconcile open trades with the broker — fire Tier 1 / Tier 2 / hard-stop
       events and append any AARs (idempotent across runs).
    2. Run setup detection on the configured universe.
    3. For every ACTIVE-phase candidate, build an ``ExecutionDecision`` and
       hand it to ``SigmaOrderManager``, which gates → governs → submits.

Calling cadence: per ``MASTER_PLAN.md §4.1`` Sigma is a daily-timeframe
strategy. The scheduler is meant to fire once per session — typically a few
minutes before close (``tpcore.calendar.next_close`` minus a buffer) so
fresh closing-context bars are in. Intra-day fills are picked up on the
*next* run; that's a deliberate trade-off — Sigma's lifecycle is days,
not minutes.
"""
from __future__ import annotations

import asyncio
import os
from datetime import UTC, date as date_t, datetime
from decimal import Decimal

import structlog

from tpcore.alpaca import AlpacaDataAdapter, AlpacaPaperBrokerAdapter
from tpcore.aar.models import AfterActionReport
from tpcore.risk.governor import (
    InMemoryRiskStateStore,
    RiskGovernor,
    RiskStateStore,
)

from sigma.models import Phase
from sigma.order_manager import ENGINE_ID, SigmaOrderManager
from sigma.plugs.aar_logging import SigmaAARLogging
from sigma.plugs.capital_gate import SigmaCapitalGate
from sigma.plugs.execution_risk import SigmaExecutionRisk
from sigma.plugs.lifecycle_analysis import SigmaLifecycleAnalysis
from sigma.plugs.setup_detection import SigmaSetupDetection

logger = structlog.get_logger(__name__)


class SigmaScheduler:
    """One-shot orchestration of a full Sigma trading cycle."""

    def __init__(
        self,
        *,
        engine_equity: Decimal = Decimal("10000"),
        platform_capital: Decimal = Decimal("10000"),
        risk_store: RiskStateStore | None = None,
        broker: AlpacaPaperBrokerAdapter | None = None,
        data: AlpacaDataAdapter | None = None,
    ) -> None:
        self._engine_equity = engine_equity
        self._broker = broker or AlpacaPaperBrokerAdapter()
        self._data = data or AlpacaDataAdapter()
        self._risk_store = risk_store or InMemoryRiskStateStore()
        self._governor = RiskGovernor(
            state_store=self._risk_store,
            broker=self._broker,
            platform_capital=platform_capital,
        )
        self._setup = SigmaSetupDetection(data=self._data)
        self._lifecycle = SigmaLifecycleAnalysis()
        self._execution = SigmaExecutionRisk()
        self._aar = SigmaAARLogging()
        self._gate = SigmaCapitalGate(engine_equity=engine_equity)
        self._order_manager = SigmaOrderManager(
            broker=self._broker,
            governor=self._governor,
            capital_gate=self._gate,
            lifecycle=self._lifecycle,
            aar=self._aar,
        )

    async def run_once(self, *, as_of: date_t | None = None) -> RunSummary:
        """Execute one full cycle. Returns a small summary for the caller."""
        as_of = as_of or datetime.now(UTC).date()
        await self._governor.register_engine(ENGINE_ID, self._engine_equity)
        logger.info("sigma.scheduler.run_start", as_of=as_of.isoformat())

        # 1. Reconcile open trades first so the open-position count is fresh
        #    before we decide on new entries. Sigma's pre-grad cap is $1,500
        #    out of $10k engine equity → 15% per trade for sizing reporting.
        new_aars = await self._order_manager.reconcile(
            sizing_pct_of_engine_equity=Decimal("0.15"),
        )

        # 2. Scan for new setups.
        candidates = await self._setup.scan(as_of=as_of)
        logger.info("sigma.scheduler.scan_done", n_candidates=len(candidates))

        submitted = 0
        account = await self._broker.get_account()
        for cand in candidates:
            assessment = self._lifecycle.assess(cand)
            if assessment.phase is not Phase.ACTIVE:
                continue
            risk_state = await self._risk_store.get(ENGINE_ID)
            open_positions = risk_state.open_positions if risk_state else 0
            decision = self._execution.decide(
                assessment,
                account_capital=account.equity,
                open_positions=open_positions,
            )
            if decision is None:
                continue
            placed = await self._order_manager.submit_decision(decision, assessment)
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
