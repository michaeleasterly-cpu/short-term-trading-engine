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
    """Emits ``ExecutionQualityScore`` as structured-log lines.

    The historic `platform.execution_quality_log` table was dropped
    2026-05-24 — it accumulated silent-write defects (writer wired,
    no functional consumer). Until a LIVE-execution consumer is built,
    every score is captured via structlog only. The `db_pool` arg is
    retained for ABI stability across callers.
    """

    def __init__(self, db_pool: Any | None = None) -> None:
        del db_pool  # retained for caller-ABI; no DB persistence

    async def write(self, score: ExecutionQualityScore) -> bool:
        logger.info("tpcore.exq.score", **score.model_dump(mode="json"))
        return True
