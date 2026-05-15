"""Sentinel — Plug 5: AAR Logging.

Writes one :class:`AfterActionReport` per ETF per activation cycle.
A cycle "closes" when Sentinel transitions out of ACTIVE/FADING back to
DORMANT/EXITED — at that point the scheduler iterates every ETF position
held during the cycle and writes the closing AAR.

Lightweight by design — like Momentum's AAR plug, this is mostly a
thin shim that exposes :class:`AARWriter` wired with the right engine
name plus a Sentinel-shaped ``write_basket_close`` helper. The
scheduler (or backtest) builds the entry/exit prices from broker fills
or backtest bar marks.
"""
from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

import structlog

from tpcore.aar.models import AfterActionReport, ExitReason
from tpcore.aar.writer import AARWriter
from tpcore.interfaces.engine_plug import BaseEnginePlug

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)


class SentinelAARLogging(BaseEnginePlug):
    """Plug 5 of Sentinel."""

    engine_name = "sentinel"

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

    async def write_basket_close(
        self,
        *,
        trade_id: str,
        ticker: str,
        cycle_id: int,
        entry_ts,
        exit_ts,
        entry_price: Decimal,
        exit_price: Decimal,
        qty: Decimal,
        engine_equity_usd: Decimal,
        exit_reason: ExitReason = ExitReason.SCHEDULED_REBALANCE,
        notes: str | None = None,
    ) -> bool:
        """Persist one AAR row for a closed Sentinel basket position.

        ``cycle_id`` ties the AAR row to its activation cycle so a single
        recession's cycle can be reviewed end-to-end. Returns ``True`` on
        success or when no writer is attached (dry-run mode).
        """
        if self._writer is None:
            logger.debug("sentinel.aar.dry_run", ticker=ticker, trade_id=trade_id)
            return True
        pnl_gross = (exit_price - entry_price) * qty
        sizing_pct = (
            (entry_price * qty) / engine_equity_usd
            if engine_equity_usd > 0
            else Decimal("0")
        )
        aar_notes = notes or f"sentinel cycle {cycle_id}"
        aar = AfterActionReport(
            engine="sentinel",
            trade_id=trade_id,
            ticker=ticker,
            entry_ts=entry_ts,
            exit_ts=exit_ts,
            entry_price=entry_price,
            exit_price=exit_price,
            qty=qty,
            confidence_at_entry=Decimal("0.5"),  # cycle-driven; no per-ETF confidence
            confidence_at_exit=None,
            sizing_pct_of_engine_equity=sizing_pct,
            pnl_gross=pnl_gross,
            pnl_net=pnl_gross,
            exit_reason=exit_reason,
            rule_compliance=True,
            notes=aar_notes,
        )
        return await self._writer.write_aar(aar)


__all__ = ["SentinelAARLogging"]
