"""Plug 1: Setup Detection — scan the universe for setups.

Populates a :class:`tpcore.backtest.filter_diagnostics.FilterDiagnostics`
instance so SIGNAL events carry per-gate pass/block counters. Reuses
shared indicators from :mod:`tpcore.indicators` rather than rolling its
own (see `tpcore/indicators/__init__.py`).
"""
from __future__ import annotations

import structlog

from tpcore.interfaces.engine_plug import BaseEnginePlug

logger = structlog.get_logger(__name__)


class EngineNameSetupDetection(BaseEnginePlug):
    """Plug 1 — scan universe + emit PhaseAssessments."""

    engine_name = "ENGINE_NAME"

    def validate_dependencies(self) -> bool:
        # TODO: assert required tpcore.indicators / data sources are reachable.
        return True

    def healthcheck(self) -> dict:
        return {
            "engine": self.engine_name,
            "plug": "setup_detection",
            "ok": True,
            "details": {},
        }

    def detect(self, *args, **kwargs):
        """Return list of ``PhaseAssessment`` for tickers passing all gates."""
        raise NotImplementedError("wire setup_detection.detect for this engine")
