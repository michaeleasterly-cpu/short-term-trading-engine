"""Reversion — Plug 5: Capital Gate.

Engine-local guardrail per plan §4.2:

* Pre-graduation hard cap per position: $2,000.
* Max concurrent positions: 5.
* Daily loss kill (mirrors RiskGovernor): freeze on −5% engine-equity drawdown.
* Graduation gate (paper → live): 10 trades, win-rate ≥ 55%, avg return ≥ 2%, profit factor > 1.5.

The platform-wide :class:`tpcore.risk.RiskGovernor` runs **after** this gate;
both must approve a trade. Graduation also requires the Data Validation
Suite (`tpcore.quality.validation.assert_passed`) to have a recent passing
run — see :meth:`ReversionCapitalGate.assert_can_graduate`.

Graduation criteria were tightened on the *quality* side and loosened on
the *count* side after the 2018–2025 backtest under the combined-filter
config (z≥3.0, EQ=HIGH) showed Reversion fires only ~1–2 times/year. The
old 30-trade bar would have required ~20 years to clear; 10 trades + the
profit-factor floor balances statistical confidence against the engine's
natural firing rate.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

import structlog

from tpcore.backtest.credibility import (
    CredibilityScoreInsufficientError,
    graduation_ready,
)
from tpcore.interfaces.engine_plug import BaseEnginePlug
from tpcore.quality.validation.capital_gate import assert_passed

from reversion.models import (
    MAX_CONCURRENT_POSITIONS,
    PRE_GRAD_POSITION_CAP_USD,
)

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)

DAILY_LOSS_FREEZE_PCT = Decimal("0.05")
GRAD_MIN_TRADES = 10
GRAD_MIN_WIN_RATE = 0.55
GRAD_MIN_AVG_RETURN = 0.02
GRAD_MIN_PROFIT_FACTOR = 1.5


@dataclass
class GraduationStats:
    n_trades: int = 0
    win_rate: float = 0.0
    avg_return: float = 0.0
    profit_factor: float = 0.0


class ReversionCapitalGate(BaseEnginePlug):
    """Plug 5 of Reversion."""

    engine_name = "reversion"

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
            logger.info("reversion.gate.reject_nonpositive", size=str(size))
            return False
        if size > self._max_position_usd:
            logger.info(
                "reversion.gate.reject_oversize",
                size=str(size),
                cap=str(self._max_position_usd),
            )
            return False
        if open_positions >= self._max_positions:
            logger.info(
                "reversion.gate.reject_position_count",
                open_positions=open_positions,
                cap=self._max_positions,
            )
            return False
        if self._engine_equity > 0:
            drawdown_pct = engine_pnl / self._engine_equity
            if drawdown_pct <= -DAILY_LOSS_FREEZE_PCT:
                logger.warning(
                    "reversion.gate.reject_daily_loss",
                    drawdown_pct=float(drawdown_pct),
                    threshold=float(-DAILY_LOSS_FREEZE_PCT),
                )
                return False
        return True

    @staticmethod
    def is_graduated(stats: GraduationStats) -> bool:
        """Reversion graduates from paper to live iff plan §4.2 thresholds met."""
        return (
            stats.n_trades >= GRAD_MIN_TRADES
            and stats.win_rate >= GRAD_MIN_WIN_RATE
            and stats.avg_return >= GRAD_MIN_AVG_RETURN
            and stats.profit_factor >= GRAD_MIN_PROFIT_FACTOR
        )

    @staticmethod
    async def assert_can_graduate(stats: GraduationStats, pool: "asyncpg.Pool") -> bool:
        """Combined gate: stats thresholds AND Data Validation Suite AND credibility ≥ 60.

        Returns ``False`` (without raising) if the stats thresholds aren't met
        (normal pre-grad case). Returns ``True`` only after a fresh successful
        validation run *and* a credibility-rubric score ≥ 60 in
        ``platform.data_quality_log``. Raises ``ValidationStaleError`` or
        ``ValidationFailedError`` if the data gate isn't satisfied; raises
        ``CredibilityScoreInsufficientError`` if the latest backtest
        credibility row is < 60 or absent.
        """
        if not ReversionCapitalGate.is_graduated(stats):
            return False
        await assert_passed(pool)
        if not await graduation_ready(pool, engine_name="reversion"):
            raise CredibilityScoreInsufficientError(
                "Reversion backtest credibility score < 60 (or no rubric run on record)"
            )
        return True
