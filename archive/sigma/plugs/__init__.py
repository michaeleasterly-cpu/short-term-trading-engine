"""Sigma engine plugs (the five-plug model from plan §4.1)."""

from .aar_logging import SigmaAARLogging
from .capital_gate import GraduationStats, SigmaCapitalGate
from .execution_risk import SigmaExecutionRisk, SizingError
from .lifecycle_analysis import SigmaLifecycleAnalysis
from .setup_detection import SigmaSetupDetection

__all__ = [
    "GraduationStats",
    "SigmaAARLogging",
    "SigmaCapitalGate",
    "SigmaExecutionRisk",
    "SigmaLifecycleAnalysis",
    "SigmaSetupDetection",
    "SizingError",
]
