"""3-tier outage policy.

Tiers, in increasing severity:

* ``INFORMATIONAL`` — log only; no operational change.
* ``AVAILABILITY``  — degrade gracefully (skip the affected feed, widen
  spreads, defer non-critical work). Trading continues.
* ``KILL_SWITCH``   — invoke ``RiskGovernor.emergency_kill``; cancel all
  orders, flatten positions, halt new trades.
"""
from __future__ import annotations

from datetime import timedelta
from enum import Enum

from pydantic import BaseModel, ConfigDict


class OutageTier(str, Enum):
    INFORMATIONAL = "informational"
    AVAILABILITY = "availability"
    KILL_SWITCH = "kill_switch"


class OutageThresholds(BaseModel):
    """Defaults are conservative; tune per source via dependency injection."""

    model_config = ConfigDict(extra="forbid")

    # INFORMATIONAL: any single failure is informational.
    # AVAILABILITY: rolling failures over a window.
    availability_consecutive_failures: int = 3
    availability_max_staleness: timedelta = timedelta(minutes=5)
    # KILL_SWITCH: prolonged failure or extreme staleness.
    kill_consecutive_failures: int = 10
    kill_max_staleness: timedelta = timedelta(minutes=30)


class OutagePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    thresholds: OutageThresholds = OutageThresholds()


def classify_outage(
    *,
    consecutive_failures: int,
    staleness: timedelta,
    thresholds: OutageThresholds,
) -> OutageTier:
    """Classify the current outage state given recent failure metrics."""
    if (
        consecutive_failures >= thresholds.kill_consecutive_failures
        or staleness >= thresholds.kill_max_staleness
    ):
        return OutageTier.KILL_SWITCH
    if (
        consecutive_failures >= thresholds.availability_consecutive_failures
        or staleness >= thresholds.availability_max_staleness
    ):
        return OutageTier.AVAILABILITY
    return OutageTier.INFORMATIONAL
