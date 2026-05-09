"""Data-quality scoring for upstream feeds (Alpaca, FMP, EDGAR, FRED, etc.)."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class DataQualityScore(BaseModel):
    """Snapshot of a single data-source health check. UTC."""

    model_config = ConfigDict(extra="forbid")

    source: str
    timestamp: datetime
    latency_ms: int
    missing_bars: int = 0
    stale: bool = False
    confidence: Decimal = Field(ge=0, le=1)
    notes: str | None = None


class DataQualityWriter:
    """Persists ``DataQualityScore`` rows to ``platform.data_quality_log``."""

    def __init__(self, db_pool) -> None:
        self._pool = db_pool

    async def write(self, score: DataQualityScore) -> None:
        """TODO: INSERT into platform.data_quality_log."""
        _ = (score, self._pool)
        raise NotImplementedError
