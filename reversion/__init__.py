"""Reversion — Mean Reversion Engine (plan §4.2, second build).

Public surface: the five plugs (re-exported from ``reversion.plugs``) plus
the shared Pydantic models in ``reversion.models``.
"""

from reversion.models import (
    HARD_STOP_PCT,
    MAX_CONCURRENT_POSITIONS,
    PRE_GRAD_POSITION_CAP_USD,
    REVERSION_TEST_UNIVERSE,
    SCORE_STRONG,
    SCORE_WEAK,
    Direction,
    ExecutionDecision,
    Phase,
    PhaseAssessment,
    SetupCandidate,
)
from reversion.plugs import (
    GraduationStats,
    ReversionAARLogging,
    ReversionCapitalGate,
    ReversionExecutionRisk,
    ReversionLifecycleAnalysis,
    ReversionSetupDetection,
    SizingError,
)

__all__ = [
    "Direction",
    "ExecutionDecision",
    "GraduationStats",
    "HARD_STOP_PCT",
    "MAX_CONCURRENT_POSITIONS",
    "PRE_GRAD_POSITION_CAP_USD",
    "Phase",
    "PhaseAssessment",
    "REVERSION_TEST_UNIVERSE",
    "ReversionAARLogging",
    "ReversionCapitalGate",
    "ReversionExecutionRisk",
    "ReversionLifecycleAnalysis",
    "ReversionSetupDetection",
    "SCORE_STRONG",
    "SCORE_WEAK",
    "SetupCandidate",
    "SizingError",
]
