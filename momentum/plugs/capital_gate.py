"""Momentum — Plug 4: Capital Gate.

Engine-local guardrail. Looser graduation criteria than Sigma/Reversion/
Vector because monthly rebalance accrues fewer trade-events per unit time
— 6 rebalances = 6 months of paper trading at this cadence.

Limits:

* **Pre-graduation total notional cap**: ``engine_equity_usd`` (defaults to
  10k). The portfolio fills equity to ~100% long since this is long-only.
* **Per-name cap**: enforced in ExecutionRisk via ``PER_NAME_CAP_PCT``.
* **Drawdown freeze** (Phase 2.5): pause new rebalances when portfolio is
  >10% off rolling peak. Not implemented in Phase 2 MVP.

Graduation (paper → live) requires three things:
1. ``stats.n_rebalances >= GRAD_MIN_REBALANCES`` (6 monthly cycles ≈ 6 months).
2. ``stats.sharpe_annualized >= GRAD_MIN_SHARPE`` (1.0).
3. Data Validation Suite + credibility rubric — same shared infrastructure
   the other engines use.
"""
from __future__ import annotations

import os
from decimal import Decimal
from typing import TYPE_CHECKING

import structlog
from pydantic import BaseModel, ConfigDict

from momentum.models import (
    GRAD_MIN_PROFIT_FACTOR,
    GRAD_MIN_REBALANCES,
    GRAD_MIN_SHARPE,
)
from tpcore.backtest.credibility import (
    CredibilityScoreInsufficientError,
    graduation_ready,
)
from tpcore.interfaces.engine_plug import BaseEnginePlug
from tpcore.quality.validation.capital_gate import assert_passed_for_engine

# Drawdown circuit breaker — pause new rebalances when portfolio is more
# than this much below its rolling-window peak. Lookback window is the
# last 60 calendar days (longer than monthly rebalance cadence; captures
# the prior peak even if the engine has skipped a month).
DRAWDOWN_BREAKER_THRESHOLD = Decimal("0.10")  # 10% off peak
DRAWDOWN_BREAKER_LOOKBACK_DAYS = 60

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)


class MomentumGraduationStats(BaseModel):
    model_config = ConfigDict(extra="forbid")

    n_rebalances: int = 0
    sharpe_annualized: float = 0.0
    profit_factor: float = 0.0


class MomentumCapitalGate(BaseEnginePlug):
    """Plug 4 of Momentum."""

    engine_name = "momentum"

    def __init__(self, engine_equity_usd: Decimal = Decimal("10000")) -> None:
        self._engine_equity = engine_equity_usd

    def validate_dependencies(self) -> bool:
        return True

    def healthcheck(self) -> dict:
        return {
            "engine": self.engine_name,
            "plug": "capital_gate",
            "ok": True,
            "details": {
                "engine_equity_usd": str(self._engine_equity),
                "grad_min_rebalances": GRAD_MIN_REBALANCES,
                "grad_min_sharpe": GRAD_MIN_SHARPE,
                "grad_min_profit_factor": GRAD_MIN_PROFIT_FACTOR,
            },
        }

    def check_rebalance(self, total_buy_notional_usd: Decimal) -> bool:
        """Engine-local sanity: the proposed buy-side notional must not exceed
        the engine's allocated equity. Long-only, no leverage."""
        if total_buy_notional_usd <= 0:
            return False
        if total_buy_notional_usd > self._engine_equity:
            logger.warning(
                "momentum.gate.reject_oversize",
                buy_notional=str(total_buy_notional_usd),
                equity=str(self._engine_equity),
            )
            return False
        return True

    @staticmethod
    def check_drawdown(
        current_equity: Decimal | float | None,
        peak_equity: Decimal | float | None,
        threshold: Decimal = DRAWDOWN_BREAKER_THRESHOLD,
    ) -> bool:
        """Return ``True`` if rebalancing is allowed (no breaker trip).

        The breaker trips when ``(peak - current) / peak ≥ threshold`` — i.e.,
        portfolio is down by ``threshold`` or more from its rolling peak.
        Returns ``True`` (allow) when either input is missing or zero —
        first run / no prior equity history is not a reason to halt.
        Pure function so the logic is unit-testable without a DB."""
        if current_equity is None or peak_equity is None:
            return True
        c = Decimal(str(current_equity))
        p = Decimal(str(peak_equity))
        if p <= 0 or c <= 0:
            return True
        drawdown = (p - c) / p
        if drawdown >= threshold:
            logger.warning(
                "momentum.gate.drawdown_breaker_tripped",
                current_equity=str(c), peak_equity=str(p),
                drawdown_pct=f"{float(drawdown)*100:.2f}",
                threshold_pct=f"{float(threshold)*100:.2f}",
            )
            return False
        return True

    @staticmethod
    def is_graduated(stats: MomentumGraduationStats) -> bool:
        return (
            stats.n_rebalances >= GRAD_MIN_REBALANCES
            and stats.sharpe_annualized >= GRAD_MIN_SHARPE
            and stats.profit_factor >= GRAD_MIN_PROFIT_FACTOR
        )

    @staticmethod
    async def assert_can_graduate(
        stats: MomentumGraduationStats, pool: asyncpg.Pool,
    ) -> bool:
        """Combined gate: stats AND Data Validation Suite AND credibility ≥ 60."""
        if not MomentumCapitalGate.is_graduated(stats):
            return False
        await assert_passed_for_engine(
            pool, "momentum",
            require_all_green=os.getenv(
                "CAPITAL_GATE_REQUIRE_ALL_GREEN", "").strip().lower()
            in ("1", "true", "yes", "on"),
        )
        if not await graduation_ready(pool, engine_name="momentum"):
            raise CredibilityScoreInsufficientError(
                "Momentum backtest credibility score < 60 (or no rubric row on record)"
            )
        return True
