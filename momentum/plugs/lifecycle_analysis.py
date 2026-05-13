"""Momentum — Plug 2: Lifecycle Analysis.

Decides whether ``as_of`` is a rebalance day. Momentum rebalances monthly,
on the *first trading day of each calendar month*. The rationale: scanning
on the last trading day of the prior month is the natural academic
construction, but submitting orders into the close is operationally fraught
— we'd compete with index-fund close prints and burn liquidity. Instead,
we measure scores using the prior month-end close (one trading day before)
and *submit orders at the open* of the first session of the new month.

This plug is intentionally tiny: no per-position lifecycle phases (Momentum
holds to next rebalance, full stop), no early exits, no drawdown circuit
breaker yet (Phase 2.5).
"""
from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import structlog

from momentum.models import RebalancePlan
from tpcore.calendar import first_session_of_month
from tpcore.interfaces.engine_plug import BaseEnginePlug

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)


class MomentumLifecycleAnalysis(BaseEnginePlug):
    """Plug 2 of Momentum."""

    engine_name = "momentum"

    def validate_dependencies(self) -> bool:
        return True

    def healthcheck(self) -> dict:
        return {
            "engine": self.engine_name,
            "plug": "lifecycle_analysis",
            "ok": True,
            "details": {"cadence": "monthly_first_trading_day"},
        }

    async def assess(self, pool: asyncpg.Pool, as_of: date) -> RebalancePlan:
        """Return a :class:`RebalancePlan` for ``as_of``.

        Rebalance fires when ``as_of`` is the first NYSE trading session of
        its calendar month. Source of truth is ``tpcore.calendar``
        (exchange_calendars XNYS) — per CLAUDE.md, every session question
        in the platform routes through there. ``pool`` is unused but kept
        in the signature for plug parity."""
        del pool  # not needed — tpcore.calendar is the calendar source
        first = first_session_of_month(as_of.year, as_of.month)
        is_today = first == as_of
        reason = (
            "first trading day of the month"
            if is_today
            else f"first trading day was {first.isoformat()}; today is mid-month"
        )
        logger.info(
            "momentum.lifecycle.assess",
            as_of=as_of.isoformat(),
            is_rebalance_day=is_today,
            first_in_month=first.isoformat(),
        )
        return RebalancePlan(as_of=as_of, is_rebalance_day=is_today, reason=reason)
