"""Execution-quality scoring (slippage, partial fills, paper-vs-live)."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict

logger = structlog.get_logger(__name__)


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
    """Persists ``ExecutionQualityScore`` rows to ``platform.execution_quality_log``.

    When ``db_pool`` is ``None`` (DB not yet wired in this environment), the
    writer falls back to emitting a structured log line so the score is still
    captured for offline aggregation.
    """

    def __init__(self, db_pool: Any | None = None) -> None:
        self._pool = db_pool

    async def write(self, score: ExecutionQualityScore) -> bool:
        """Insert ``score`` once. Returns True iff a new row was written.

        Idempotency is enforced by the ``(broker, order_id)`` unique constraint
        plus ``ON CONFLICT DO NOTHING``. With no pool, every call returns
        ``True`` (the structlog sink has no dedup).
        """
        if self._pool is None:
            logger.info("tpcore.exq.score", **score.model_dump(mode="json"))
            return True

        sql = """
            INSERT INTO platform.execution_quality_log (
                broker, order_id, requested_price, fill_price, slippage_bps,
                partial_fill, paper_or_live, timestamp, notes
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            ON CONFLICT (broker, order_id) DO NOTHING
            RETURNING 1
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                sql,
                score.broker,
                score.order_id,
                score.requested_price,
                score.fill_price,
                score.slippage_bps,
                score.partial_fill,
                score.paper_or_live,
                score.timestamp,
                score.notes,
            )
        return row is not None
