"""Wash-sale tracker.

Per IRS §1091, a loss on the sale of a security is disallowed if a
"substantially identical" security is bought within the **61-day window**
(30 days before through 30 days after the sale). The disallowed loss is
added to the cost basis of the replacement shares.

This tracker spans **all engines** — re-entry by a *different* engine still
triggers a wash sale.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, ConfigDict

WASH_SALE_WINDOW = timedelta(days=30)


class WashSaleVerdict(str, Enum):
    NO_WASH = "no_wash"
    WASH_LOSS_DISALLOWED = "wash_loss_disallowed"


class WashSaleEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticker: str
    sale_date: date
    sale_loss: Decimal
    triggering_purchase_date: date | None
    triggering_purchase_engine: str | None
    disallowed_loss: Decimal
    basis_adjustment_lot_id: str | None
    verdict: WashSaleVerdict
    recorded_at: datetime


class WashSaleTracker:
    """Cross-engine wash-sale detection and basis adjustment."""

    def __init__(self, db_pool) -> None:
        self._pool = db_pool

    async def evaluate_sale(
        self,
        *,
        ticker: str,
        sale_date: date,
        loss: Decimal,
    ) -> WashSaleEvent:
        """Check the 61-day window for any buy in ``ticker`` (any engine).

        TODO: query platform.tax_lots for purchases of ``ticker`` whose
        acquisition_date falls within ``sale_date ± 30 days``. If any
        exists and ``loss < 0``, mark the loss disallowed and add it to
        the replacement lot's cost basis.
        """
        _ = (ticker, sale_date, loss, self._pool)
        raise NotImplementedError

    async def is_blocked_for_reentry(self, ticker: str, today: date) -> bool:
        """True iff buying ``ticker`` today would trigger a wash sale on a recent loss."""
        _ = (ticker, today, self._pool)
        raise NotImplementedError
