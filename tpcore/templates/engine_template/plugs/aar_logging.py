"""Plug 4: AAR Logging — construct + emit AfterActionReports.

Use :class:`tpcore.aar.classify_exit_reason` to map (entry, exit) → an
``ExitReason``. Persistence goes through :class:`tpcore.aar.AARWriter`
(injected at the order-manager layer, not here) — this plug is pure
construction so it can be unit-tested without a DB.
"""
from __future__ import annotations

import structlog

from tpcore.interfaces.engine_plug import BaseEnginePlug

logger = structlog.get_logger(__name__)


class EngineNameAARLogging(BaseEnginePlug):
    """Plug 4 — build AARs."""

    engine_name = "ENGINE_NAME"

    def validate_dependencies(self) -> bool:
        return True

    def healthcheck(self) -> dict:
        return {
            "engine": self.engine_name,
            "plug": "aar_logging",
            "ok": True,
            "details": {},
        }

    def build_aar(self, *args, **kwargs):
        """Return an :class:`tpcore.aar.models.AfterActionReport`."""
        raise NotImplementedError

    def log_aar(self, aar) -> None:
        """Structured log of the AAR — operator-readable summary."""
        logger.info(
            f"{self.engine_name}.aar",
            trade_id=aar.trade_id,
            ticker=aar.ticker,
            pnl_net=str(aar.pnl_net),
        )
