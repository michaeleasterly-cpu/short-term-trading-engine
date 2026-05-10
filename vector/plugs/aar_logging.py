"""Vector — Plug 4: AAR Logging.

Builds a ``tpcore.aar.AfterActionReport`` for each closed Vector trade.
Mirrors Sigma's plug shape; the only Vector-specific bits are the
``engine_name`` and the trigger string carried in `notes`.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import structlog

from tpcore.aar.models import AfterActionReport, ExitReason
from tpcore.interfaces.engine_plug import BaseEnginePlug

logger = structlog.get_logger(__name__)


class VectorAARLogging(BaseEnginePlug):
    """Plug 4 of Vector."""

    engine_name = "vector"

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
        *,
        trade_id: str,
        ticker: str,
        entry_ts: datetime,
        exit_ts: datetime,
        entry_price: Decimal,
        exit_price: Decimal,
        qty: Decimal,
        exit_reason: ExitReason,
        confidence_at_entry: Decimal,
        sizing_pct_of_engine_equity: Decimal,
        rule_compliance: bool = True,
        notes: str | None = None,
    ) -> AfterActionReport:
        pnl_gross = (exit_price - entry_price) * qty
        aar = AfterActionReport(
            engine=self.engine_name,
            trade_id=trade_id,
            ticker=ticker,
            entry_ts=entry_ts,
            exit_ts=exit_ts,
            entry_price=entry_price,
            exit_price=exit_price,
            qty=qty,
            confidence_at_entry=confidence_at_entry,
            sizing_pct_of_engine_equity=sizing_pct_of_engine_equity,
            pnl_gross=pnl_gross,
            pnl_net=pnl_gross,  # commissions = 0 on Alpaca paper
            exit_reason=exit_reason,
            rule_compliance=rule_compliance,
            notes=notes,
        )
        logger.info(
            "vector.aar.built",
            ticker=ticker,
            trade_id=trade_id,
            exit_reason=exit_reason.value,
            pnl_gross=str(pnl_gross),
        )
        return aar


__all__ = ["VectorAARLogging"]
