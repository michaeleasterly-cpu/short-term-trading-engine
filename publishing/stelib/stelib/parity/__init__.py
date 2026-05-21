"""Live/paper parity harness — submits identical orders to both endpoints and logs drift."""

from .data_parity import (
    DataParityResult,
    FeedClass,
    ParitySample,
    ParityTolerance,
    ParityVerdict,
    compare_provider_parity,
)
from .harness import (
    DriftSummary,
    LivePaperParityHarness,
    ParityDriftRecord,
    weekly_drift_summary,
)

__all__ = [
    "DataParityResult",
    "DriftSummary",
    "FeedClass",
    "LivePaperParityHarness",
    "ParityDriftRecord",
    "ParitySample",
    "ParityTolerance",
    "ParityVerdict",
    "compare_provider_parity",
    "weekly_drift_summary",
]
