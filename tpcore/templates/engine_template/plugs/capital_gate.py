"""Plug 5: Capital Gate — engine-local per-trade + daily-loss guard.

Composition: ``assert_can_graduate`` requires stats thresholds AND a
fresh Data Validation Suite pass AND a backtest credibility score ≥ 60
in ``platform.data_quality_log``. The platform-wide
:class:`tpcore.risk.RiskGovernor` runs **after** this gate.

Per-trade engines use :class:`tpcore.models.graduation.PerTradeGraduationStats`
(n_trades / win_rate / avg_return) directly or subclass it to add fields
(e.g. Reversion adds ``profit_factor``).
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
from tpcore.models.graduation import PerTradeGraduationStats as GraduationStats  # noqa: F401
from tpcore.quality.validation.capital_gate import assert_passed

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)

# TODO: wire to engine spec.
DAILY_LOSS_FREEZE_PCT = Decimal("0.05")
GRAD_MIN_TRADES = 30
GRAD_MIN_WIN_RATE = 0.55
GRAD_MIN_AVG_RETURN = 0.03


class EngineNameCapitalGate(BaseEnginePlug):
    """Plug 5 — engine-local guardrail."""

    engine_name = "ENGINE_NAME"

    def __init__(
        self,
        engine_equity: Decimal = Decimal("10000"),
        max_position_usd: Decimal = Decimal("1500"),
        max_positions: int = 4,
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
            "details": {},
        }

    def check_trade(
        self,
        size: Decimal,
        engine_pnl: Decimal,
        open_positions: int = 0,
    ) -> bool:
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
    def is_graduated(stats) -> bool:
        return (
            stats.n_trades >= GRAD_MIN_TRADES
            and stats.win_rate >= GRAD_MIN_WIN_RATE
            and stats.avg_return >= GRAD_MIN_AVG_RETURN
        )

    @classmethod
    async def assert_can_graduate(cls, stats, pool: asyncpg.Pool) -> bool:
        if not cls.is_graduated(stats):
            return False
        await assert_passed(pool)
        if not await graduation_ready(pool, engine_name="ENGINE_NAME"):
            raise CredibilityScoreInsufficientError(
                "ENGINE_NAME backtest credibility score < 60 (or no rubric run on record)"
            )
        return True
