"""Data-quality scoring for upstream feeds (Alpaca, FMP, EDGAR, FRED, etc.)."""
from __future__ import annotations

import json
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


def _notes_to_jsonb_text(notes: str | None) -> str | None:
    """Coerce a ``DataQualityScore.notes`` string into a jsonb-castable text.

    The redesigned ``platform.data_quality_log.notes`` column is ``jsonb`` (Plan 2
    migration 20260604_0500). Existing producers serialize ``notes`` either as a
    JSON document (validation suite → ``json.dumps([...])``; credibility →
    ``CredibilityScore.model_dump_json()``; cross-table → ``json.dumps({...})``)
    or, occasionally, as plain free text. To keep the downstream JSON readers
    intact (e.g. ``tpcore.lab.ledger`` reads ``notes::jsonb->>'trials'``;
    ``scripts.generate_tip_sheet`` does ``model_validate_json(notes)``):

      * already-valid JSON  → pass through unchanged (the value is jsonb-cast as-is).
      * plain free text     → wrap as ``{"text": <notes>}`` so the column stays
        valid jsonb without losing the message.
      * ``None``            → ``None`` (jsonb NULL).

    Returns the text to bind to a ``$N::jsonb`` parameter.
    """
    if notes is None:
        return None
    try:
        json.loads(notes)
    except (ValueError, TypeError):
        return json.dumps({"text": notes})
    return notes


class DataQualityWriter:
    """Persists ``DataQualityScore`` rows to ``platform.data_quality_log``.

    Plan 2 (migration 20260604_0500) reshaped the table: ``id`` is now a uuid PK,
    a ``kind`` discriminator is required, the typed metric columns are
    VALIDATION-ONLY (a CHECK ties them to ``kind='validation'``), and ``notes`` is
    ``jsonb``. This writer is the minimal Plan-2 shim: it stamps ``kind='validation'``
    on every row it writes (the only CHECK-compliant value for rows that carry the
    typed metric columns — both freshness checks AND credibility scores flow
    through here) and casts ``notes`` to jsonb. The per-``kind`` writer split
    (``backtest_credibility`` / ``parity_drift`` / ``forensics_trigger`` etc.) is
    deferred to Plan 3/4 as those producers are wired in.

    Idempotency note: the old ``UNIQUE (source, timestamp)`` was dropped in the
    redesign (the uuid PK makes every row unique), so the former
    ``ON CONFLICT (source, timestamp) DO NOTHING`` semantics no longer apply — this
    is now a plain INSERT. ``source_freshness_days`` still has no column and is not
    persisted; serialize it into ``notes`` if needed.
    """

    def __init__(self, db_pool: asyncpg.Pool | None = None) -> None:
        self._pool = db_pool

    async def write(self, score: DataQualityScore) -> bool:
        """Insert ``score``. Returns ``True`` iff a row was written."""
        if self._pool is None:
            return False

        sql = """
            INSERT INTO platform.data_quality_log (
                kind, source, timestamp, latency_ms, missing_bars,
                stale, confidence, notes
            )
            VALUES ('validation', $1, $2, $3, $4, $5, $6, $7::jsonb)
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
                _notes_to_jsonb_text(score.notes),
            )
        wrote = row is not None
        logger.debug(
            "tpcore.data_quality.write",
            source=score.source,
            timestamp=score.timestamp.isoformat(),
            wrote=wrote,
        )
        return wrote
