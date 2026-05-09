"""3-tier outage policy for upstream service failures."""

from .policy import (
    OutagePolicy,
    OutageThresholds,
    OutageTier,
    classify_outage,
)

__all__ = ["OutagePolicy", "OutageThresholds", "OutageTier", "classify_outage"]
