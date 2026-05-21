"""Tax-lot tracker (FIFO by default).

Every buy fill creates a lot. Every sell fill consumes lots in FIFO order
and emits a realized-gain/loss record. Lots are persisted to
``platform.tax_lots``.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class LotStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    PARTIAL = "partial"


class TaxLot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lot_id: str
    ticker: str
    engine_id: str
    acquisition_date: date
    shares: Decimal
    cost_basis: Decimal
    status: LotStatus = LotStatus.OPEN
    closed_at: datetime | None = None
    realized_pnl: Decimal | None = None


class TaxLotTracker:
    """Records purchases and assigns lots on sale (FIFO)."""

    def __init__(self, db_pool) -> None:
        self._pool = db_pool

    async def record_purchase(
        self,
        *,
        ticker: str,
        engine_id: str,
        shares: Decimal,
        cost_basis: Decimal,
        acquisition_date: date,
    ) -> TaxLot:
        """Persist a new OPEN lot. TODO: implement via INSERT into platform.tax_lots."""
        _ = (ticker, engine_id, shares, cost_basis, acquisition_date, self._pool)
        raise NotImplementedError

    async def assign_lots_for_sale(
        self,
        *,
        ticker: str,
        shares_sold: Decimal,
        sale_price: Decimal,
        method: str = "fifo",
    ) -> list[TaxLot]:
        """Consume open lots in FIFO order until ``shares_sold`` is satisfied.

        Returns the closed (or partially closed) lots with realized P&L set.
        TODO: implement; support FIFO (default), LIFO, and HIFO selectable
        per call so the harvester can pick the highest-cost lots when desired.
        """
        _ = (ticker, shares_sold, sale_price, method, self._pool)
        raise NotImplementedError
