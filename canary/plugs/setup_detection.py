"""Plug 1 — trivial: every cadence the canary's 'setup' is SPY x1."""
from __future__ import annotations

import structlog

from canary.models import CanarySignal
from tpcore.backtest.filter_diagnostics import FilterDiagnostics
from tpcore.interfaces.engine_plug import BaseEnginePlug

logger = structlog.get_logger(__name__)


class CanarySetupDetection(BaseEnginePlug):
    engine_name = "canary"

    def validate_dependencies(self) -> bool:
        return True

    def healthcheck(self) -> dict:
        return {"engine": self.engine_name, "plug": "setup_detection",
                "ok": True, "details": {}}

    def detect(self) -> tuple[CanarySignal, FilterDiagnostics]:
        """Deterministic heartbeat: SPY always passes (universe of 1)."""
        diag = FilterDiagnostics(universe_total=1)
        diag.candidates_passed = 1
        return CanarySignal(), diag
