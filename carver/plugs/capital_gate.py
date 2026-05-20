"""Carver — Plug 5: Capital Gate (engine-local + graduation rubric).

Engine-local guardrails:
  - ``check_rebalance(total_buy_notional_usd)`` — total buys must not
    exceed engine equity (long-only, no leverage).
  - ``check_drawdown(current_equity, peak_equity)`` — circuit breaker;
    trips at >=10% off peak.
  - ``check_trade(size, engine_pnl, open_positions=0)`` — per-trade
    oversize + position-count + daily-loss freeze (5% engine-equity).

Graduation:
  - ``is_graduated(stats)`` — pre-PAPER rubric (n_trades / win_rate /
    avg_return thresholds).
  - ``assert_can_graduate(stats, pool)`` — composes
    ``tpcore.quality.validation.capital_gate.assert_passed`` +
    ``tpcore.backtest.credibility.graduation_ready(engine_name="carver")``
    with the engine-local stats threshold; raises
    ``CredibilityScoreInsufficientError`` if a rubric run is below 60.

See ``docs/superpowers/specs/2026-05-20-carver-design.md`` Section 5
(no per-name stops between rebalances; risk via diversification + speed
limit + drawdown breaker).
"""
from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

import structlog
from pydantic import BaseModel, ConfigDict

from carver.models import (
    DAILY_LOSS_FREEZE_PCT,
    DRAWDOWN_BREAKER_LOOKBACK_DAYS,
    MAX_CONCURRENT_POSITIONS,
    PRE_GRAD_POSITION_CAP_USD,
)
from tpcore.backtest.credibility import (
    CredibilityScoreInsufficientError,
    graduation_ready,
)
from tpcore.interfaces.engine_plug import BaseEnginePlug
from tpcore.quality.validation.capital_gate import assert_passed

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)

# Drawdown circuit breaker — pause new rebalances when portfolio is more
# than this much below its rolling-window peak. Lookback window from
# carver.models (365 calendar days; spec Section 4.3).
DRAWDOWN_BREAKER_THRESHOLD = Decimal("0.10")  # 10% off peak

# Graduation thresholds — looser than per-trade engines because carver
# accrues fewer trade-events per unit time (monthly rebalances; speed
# limit caps at 12 flips/instrument/year). See spec Section 5.
GRAD_MIN_TRADES = 24            # >=2 years at 12 instruments/year
GRAD_MIN_WIN_RATE = 0.55
GRAD_MIN_AVG_RETURN = 0.03


class CarverGraduationStats(BaseModel):
    """Carver paper->live graduation rubric stats.

    Carver uses the same 3-field shape as Sigma/Vector
    (``tpcore.models.graduation.PerTradeGraduationStats``) but lives
    locally for engine-internal threshold ownership."""

    model_config = ConfigDict(extra="forbid")

    n_trades: int = 0
    win_rate: float = 0.0
    avg_return: float = 0.0


class CarverCapitalGate(BaseEnginePlug):
    """Plug 5 of Carver — engine-local + graduation rubric composition."""

    engine_name = "carver"

    def __init__(
        self,
        *,
        engine_equity_usd: Decimal = Decimal("10000"),
        max_position_usd: Decimal = PRE_GRAD_POSITION_CAP_USD,
        max_positions: int = MAX_CONCURRENT_POSITIONS,
    ) -> None:
        self._engine_equity = engine_equity_usd
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
                "grad_min_trades": GRAD_MIN_TRADES,
                "grad_min_win_rate": GRAD_MIN_WIN_RATE,
                "grad_min_avg_return": GRAD_MIN_AVG_RETURN,
            },
        }

    def check_rebalance(self, total_buy_notional_usd: Decimal) -> bool:
        """Total buy notional must be positive and within engine equity."""
        if total_buy_notional_usd <= 0:
            return False
        if total_buy_notional_usd > self._engine_equity:
            logger.warning(
                "carver.gate.reject_oversize",
                buy_notional=str(total_buy_notional_usd),
                equity=str(self._engine_equity),
            )
            return False
        return True

    def check_trade(
        self,
        *,
        size: Decimal,
        engine_pnl: Decimal,
        open_positions: int = 0,
    ) -> bool:
        """Per-trade gate (oversize / position-count / daily-loss freeze)."""
        if size <= 0:
            return False
        if size > self._max_position_usd:
            logger.warning(
                "carver.gate.reject_oversize",
                size=str(size), cap=str(self._max_position_usd),
            )
            return False
        if open_positions >= self._max_positions:
            logger.warning(
                "carver.gate.reject_position_count",
                open=open_positions, cap=self._max_positions,
            )
            return False
        if self._engine_equity > 0:
            drawdown_pct = engine_pnl / self._engine_equity
            if drawdown_pct <= -DAILY_LOSS_FREEZE_PCT:
                logger.warning(
                    "carver.gate.reject_daily_loss_freeze",
                    drawdown_pct=str(drawdown_pct),
                    threshold=str(DAILY_LOSS_FREEZE_PCT),
                )
                return False
        return True

    @staticmethod
    def check_drawdown(
        current_equity: Decimal | float | None,
        peak_equity: Decimal | float | None,
        threshold: Decimal = DRAWDOWN_BREAKER_THRESHOLD,
    ) -> bool:
        """Return True if rebalancing is allowed (no breaker trip).

        The breaker trips when (peak - current) / peak >= threshold."""
        if current_equity is None or peak_equity is None:
            return True
        c = Decimal(str(current_equity))
        p = Decimal(str(peak_equity))
        if p <= 0 or c <= 0:
            return True
        drawdown = (p - c) / p
        if drawdown >= threshold:
            logger.warning(
                "carver.gate.drawdown_breaker_tripped",
                current_equity=str(c), peak_equity=str(p),
                drawdown_pct=f"{float(drawdown)*100:.2f}",
                threshold_pct=f"{float(threshold)*100:.2f}",
            )
            return False
        return True

    @staticmethod
    def is_graduated(stats: CarverGraduationStats) -> bool:
        return (
            stats.n_trades >= GRAD_MIN_TRADES
            and stats.win_rate >= GRAD_MIN_WIN_RATE
            and stats.avg_return >= GRAD_MIN_AVG_RETURN
        )

    @classmethod
    async def assert_can_graduate(
        cls,
        *,
        stats: CarverGraduationStats,
        pool: asyncpg.Pool | None,
    ) -> bool:
        """Combined paper->live gate.

        Returns False (without raising) if the engine-local stats
        thresholds aren't met. Otherwise enforces:
          - Data Validation Suite freshness via ``assert_passed`` (raises
            on failure)
          - credibility rubric >= 60 via
            ``graduation_ready(engine_name="carver")`` (raises
            ``CredibilityScoreInsufficientError`` if absent/insufficient).
        """
        if not cls.is_graduated(stats):
            return False
        await assert_passed(pool)
        if not await graduation_ready(pool, engine_name=cls.engine_name):
            raise CredibilityScoreInsufficientError(
                f"{cls.engine_name.capitalize()} backtest credibility score "
                "< 60 (or no rubric run on record)"
            )
        return True


__all__ = [
    "DAILY_LOSS_FREEZE_PCT",
    "DRAWDOWN_BREAKER_LOOKBACK_DAYS",
    "DRAWDOWN_BREAKER_THRESHOLD",
    "GRAD_MIN_AVG_RETURN",
    "GRAD_MIN_TRADES",
    "GRAD_MIN_WIN_RATE",
    "CarverCapitalGate",
    "CarverGraduationStats",
]
