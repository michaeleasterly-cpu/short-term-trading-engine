"""Shared technical indicators.

Indicators that more than one engine or service consumes live here.
Engines must NOT define their own copy of a shared indicator —
import from ``tpcore.indicators`` instead. Architecture principle
from CLAUDE.md: "Always use tpcore for shared concerns."

Today's residents:

* ``chop`` — Dreiss Choppiness Index. Used by Sigma's setup-detection
  for regime filtering and by AllocatorService for rebalance gating.
"""

from .chop import (
    CHOP_PERIOD,
    CHOP_SIDEWAYS_STRONG,
    CHOP_SIDEWAYS_WEAK,
    compute_chop,
)

__all__ = [
    "CHOP_PERIOD",
    "CHOP_SIDEWAYS_STRONG",
    "CHOP_SIDEWAYS_WEAK",
    "compute_chop",
]
