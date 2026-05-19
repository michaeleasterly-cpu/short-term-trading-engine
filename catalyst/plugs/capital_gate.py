"""Catalyst — Plug 5: Capital Gate.

Per-trade engine-local capital gate (per-position cap + max-concurrent
+ daily-loss freeze) and the graduation check
(:meth:`assert_can_graduate`). The graduation pipeline is the canonical
3-stage composition: per-trade stats AND fresh validation-suite pass AND
backtest credibility ≥ 60 (read from ``platform.data_quality_log`` via
``tpcore.backtest.credibility.graduation_ready``).
"""
from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

import structlog

from catalyst.models import (
    DAILY_LOSS_FREEZE_PCT,
    GRAD_MIN_AVG_RETURN,
    GRAD_MIN_TRADES,
    GRAD_MIN_WIN_RATE,
    MAX_CONCURRENT_POSITIONS,
    PRE_GRAD_POSITION_CAP_USD,
)
from tpcore.backtest.credibility import (
    CredibilityScoreInsufficientError,
    graduation_ready,
)
from tpcore.interfaces.engine_plug import BaseEnginePlug
from tpcore.models.graduation import PerTradeGraduationStats as GraduationStats
from tpcore.quality.validation.capital_gate import assert_passed

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)


class CatalystCapitalGate(BaseEnginePlug):
    """Plug 5 — engine-local guardrail + graduation gate."""

    engine_name = "catalyst"

    def __init__(
        self,
        engine_equity: Decimal = Decimal("10000"),
        max_position_usd: Decimal = PRE_GRAD_POSITION_CAP_USD,
        max_positions: int = MAX_CONCURRENT_POSITIONS,
    ) -> None:
        self._engine_equity = engine_equity
        self._max_position_usd = max_position_usd
        self._max_positions = max_positions

    def validate_dependencies(self) -> bool:
        return (
            self._engine_equity >= 0
            and self._max_position_usd > 0
            and self._max_positions > 0
        )

    def healthcheck(self) -> dict:
        return {
            "engine": self.engine_name,
            "plug": "capital_gate",
            "ok": True,
            "details": {
                "engine_equity": str(self._engine_equity),
                "max_position_usd": str(self._max_position_usd),
                "max_positions": self._max_positions,
            },
        }

    def check_trade(
        self,
        *,
        size: Decimal,
        engine_pnl: Decimal,
        open_positions: int = 0,
    ) -> bool:
        """Return True iff the proposed trade respects every local rule."""
        if size <= 0 or size > self._max_position_usd:
            return False
        if open_positions >= self._max_positions:
            return False
        if self._engine_equity > 0:
            drawdown_pct = engine_pnl / self._engine_equity
            if drawdown_pct <= -DAILY_LOSS_FREEZE_PCT:
                return False
        return True

    @staticmethod
    def is_graduated(stats: GraduationStats) -> bool:
        return (
            stats.n_trades >= GRAD_MIN_TRADES
            and stats.win_rate >= GRAD_MIN_WIN_RATE
            and stats.avg_return >= GRAD_MIN_AVG_RETURN
        )

    @classmethod
    async def assert_can_graduate(
        cls, stats: GraduationStats, pool: asyncpg.Pool,
    ) -> bool:
        """Three-stage graduation: per-trade stats AND fresh validation
        AND credibility ≥ 60. Raises on validation failure; raises
        :class:`CredibilityScoreInsufficientError` on missing/low rubric.
        """
        if not cls.is_graduated(stats):
            return False
        await assert_passed(pool)
        if not await graduation_ready(pool, engine_name="catalyst"):
            raise CredibilityScoreInsufficientError(
                "catalyst backtest credibility score < 60 (or no rubric "
                "run on record)"
            )
        return True


__all__ = ["CatalystCapitalGate"]
