"""Live/paper parity harness.

Submits the *same* order to both paper and live Alpaca endpoints and
records the fill drift. A growing drift is an early signal of broker-side
behavior changes or strategy assumptions that no longer hold.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from tpcore.interfaces.broker import BrokerExecutionInterface, Order


class ParityDriftRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_order_id: str
    paper_fill_price: Decimal | None
    live_fill_price: Decimal | None
    drift_bps: Decimal | None
    paper_filled_at: datetime | None
    live_filled_at: datetime | None
    timestamp: datetime


class LivePaperParityHarness:
    """Submit identical orders to two brokers and log the resulting drift."""

    def __init__(
        self,
        paper_broker: BrokerExecutionInterface,
        live_broker: BrokerExecutionInterface,
        db_pool,
    ) -> None:
        self._paper = paper_broker
        self._live = live_broker
        self._pool = db_pool

    async def submit_parallel(self, order: Order) -> ParityDriftRecord:
        """Submit ``order`` to both brokers and record the drift.

        TODO: dispatch both orders concurrently, await fills with a sane timeout,
        compute drift in bps relative to the paper fill, and INSERT into
        ``platform.parity_drift_log``.
        """
        _ = (order, self._paper, self._live, self._pool)
        raise NotImplementedError
