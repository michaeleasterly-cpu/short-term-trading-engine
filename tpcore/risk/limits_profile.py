"""Single source of truth for each engine's RiskLimits.

Per-trade engines (reversion/vector) use the default 8-position cap.
Batch engines hold a basket far larger than 8, so their position cap is
sized to the basket (momentum ≈ decile of T1+T2 universe; sentinel = 5
ETFs). Loss-cap / net-long percentages stay platform-uniform unless an
engine genuinely needs otherwise — change here, nowhere else.
"""
from __future__ import annotations

from tpcore.risk.governor import RiskLimits

_PROFILE: dict[str, RiskLimits] = {
    "momentum": RiskLimits(max_open_positions=200),
    "sentinel": RiskLimits(max_open_positions=5),
    "canary":   RiskLimits(max_open_positions=1),
}


def limits_for(engine_id: str) -> RiskLimits:
    """RiskLimits for an engine; default (8-pos) if not profiled."""
    return _PROFILE.get(engine_id, RiskLimits())
