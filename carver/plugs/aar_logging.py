"""Carver — Plug 4: AAR logging.

Portfolio-allocation engines without per-name TP/SL get
``ExitReason.TIME_STOP`` from ``tpcore.aar.classify_exit_reason`` when
``take_profit=None`` and ``stop_loss=None`` — the classifier's documented
fall-through. This plug builds + (optionally) persists AARs.

See ``docs/superpowers/specs/2026-05-20-carver-design.md`` Section 5
(no per-name stops between rebalances; lifecycle-driven exits only).
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import structlog

from tpcore.aar.classifier import classify_exit_reason
from tpcore.aar.models import AfterActionReport, ExitReason
from tpcore.aar.writer import AARWriter
from tpcore.interfaces.engine_plug import BaseEnginePlug

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)


class CarverAARLogging(BaseEnginePlug):
    """Plug 4 of Carver — TIME_STOP-based AAR construction + log."""

    engine_name = "carver"

    def __init__(self, pool: asyncpg.Pool | None = None) -> None:
        self._pool = pool
        self._writer = AARWriter(pool) if pool is not None else None

    def validate_dependencies(self) -> bool:
        return True

    def healthcheck(self) -> dict:
        return {
            "engine": self.engine_name,
            "plug": "aar_logging",
            "ok": True,
            "details": {"db_pool_attached": self._pool is not None},
        }

    @staticmethod
    def build_aar(
        *,
        trade_id: str,
        ticker: str,
        entry_price: Decimal,
        exit_price: Decimal,
        qty: int | Decimal,
        entry_time: datetime,
        exit_time: datetime,
        take_profit: Decimal | None = None,
        stop_loss: Decimal | None = None,
        engine_equity_usd: Decimal = Decimal("10000"),
        notes: str | None = None,
    ) -> AfterActionReport:
        """Construct an AAR using ``classify_exit_reason`` (no hardcoded literal).

        Carver passes ``take_profit=None`` and ``stop_loss=None`` so the
        classifier's documented fall-through hands back
        :data:`ExitReason.TIME_STOP` (compliance grep #5)."""
        exit_reason: ExitReason = classify_exit_reason(
            exit_price=exit_price,
            take_profit=take_profit,
            stop_loss=stop_loss,
        )
        qty_d = Decimal(str(qty))
        pnl_gross = (exit_price - entry_price) * qty_d
        sizing_pct = (
            (entry_price * qty_d) / engine_equity_usd
            if engine_equity_usd > 0
            else Decimal("0")
        )
        return AfterActionReport(
            engine="carver",
            trade_id=trade_id,
            ticker=ticker,
            entry_ts=entry_time,
            exit_ts=exit_time,
            entry_price=entry_price,
            exit_price=exit_price,
            qty=qty_d,
            confidence_at_entry=Decimal("0.5"),  # forecast-driven, no per-name confidence
            confidence_at_exit=None,
            sizing_pct_of_engine_equity=sizing_pct,
            pnl_gross=pnl_gross,
            pnl_net=pnl_gross,
            exit_reason=exit_reason,
            rule_compliance=True,
            notes=notes,
        )

    def log_aar(self, aar: AfterActionReport) -> None:
        """Emit a structured log line for the AAR (cheap structlog event)."""
        logger.info(
            "carver.aar",
            trade_id=aar.trade_id,
            ticker=aar.ticker,
            qty=str(aar.qty),
            entry_price=str(aar.entry_price),
            exit_price=str(aar.exit_price),
            pnl_gross=str(aar.pnl_gross),
            exit_reason=aar.exit_reason.value,
        )

    async def write_rebalance_close(
        self,
        *,
        trade_id: str,
        ticker: str,
        entry_ts: datetime,
        exit_ts: datetime,
        entry_price: Decimal,
        exit_price: Decimal,
        qty: Decimal,
        engine_equity_usd: Decimal,
        notes: str | None = None,
    ) -> bool:
        """Persist one AAR row for a closed Carver position.

        Returns True on success (or no-pool dry-run); False on a write failure."""
        aar = self.build_aar(
            trade_id=trade_id,
            ticker=ticker,
            entry_price=entry_price,
            exit_price=exit_price,
            qty=qty,
            entry_time=entry_ts,
            exit_time=exit_ts,
            take_profit=None,
            stop_loss=None,
            engine_equity_usd=engine_equity_usd,
            notes=notes,
        )
        self.log_aar(aar)
        if self._writer is None:
            logger.debug("carver.aar.dry_run", ticker=ticker, trade_id=trade_id)
            return True
        return await self._writer.write_aar(aar)


__all__ = ["CarverAARLogging"]
