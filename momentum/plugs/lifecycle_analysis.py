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

        Rebalance fires when ``as_of`` is the first trading day of its
        calendar month. "Trading day" is defined by existence of bars in
        ``platform.prices_daily``; we look at any liquid ticker (using SPY
        as the canonical session marker) and check whether any prior date
        in the same month has a bar."""
        async with pool.acquire() as conn:
            # Find the earliest trading day in the current calendar month.
            first_in_month = await conn.fetchval(
                """
                SELECT MIN(date)
                FROM platform.prices_daily
                WHERE ticker = 'SPY'
                  AND date >= make_date($1, $2, 1)
                  AND date <= $3
                """,
                as_of.year, as_of.month, as_of,
            )

        if first_in_month is None:
            # Either SPY is missing or no bars yet this month — treat as
            # "not a rebalance day" rather than crash. Operator can inspect.
            logger.warning("momentum.lifecycle.no_session_data", as_of=as_of.isoformat())
            return RebalancePlan(
                as_of=as_of,
                is_rebalance_day=False,
                reason="no prices_daily bars for this month yet",
            )

        is_today = first_in_month == as_of
        reason = (
            "first trading day of the month"
            if is_today
            else f"first trading day was {first_in_month.isoformat()}; today is mid-month"
        )
        logger.info(
            "momentum.lifecycle.assess",
            as_of=as_of.isoformat(),
            is_rebalance_day=is_today,
            first_in_month=first_in_month.isoformat(),
        )
        return RebalancePlan(as_of=as_of, is_rebalance_day=is_today, reason=reason)
