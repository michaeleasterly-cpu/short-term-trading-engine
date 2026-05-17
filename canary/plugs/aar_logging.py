"""Plug 4 — build the AAR for the daily round-trip. Uses
classify_exit_reason (no TP/SL → TIME_STOP); never hardcodes a literal."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import structlog

from tpcore.aar.classifier import classify_exit_reason
from tpcore.aar.models import AfterActionReport, ExitReason
from tpcore.interfaces.engine_plug import BaseEnginePlug

logger = structlog.get_logger(__name__)


class CanaryAARLogging(BaseEnginePlug):
    engine_name = "canary"

    def validate_dependencies(self) -> bool:
        return True

    def healthcheck(self) -> dict:
        return {"engine": self.engine_name, "plug": "aar_logging",
                "ok": True, "details": {}}

    def build_aar(self, *, trade_id: str, entry_ts_iso: str,
                  exit_ts_iso: str, entry_price: Decimal,
                  exit_price: Decimal, qty: Decimal,
                  engine_equity_usd: Decimal,
                  exit_reason: ExitReason | None = None) -> AfterActionReport:
        if exit_reason is None:
            exit_reason = classify_exit_reason(
                exit_price=exit_price, take_profit=None, stop_loss=None)
        pnl = (exit_price - entry_price) * qty
        sizing = ((entry_price * qty) / engine_equity_usd
                  if engine_equity_usd > 0 else Decimal("0"))
        return AfterActionReport(
            engine="canary", trade_id=trade_id, ticker="SPY",
            entry_ts=datetime.fromisoformat(entry_ts_iso),
            exit_ts=datetime.fromisoformat(exit_ts_iso),
            entry_price=entry_price, exit_price=exit_price, qty=qty,
            confidence_at_entry=Decimal("0.5"), confidence_at_exit=None,
            sizing_pct_of_engine_equity=sizing,
            pnl_gross=pnl, pnl_net=pnl, exit_reason=exit_reason,
            rule_compliance=True, notes="canary heartbeat round-trip")

    def log_aar(self, aar: AfterActionReport) -> None:
        logger.info("canary.aar", trade_id=aar.trade_id,
                    pnl_net=str(aar.pnl_net))
