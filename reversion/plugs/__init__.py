"""Reversion engine plugs (the five-plug model from plan §4.2)."""

from .aar_logging import ReversionAARLogging
from .capital_gate import GraduationStats, ReversionCapitalGate
from .execution_risk import ReversionExecutionRisk, SizingError
from .lifecycle_analysis import ReversionLifecycleAnalysis
from .setup_detection import ReversionSetupDetection

__all__ = [
    "GraduationStats",
    "ReversionAARLogging",
    "ReversionCapitalGate",
    "ReversionExecutionRisk",
    "ReversionLifecycleAnalysis",
    "ReversionSetupDetection",
    "SizingError",
]
