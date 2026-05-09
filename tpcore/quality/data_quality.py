"""Data-quality scoring for upstream feeds (Alpaca, FMP, EDGAR, FRED, etc.)."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import structlog
from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)


class DataQualityScore(BaseModel):
    """Snapshot of a single data-source health check. UTC."""

    model_config = ConfigDict(extra="forbid")

    source: str
    timestamp: datetime
    latency_ms: int = 0
    missing_bars: int = 0
    stale: bool = False
    confidence: Decimal = Field(ge=0, le=1)
    source_freshness_days: int | None = Field(
        default=None,
        description="Days between the data point's filing/observation date and ``timestamp``.",
    )
    notes: str | None = None


class DataQualityWriter:
    """Persists ``DataQualityScore`` rows to ``platform.data_quality_log``.

    Idempotency follows D-137 Pattern A: ``ON CONFLICT (source, timestamp) DO
    NOTHING`` plus ``RETURNING 1`` so callers can tell new inserts from
    duplicates. ``source_freshness_days`` has no column in the schema and is
    intentionally not persisted; if the field matters for a given source,
    serialize it into ``notes`` (JSON).
    """

    def __init__(self, db_pool: "asyncpg.Pool | None" = None) -> None:
        self._pool = db_pool

    async def write(self, score: DataQualityScore) -> bool:
        """Insert ``score`` if absent. Returns ``True`` iff a new row was written."""
        if self._pool is None:
            return False

        sql = """
            INSERT INTO platform.data_quality_log (
                source, timestamp, latency_ms, missing_bars,
                stale, confidence, notes
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (source, timestamp) DO NOTHING
            RETURNING 1
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                sql,
                score.source,
                score.timestamp,
                score.latency_ms,
                score.missing_bars,
                score.stale,
                score.confidence,
                score.notes,
            )
        wrote = row is not None
        logger.debug(
            "tpcore.data_quality.write",
            source=score.source,
            timestamp=score.timestamp.isoformat(),
            wrote=wrote,
        )
        return wrote
