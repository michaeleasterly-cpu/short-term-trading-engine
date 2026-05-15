"""Plug 4: AAR Logging — construct + emit AfterActionReports.

Use :class:`tpcore.aar.classify_exit_reason` to map (entry, exit) → an
``ExitReason``. **Never hardcode an** ``ExitReason`` **literal** —
STYLE_GUIDE.md "Engine plug compliance" makes this a hard rule.
Persistence goes through :class:`tpcore.aar.AARWriter` (injected at the
order-manager layer, not here) — this plug is pure construction so it
can be unit-tested without a DB.
"""
from __future__ import annotations

from decimal import Decimal

import structlog

from tpcore.aar.classifier import classify_exit_reason
from tpcore.aar.models import ExitReason
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

    def build_aar(
        self,
        *args,
        exit_price: Decimal | None = None,
        take_profit: Decimal | None = None,
        stop_loss: Decimal | None = None,
        exit_reason: ExitReason | None = None,
        **kwargs,
    ):
        """Return an :class:`tpcore.aar.models.AfterActionReport`.

        Compliance pattern: if the caller didn't pass an explicit
        ``exit_reason``, derive it from the broker fill via
        :func:`classify_exit_reason` — never default to a hardcoded
        ``ExitReason`` literal. For portfolio-allocation engines without
        TP/SL on positions, pass ``take_profit=None, stop_loss=None`` and
        the classifier returns ``TIME_STOP``.
        """
        if exit_reason is None and exit_price is not None:
            exit_reason = classify_exit_reason(
                exit_price=exit_price,
                take_profit=take_profit,
                stop_loss=stop_loss,
            )
        raise NotImplementedError

    def log_aar(self, aar) -> None:
        """Structured log of the AAR — operator-readable summary."""
        logger.info(
            f"{self.engine_name}.aar",
            trade_id=aar.trade_id,
            ticker=aar.ticker,
            pnl_net=str(aar.pnl_net),
        )
