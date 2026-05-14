"""Shared technical indicators.

Indicators that more than one engine or service consumes live here.
Engines must NOT define their own copy of a shared indicator —
import from ``tpcore.indicators`` instead. Architecture principle
from CLAUDE.md: "Always use tpcore for shared concerns."

Today's residents:

* ``chop`` — Dreiss Choppiness Index. Used by Sigma's setup-detection
  for regime filtering and by AllocatorService for rebalance gating.
"""

from .adx import ADX_PERIOD, compute_adx
from .bbands import BB_NUM_STD, BB_PERIOD, compute_bbands
from .chop import (
    CHOP_PERIOD,
    CHOP_SIDEWAYS_STRONG,
    CHOP_SIDEWAYS_WEAK,
    compute_chop,
)

__all__ = [
    "ADX_PERIOD",
    "BB_NUM_STD",
    "BB_PERIOD",
    "CHOP_PERIOD",
    "CHOP_SIDEWAYS_STRONG",
    "CHOP_SIDEWAYS_WEAK",
    "compute_adx",
    "compute_bbands",
    "compute_chop",
]
