"""Vector engine plugs (5 of 5)."""
from vector.plugs.aar_logging import VectorAARLogging
from vector.plugs.capital_gate import GraduationStats, VectorCapitalGate
from vector.plugs.execution_risk import VectorExecutionRisk
from vector.plugs.lifecycle_analysis import VectorLifecycleAnalysis
from vector.plugs.setup_detection import VectorSetupDetection

__all__ = [
    "GraduationStats",
    "VectorAARLogging",
    "VectorCapitalGate",
    "VectorExecutionRisk",
    "VectorLifecycleAnalysis",
    "VectorSetupDetection",
]
