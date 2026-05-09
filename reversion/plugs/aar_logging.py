"""Reversion — Plug 4: AAR Logging.

Mirror of ``sigma.plugs.aar_logging`` shape; same Tier 1 partial /
Tier 2 final pattern. Reversion reuses ``ExitReason.TIER1_MID_BAND``
and ``TIER2_OPPOSITE_BAND`` even though the geometry isn't really
"bands" — keeping the enum stable across engines means Forensics can
aggregate without engine-specific handling. (When more engines arrive
and the enum needs renaming, Forensics is the single migration point.)
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import structlog

from tpcore.aar.models import AfterActionReport, ExitReason
from tpcore.interfaces.engine_plug import BaseEnginePlug

logger = structlog.get_logger(__name__)


class ReversionAARLogging(BaseEnginePlug):
    """Plug 4 of Reversion."""

    engine_name = "reversion"

    def validate_dependencies(self) -> bool:
        return True

    def healthcheck(self) -> dict:
        return {
            "engine": self.engine_name,
            "plug": "aar_logging",
            "ok": True,
            "details": {"sink": "structlog"},
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
        confidence_at_entry: Decimal,
        sizing_pct_of_engine_equity: Decimal,
        exit_reason: ExitReason,
        rule_compliance: bool,
        regime_tags: list[str] | None = None,
        confidence_at_exit: Decimal | None = None,
        fees: Decimal = Decimal("0"),
        slippage_bps: Decimal | None = None,
        notes: str | None = None,
    ) -> AfterActionReport:
        gross = (exit_price - entry_price) * qty
        net = gross - fees
        return AfterActionReport(
            engine=self.engine_name,
            trade_id=trade_id,
            ticker=ticker,
            entry_ts=entry_ts,
            exit_ts=exit_ts,
            entry_price=entry_price,
            exit_price=exit_price,
            qty=qty,
            confidence_at_entry=confidence_at_entry,
            confidence_at_exit=confidence_at_exit,
            sizing_pct_of_engine_equity=sizing_pct_of_engine_equity,
            pnl_gross=gross,
            pnl_net=net,
            fees=fees,
            slippage_bps=slippage_bps,
            regime_tags=regime_tags or [],
            exit_reason=exit_reason,
            rule_compliance=rule_compliance,
            notes=notes,
        )

    def build_tier1_aar(
        self,
        *,
        trade_id: str,
        ticker: str,
        entry_ts: datetime,
        exit_ts: datetime,
        entry_price: Decimal,
        exit_price: Decimal,
        tier1_qty: Decimal,
        confidence_at_entry: Decimal,
        sizing_pct_of_engine_equity: Decimal,
        rule_compliance: bool,
        regime_tags: list[str] | None = None,
        notes: str | None = None,
    ) -> AfterActionReport:
        return self.build_aar(
            trade_id=f"{trade_id}-tier1",
            ticker=ticker,
            entry_ts=entry_ts,
            exit_ts=exit_ts,
            entry_price=entry_price,
            exit_price=exit_price,
            qty=tier1_qty,
            confidence_at_entry=confidence_at_entry,
            sizing_pct_of_engine_equity=sizing_pct_of_engine_equity,
            exit_reason=ExitReason.TIER1_MID_BAND,
            rule_compliance=rule_compliance,
            regime_tags=regime_tags,
            notes=notes,
        )

    def build_tier2_aar(
        self,
        *,
        trade_id: str,
        ticker: str,
        entry_ts: datetime,
        exit_ts: datetime,
        entry_price: Decimal,
        tier1_exit_price: Decimal,
        tier2_exit_price: Decimal,
        tier1_qty: Decimal,
        tier2_qty: Decimal,
        confidence_at_entry: Decimal,
        sizing_pct_of_engine_equity: Decimal,
        rule_compliance: bool,
        regime_tags: list[str] | None = None,
        notes: str | None = None,
    ) -> AfterActionReport:
        total_qty = tier1_qty + tier2_qty
        if total_qty <= 0:
            raise ValueError("total_qty must be > 0 for a tier2 AAR")
        gross_combined = (
            (tier1_exit_price - entry_price) * tier1_qty
            + (tier2_exit_price - entry_price) * tier2_qty
        )
        weighted_exit = entry_price + gross_combined / total_qty
        return self.build_aar(
            trade_id=f"{trade_id}-tier2",
            ticker=ticker,
            entry_ts=entry_ts,
            exit_ts=exit_ts,
            entry_price=entry_price,
            exit_price=weighted_exit,
            qty=total_qty,
            confidence_at_entry=confidence_at_entry,
            sizing_pct_of_engine_equity=sizing_pct_of_engine_equity,
            exit_reason=ExitReason.TIER2_OPPOSITE_BAND,
            rule_compliance=rule_compliance,
            regime_tags=regime_tags,
            notes=notes,
        )

    def log_aar(self, aar: AfterActionReport) -> dict:
        payload = aar.model_dump(mode="json")
        logger.info("reversion.aar", **payload)
        return payload
