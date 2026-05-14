"""Shared graduation-statistics models for per-trade engines.

Sigma + Vector use ``PerTradeGraduationStats`` directly. Reversion
subclasses it (adding ``profit_factor``) so the engine-specific
``reversion.plugs.capital_gate.GraduationStats`` keeps working.

Momentum has a different metric set (n_rebalances + Sharpe + PF) and
keeps its own ``MomentumGraduationStats`` in ``momentum.plugs.capital_gate``.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class PerTradeGraduationStats(BaseModel):
    """Per-trade engine graduation rubric.

    The shared 3-field shape Sigma and Vector use; Reversion subclasses
    this to add ``profit_factor``. Defaults are zero so an engine that
    hasn't traded yet returns a clean "not graduated" without raising.
    """

    model_config = ConfigDict(extra="forbid")

    n_trades: int = 0
    win_rate: float = 0.0
    avg_return: float = 0.0


__all__ = ["PerTradeGraduationStats"]
