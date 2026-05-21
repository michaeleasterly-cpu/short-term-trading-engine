"""Tax-loss harvester.

Daily scan of open positions with unrealized losses. Consults the owning
engine's *Lifecycle Analysis* plug for confidence/phase signals. Names that
are "probably failing" (confidence dropped, near stop or time-stop) are
candidates for harvest.

Auto-execute is enabled in **Q4** within a configurable annual net-loss
cap (default $3,000 to match the IRS individual deduction limit). Outside
Q4, the harvester surfaces *recommendations* only.

Reentry is blocked for **31 days** via :class:`WashSaleTracker`.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from stelib.interfaces.broker import BrokerExecutionInterface
from stelib.tax.wash_sale import WashSaleTracker

DEFAULT_ANNUAL_NET_LOSS_CAP = Decimal("3000")
REENTRY_BLOCK_DAYS = 31


class HarvestDisposition(StrEnum):
    AUTO_EXECUTED = "auto_executed"
    RECOMMENDED_MANUAL = "recommended_manual"
    SKIPPED = "skipped"


class HarvestRecommendation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticker: str
    engine_id: str
    unrealized_loss: Decimal
    lifecycle_confidence: Decimal | None
    lifecycle_phase: str | None
    disposition: HarvestDisposition
    reason: str


class TaxLossHarvester:
    """Daily harvester. Q4 auto-execute behind a net-loss cap; manual otherwise."""

    def __init__(
        self,
        broker: BrokerExecutionInterface,
        wash_sale_tracker: WashSaleTracker,
        db_pool,
        annual_net_loss_cap: Decimal = DEFAULT_ANNUAL_NET_LOSS_CAP,
    ) -> None:
        self._broker = broker
        self._wash = wash_sale_tracker
        self._pool = db_pool
        self._cap = annual_net_loss_cap

    async def scan(self, *, today: date) -> list[HarvestRecommendation]:
        """Daily scan. Returns one recommendation per open position w/ unrealized loss.

        Steps (TODO implement):
          1. Pull open positions from broker.
          2. For each loser, fetch lifecycle confidence/phase from owning
             engine's plug; if "probably failing", mark as candidate.
          3. If month is Oct/Nov/Dec **and** YTD harvested losses + this
             loss <= ``self._cap``, auto-execute via ``self._broker``.
             Otherwise recommend manual review.
          4. After any sale, mark ticker as blocked for ``REENTRY_BLOCK_DAYS``
             via ``self._wash``.
        """
        _ = today
        raise NotImplementedError
