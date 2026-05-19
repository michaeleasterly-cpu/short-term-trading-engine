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

from tpcore.interfaces.capital_gate_base import PerTradeCapitalGateBase

# Vector's GraduationStats is the shared per-trade shape — moved to
# tpcore.models.graduation 2026-05-14. Re-export under the original
# name for back-compat.
from tpcore.models.graduation import PerTradeGraduationStats as GraduationStats  # noqa: F401
from vector.models import (
    DAILY_LOSS_FREEZE_PCT,
    MAX_CONCURRENT_POSITIONS,
    PRE_GRAD_POSITION_CAP_USD,
)

GRAD_MIN_TRADES = 30
GRAD_MIN_WIN_RATE = 0.55
GRAD_MIN_AVG_RETURN = 0.03


class VectorCapitalGate(PerTradeCapitalGateBase):
    """Plug 5 of Vector.

    Lean P5.5b/c: ``check_trade`` / ``healthcheck`` /
    ``assert_can_graduate`` are the consolidated
    :class:`PerTradeCapitalGateBase` implementations (cluster #3/#4/#7);
    only ``is_graduated`` (the vector-specific thresholds — no
    profit-factor floor, unlike reversion) stays engine-owned. The P5.5b
    ``_legacy_*`` parallel-diff scaffolding was retired at the staged
    cutover (plan P5.5c) once byte-equivalence was locked in CI.
    """

    engine_name = "vector"
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
        """Vector graduates from paper to live iff plan §4.3 thresholds met."""
        return (
            stats.n_trades >= GRAD_MIN_TRADES
            and stats.win_rate >= GRAD_MIN_WIN_RATE
            and stats.avg_return >= GRAD_MIN_AVG_RETURN
        )


__all__ = ["VectorCapitalGate", "GraduationStats", "GRAD_MIN_TRADES", "GRAD_MIN_WIN_RATE", "GRAD_MIN_AVG_RETURN"]
