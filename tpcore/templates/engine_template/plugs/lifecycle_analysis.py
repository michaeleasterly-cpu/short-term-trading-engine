"""Plug 2: Lifecycle Analysis — phase transitions + post-fill bookkeeping.

For per-trade engines (sigma/reversion/vector), ``handle_tier1_fill``
updates the cached ``PhaseAssessment`` after the Tier 1 entry fills so
the order manager can compute Tier 2 / trailing-stop state. Cross-
sectional engines (momentum) usually leave this a no-op.
"""
from __future__ import annotations

import structlog

from tpcore.interfaces.engine_plug import BaseEnginePlug

logger = structlog.get_logger(__name__)


class EngineNameLifecycleAnalysis(BaseEnginePlug):
    """Plug 2 — phase transitions."""

    engine_name = "ENGINE_NAME"

    def validate_dependencies(self) -> bool:
        return True

    def healthcheck(self) -> dict:
        return {
            "engine": self.engine_name,
            "plug": "lifecycle_analysis",
            "ok": True,
            "details": {},
        }

    def assess(self, *args, **kwargs):
        """Map a bar of prices + state → a fresh ``PhaseAssessment``."""
        raise NotImplementedError

    def handle_tier1_fill(self, *args, **kwargs):
        """Per-trade engines: update assessment post-Tier-1 fill."""
        raise NotImplementedError
