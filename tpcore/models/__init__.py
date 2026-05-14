"""Shared Pydantic models used across engines.

Engine-specific models (SetupCandidate, PhaseAssessment, ExecutionDecision)
stay in each engine's ``models.py`` because their shapes differ. This
package is for genuinely-shared models the per-trade engines have
duplicated.

Today's residents:

* :class:`PerTradeGraduationStats` — the canonical per-trade engine
  graduation rubric (``n_trades``, ``win_rate``, ``avg_return``).
  Sigma + Vector use this directly; Reversion extends it with
  ``profit_factor`` in :class:`reversion.plugs.capital_gate.GraduationStats`.
  Momentum has its own ``MomentumGraduationStats`` shape because the
  portfolio-rotation engine measures different metrics.
"""

from .graduation import PerTradeGraduationStats

__all__ = ["PerTradeGraduationStats"]
