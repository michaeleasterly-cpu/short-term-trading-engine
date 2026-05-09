"""Execution-quality scoring (slippage, partial fills, paper-vs-live)."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class ExecutionQualityScore(BaseModel):
    model_config = ConfigDict(extra="forbid")

    broker: str
    order_id: str
    requested_price: Decimal | None
    fill_price: Decimal
    slippage_bps: Decimal
    partial_fill: bool = False
    paper_or_live: str  # "paper" | "live"
    timestamp: datetime
    notes: str | None = None


class ExecutionQualityWriter:
    """Persists ``ExecutionQualityScore`` rows to ``platform.execution_quality_log``."""

    def __init__(self, db_pool) -> None:
        self._pool = db_pool

    async def write(self, score: ExecutionQualityScore) -> None:
        """TODO: INSERT into platform.execution_quality_log."""
        _ = (score, self._pool)
        raise NotImplementedError
