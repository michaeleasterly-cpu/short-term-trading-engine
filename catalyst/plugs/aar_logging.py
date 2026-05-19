"""Catalyst — Plug 4: AAR Logging.

Per-trade flat-bracket engine — builds one :class:`AfterActionReport`
per closed position. Exit reason derived via
:func:`tpcore.aar.classify_exit_reason` so TP/SL classification is
canonical (never a hardcoded ``ExitReason`` literal — STYLE_GUIDE rule).
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


class CatalystAARLogging(BaseEnginePlug):
    """Plug 4 — AAR construction + emission."""

    engine_name = "catalyst"

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
        engine_equity_usd: Decimal,
        take_profit: Decimal | None,
        stop_loss: Decimal | None,
        confidence_at_entry: Decimal = Decimal("0.6"),
        exit_reason: ExitReason | None = None,
        rule_compliance: bool = True,
        notes: str | None = None,
    ) -> AfterActionReport:
        """Construct one :class:`AfterActionReport` for a closed catalyst trade.

        If ``exit_reason`` is not supplied, classify it from
        ``(exit_price, take_profit, stop_loss)`` — the canonical mapper.
        Hardcoded ``ExitReason`` literals are FORBIDDEN by
        STYLE_GUIDE.md "Engine plug compliance".
        """
        if exit_reason is None:
            exit_reason = classify_exit_reason(
                exit_price=exit_price,
                take_profit=take_profit,
                stop_loss=stop_loss,
            )
        pnl = (exit_price - entry_price) * qty
        sizing = (
            (entry_price * qty) / engine_equity_usd
            if engine_equity_usd > 0
            else Decimal("0")
        )
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
            confidence_at_exit=None,
            sizing_pct_of_engine_equity=sizing,
            pnl_gross=pnl,
            pnl_net=pnl,
            exit_reason=exit_reason,
            rule_compliance=rule_compliance,
            notes=notes or "catalyst insider-cluster trade",
        )

    async def write_aar(self, aar: AfterActionReport) -> bool:
        """Persist via :class:`AARWriter`. Returns True if no writer is
        attached (dry-run / test) — mirrors the Sentinel pattern."""
        if self._writer is None:
            logger.debug("catalyst.aar.dry_run", trade_id=aar.trade_id,
                         ticker=aar.ticker)
            return True
        return await self._writer.write_aar(aar)

    def log_aar(self, aar: AfterActionReport) -> None:
        logger.info(
            "catalyst.aar",
            trade_id=aar.trade_id,
            ticker=aar.ticker,
            pnl_net=str(aar.pnl_net),
        )


__all__ = ["CatalystAARLogging"]
