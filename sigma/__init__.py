"""Sigma — Range Scalping Engine (plan §4.1, first build).

Public surface: the five plugs (re-exported from ``sigma.plugs``) plus the
shared Pydantic models in ``sigma.models``.
"""

from sigma.models import (
    HARD_STOP_PCT,
    MAX_CONCURRENT_POSITIONS,
    PRE_GRAD_POSITION_CAP_USD,
    SCORE_STRONG,
    SCORE_WEAK,
    SIGMA_TEST_UNIVERSE,
    ExecutionDecision,
    Phase,
    PhaseAssessment,
    SetupCandidate,
)
from sigma.plugs import (
    GraduationStats,
    SigmaAARLogging,
    SigmaCapitalGate,
    SigmaExecutionRisk,
    SigmaLifecycleAnalysis,
    SigmaSetupDetection,
    SizingError,
)

__all__ = [
    "ExecutionDecision",
    "GraduationStats",
    "HARD_STOP_PCT",
    "MAX_CONCURRENT_POSITIONS",
    "PRE_GRAD_POSITION_CAP_USD",
    "Phase",
    "PhaseAssessment",
    "SCORE_STRONG",
    "SCORE_WEAK",
    "SIGMA_TEST_UNIVERSE",
    "SetupCandidate",
    "SigmaAARLogging",
    "SigmaCapitalGate",
    "SigmaExecutionRisk",
    "SigmaLifecycleAnalysis",
    "SigmaSetupDetection",
    "SizingError",
]
