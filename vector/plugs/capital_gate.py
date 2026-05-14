"""Vector — Plug 5: Capital Gate.

Engine-local guardrail per plan §4.3:

* Pre-graduation hard cap per position: $2,000.
* Max concurrent positions: 5.
* Daily loss kill (mirrors RiskGovernor): freeze on −5% engine-equity drawdown.
* Graduation gate (paper → live): 30 trades, win-rate ≥ 55%, avg return ≥ 3%.

Same composition as Sigma + Reversion: ``assert_can_graduate`` requires
stats thresholds AND a fresh Data Validation Suite pass AND a backtest
credibility score ≥ 60 in ``platform.data_quality_log``. The platform-wide
:class:`tpcore.risk.RiskGovernor` runs **after** this gate.
"""
from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

import structlog

from tpcore.backtest.credibility import (
    CredibilityScoreInsufficientError,
    graduation_ready,
)
from tpcore.interfaces.engine_plug import BaseEnginePlug

# Vector's GraduationStats is the shared per-trade shape — moved to
# tpcore.models.graduation 2026-05-14. Re-export under the original
# name for back-compat.
from tpcore.models.graduation import PerTradeGraduationStats as GraduationStats  # noqa: F401
from tpcore.quality.validation.capital_gate import assert_passed
from vector.models import (
    DAILY_LOSS_FREEZE_PCT,
    MAX_CONCURRENT_POSITIONS,
    PRE_GRAD_POSITION_CAP_USD,
)

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)

GRAD_MIN_TRADES = 30
GRAD_MIN_WIN_RATE = 0.55
GRAD_MIN_AVG_RETURN = 0.03


class VectorCapitalGate(BaseEnginePlug):
    """Plug 5 of Vector."""

    engine_name = "vector"

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
        return True

    def healthcheck(self) -> dict:
        return {
            "engine": self.engine_name,
            "plug": "capital_gate",
            "ok": True,
            "details": {
                "engine_equity_usd": str(self._engine_equity),
                "max_position_usd": str(self._max_position_usd),
                "max_positions": self._max_positions,
            },
        }

    def check_trade(
        self,
        size: Decimal,
        engine_pnl: Decimal,
        open_positions: int = 0,
    ) -> bool:
        """Return True iff the proposed trade obeys engine-local limits."""
        if size <= 0:
            logger.info("vector.gate.reject_nonpositive", size=str(size))
            return False
        if size > self._max_position_usd:
            logger.info(
                "vector.gate.reject_oversize",
                size=str(size),
                cap=str(self._max_position_usd),
            )
            return False
        if open_positions >= self._max_positions:
            logger.info(
                "vector.gate.reject_position_count",
                open_positions=open_positions,
                cap=self._max_positions,
            )
            return False
        if self._engine_equity > 0:
            drawdown_pct = engine_pnl / self._engine_equity
            if drawdown_pct <= -DAILY_LOSS_FREEZE_PCT:
                logger.warning(
                    "vector.gate.reject_daily_loss",
                    drawdown_pct=float(drawdown_pct),
                    threshold=float(-DAILY_LOSS_FREEZE_PCT),
                )
                return False
        return True

    @staticmethod
    def is_graduated(stats: GraduationStats) -> bool:
        """Vector graduates from paper to live iff plan §4.3 thresholds met."""
        return (
            stats.n_trades >= GRAD_MIN_TRADES
            and stats.win_rate >= GRAD_MIN_WIN_RATE
            and stats.avg_return >= GRAD_MIN_AVG_RETURN
        )

    @staticmethod
    async def assert_can_graduate(stats: GraduationStats, pool: asyncpg.Pool) -> bool:
        """Combined gate: stats AND validation suite AND credibility ≥ 60."""
        if not VectorCapitalGate.is_graduated(stats):
            return False
        await assert_passed(pool)
        if not await graduation_ready(pool, engine_name="vector"):
            raise CredibilityScoreInsufficientError(
                "Vector backtest credibility score < 60 (or no rubric run on record)"
            )
        return True


__all__ = ["VectorCapitalGate", "GraduationStats", "GRAD_MIN_TRADES", "GRAD_MIN_WIN_RATE", "GRAD_MIN_AVG_RETURN"]
