"""Plug 2 — no-op: canary is a stateless daily round-trip."""
from __future__ import annotations

import structlog

from tpcore.interfaces.engine_plug import BaseEnginePlug

logger = structlog.get_logger(__name__)


class CanaryLifecycleAnalysis(BaseEnginePlug):
    engine_name = "canary"

    def validate_dependencies(self) -> bool:
        return True

    def healthcheck(self) -> dict:
        return {"engine": self.engine_name, "plug": "lifecycle_analysis",
                "ok": True, "details": {}}

    def assess(self) -> None:
        """Stateless heartbeat — no lifecycle state to track."""
        return None
