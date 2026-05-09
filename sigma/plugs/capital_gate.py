"""Sigma — Plug 5: Capital Gate.

Engine-local guardrail. Plan §4.1 limits:

* Pre-graduation hard cap per position: $1,500.
* Max concurrent positions: 4.
* Daily loss kill (mirrors RiskGovernor): freeze on −5% engine-equity drawdown.
* Graduation gate (paper → live): 50 trades, win-rate ≥ 65%, avg return ≥ 1.5%.

The platform-wide :class:`tpcore.risk.RiskGovernor` runs **after** this gate;
both must approve a trade.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import structlog

from tpcore.interfaces.engine_plug import BaseEnginePlug

from sigma.models import (
    MAX_CONCURRENT_POSITIONS,
    PRE_GRAD_POSITION_CAP_USD,
)

logger = structlog.get_logger(__name__)

DAILY_LOSS_FREEZE_PCT = Decimal("0.05")
GRAD_MIN_TRADES = 50
GRAD_MIN_WIN_RATE = 0.65
GRAD_MIN_AVG_RETURN = 0.015


@dataclass
class GraduationStats:
    n_trades: int = 0
    win_rate: float = 0.0
    avg_return: float = 0.0


class SigmaCapitalGate(BaseEnginePlug):
    """Plug 5 of Sigma."""

    engine_name = "sigma"

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
        """Return True iff the proposed trade obeys engine-local limits.

        ``size`` is the proposed *notional* in USD.
        ``engine_pnl`` is today's running engine P&L in USD (signed).
        """
        if size <= 0:
            logger.info("sigma.gate.reject_nonpositive", size=str(size))
            return False
        if size > self._max_position_usd:
            logger.info(
                "sigma.gate.reject_oversize",
                size=str(size),
                cap=str(self._max_position_usd),
            )
            return False
        if open_positions >= self._max_positions:
            logger.info(
                "sigma.gate.reject_position_count",
                open_positions=open_positions,
                cap=self._max_positions,
            )
            return False
        # Convert engine_pnl to a fraction of equity and freeze if below threshold.
        if self._engine_equity > 0:
            drawdown_pct = engine_pnl / self._engine_equity
            if drawdown_pct <= -DAILY_LOSS_FREEZE_PCT:
                logger.warning(
                    "sigma.gate.reject_daily_loss",
                    drawdown_pct=float(drawdown_pct),
                    threshold=float(-DAILY_LOSS_FREEZE_PCT),
                )
                return False
        return True

    @staticmethod
    def is_graduated(stats: GraduationStats) -> bool:
        """Sigma graduates from paper to live iff plan §4.1 thresholds are all met."""
        return (
            stats.n_trades >= GRAD_MIN_TRADES
            and stats.win_rate >= GRAD_MIN_WIN_RATE
            and stats.avg_return >= GRAD_MIN_AVG_RETURN
        )
