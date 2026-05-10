"""Vector — Momentum Swing Engine (plan §4.3, third build).

Public surface: the five plugs (re-exported from ``vector.plugs``) plus the
shared Pydantic models in ``vector.models``. Engines stay isolated; this
package never imports from sigma/ or reversion/.
"""
from vector.models import (
    DAILY_LOSS_FREEZE_PCT,
    HARD_STOP_PCT,
    MAX_CONCURRENT_POSITIONS,
    PRE_GRAD_POSITION_CAP_USD,
    PROFIT_TARGET_PCT,
    SCORE_STRONG,
    SCORE_WEAK,
    TRAILING_STOP_PCT,
    TRAILING_STOP_TRIGGER_PCT,
    VECTOR_TEST_UNIVERSE,
    ExecutionDecision,
    Phase,
    PhaseAssessment,
    SetupCandidate,
)
from vector.plugs import (
    GraduationStats,
    VectorAARLogging,
    VectorCapitalGate,
    VectorExecutionRisk,
    VectorLifecycleAnalysis,
    VectorSetupDetection,
)

__all__ = [
    "DAILY_LOSS_FREEZE_PCT",
    "ExecutionDecision",
    "GraduationStats",
    "HARD_STOP_PCT",
    "MAX_CONCURRENT_POSITIONS",
    "PRE_GRAD_POSITION_CAP_USD",
    "PROFIT_TARGET_PCT",
    "Phase",
    "PhaseAssessment",
    "SCORE_STRONG",
    "SCORE_WEAK",
    "SetupCandidate",
    "TRAILING_STOP_PCT",
    "TRAILING_STOP_TRIGGER_PCT",
    "VECTOR_TEST_UNIVERSE",
    "VectorAARLogging",
    "VectorCapitalGate",
    "VectorExecutionRisk",
    "VectorLifecycleAnalysis",
    "VectorSetupDetection",
]
