"""Momentum — Plug 5: AAR Logging.

Writes one :class:`AfterActionReport` per ticker per *completed* rebalance
cycle. A rebalance "completes" when a ticker is sold — either because it
fell out of the top decile and was closed, or it was rebalanced down. The
AAR captures the holding period (entry rebalance → exit rebalance) and
the realized P&L.

Implementation note for Phase 2 MVP: this plug is lightweight — it just
exposes the AARWriter wired with the right engine name. The scheduler
constructs AAR rows from broker fills + last-known entry data; this plug
exists for parity with the other engines and to keep the writer
configuration in one place.
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


class MomentumAARLogging(BaseEnginePlug):
    """Plug 5 of Momentum."""

    engine_name = "momentum"

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

    async def write_rebalance_close(
        self,
        *,
        trade_id: str,
        ticker: str,
        entry_ts,
        exit_ts,
        entry_price: Decimal,
        exit_price: Decimal,
        qty: Decimal,
        engine_equity_usd: Decimal,
        exit_reason: ExitReason = ExitReason.SCHEDULED_REBALANCE,
        notes: str | None = None,
    ) -> bool:
        """Persist one AAR row for a closed Momentum position.

        Returns ``True`` if the write succeeded (or no writer is attached,
        in which case the call is a no-op for dry-run tests)."""
        if self._writer is None:
            logger.debug("momentum.aar.dry_run", ticker=ticker, trade_id=trade_id)
            return True
        pnl_gross = (exit_price - entry_price) * qty
        sizing_pct = (
            (entry_price * qty) / engine_equity_usd
            if engine_equity_usd > 0
            else Decimal("0")
        )
        aar = AfterActionReport(
            engine="momentum",
            trade_id=trade_id,
            ticker=ticker,
            entry_ts=entry_ts,
            exit_ts=exit_ts,
            entry_price=entry_price,
            exit_price=exit_price,
            qty=qty,
            confidence_at_entry=Decimal("0.5"),  # rank-based; no per-name confidence
            confidence_at_exit=None,
            sizing_pct_of_engine_equity=sizing_pct,
            pnl_gross=pnl_gross,
            pnl_net=pnl_gross,
            exit_reason=exit_reason,
            rule_compliance=True,
            notes=notes,
        )
        return await self._writer.write_aar(aar)
