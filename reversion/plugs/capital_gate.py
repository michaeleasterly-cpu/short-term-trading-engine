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

from decimal import Decimal

from reversion.models import (
    MAX_CONCURRENT_POSITIONS,
    PRE_GRAD_POSITION_CAP_USD,
)
from tpcore.interfaces.capital_gate_base import PerTradeCapitalGateBase

# Reversion's GraduationStats subclasses the shared
# PerTradeGraduationStats (n_trades / win_rate / avg_return) and adds
# profit_factor. Refactored 2026-05-14 to consolidate the shared fields
# in tpcore.models.graduation.
from tpcore.models.graduation import PerTradeGraduationStats

DAILY_LOSS_FREEZE_PCT = Decimal("0.05")
GRAD_MIN_TRADES = 10
GRAD_MIN_WIN_RATE = 0.55
GRAD_MIN_AVG_RETURN = 0.02
GRAD_MIN_PROFIT_FACTOR = 1.5


class GraduationStats(PerTradeGraduationStats):
    """Reversion's graduation rubric — shared per-trade fields plus PF."""

    profit_factor: float = 0.0


class ReversionCapitalGate(PerTradeCapitalGateBase):
    """Plug 5 of Reversion.

    Lean P5.5a/c: ``check_trade`` / ``healthcheck`` /
    ``assert_can_graduate`` are the consolidated
    :class:`PerTradeCapitalGateBase` implementations (cluster #3/#4/#7);
    only ``is_graduated`` (the reversion-specific thresholds incl. the
    profit-factor floor) stays engine-owned. The P5.5a ``_legacy_*``
    parallel-diff scaffolding was retired at the staged cutover
    (plan P5.5c) once byte-equivalence was locked in CI.
    """

    engine_name = "reversion"
    _daily_loss_freeze_pct = DAILY_LOSS_FREEZE_PCT

    def __init__(
        self,
        engine_equity: Decimal = Decimal("10000"),
        max_position_usd: Decimal = PRE_GRAD_POSITION_CAP_USD,
        max_positions: int = MAX_CONCURRENT_POSITIONS,
    ) -> None:
        super().__init__(engine_equity, max_position_usd, max_positions)

    @staticmethod
    def is_graduated(stats: GraduationStats) -> bool:
        """Reversion graduates from paper to live iff plan §4.2 thresholds met."""
        return (
            stats.n_trades >= GRAD_MIN_TRADES
            and stats.win_rate >= GRAD_MIN_WIN_RATE
            and stats.avg_return >= GRAD_MIN_AVG_RETURN
            and stats.profit_factor >= GRAD_MIN_PROFIT_FACTOR
        )
