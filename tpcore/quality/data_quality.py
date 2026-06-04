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

# The ``kind`` discriminator enum on ``platform.data_quality_log`` (Plan 2
# migration 20260604_0500). Mirrors the migration's ``KINDS`` tuple + the
# ``kind IN (...)`` CHECK; kept here so producers reference a named constant
# rather than re-typing the literal. The typed metric columns
# (``latency_ms``/``missing_bars``/``stale``/``confidence``) are VALIDATION-ONLY
# (enforced by the ``dql_typed_cols_validation_only`` CHECK).
KIND_VALIDATION = "validation"
KIND_CONFIRMED_DATA_GAP_EVIDENCE = "confirmed_data_gap_evidence"
KIND_PARITY_DRIFT = "parity_drift"
KIND_FORENSICS_TRIGGER = "forensics_trigger"
KIND_BACKTEST_CREDIBILITY = "backtest_credibility"

DQL_KINDS: frozenset[str] = frozenset(
    {
        KIND_VALIDATION,
        KIND_CONFIRMED_DATA_GAP_EVIDENCE,
        KIND_PARITY_DRIFT,
        KIND_FORENSICS_TRIGGER,
        KIND_BACKTEST_CREDIBILITY,
    }
)


def _coerce_notes_to_jsonb_text(notes: str | dict | list | None) -> str | None:
    """Coerce a producer's ``notes`` into jsonb-castable text.

    Accepts the historical free-text/JSON-string form (validation suite,
    credibility, cross-table) AND the structured ``dict`` / ``list`` form the
    folded-sidecar producers (forensics / parity) pass directly. Returns the
    text to bind to a ``$N::jsonb`` parameter:

      * ``dict`` / ``list``  → ``json.dumps`` (with ``default=str`` so Decimals
        / datetimes serialize).
      * ``str``              → delegated to :func:`_notes_to_jsonb_text`
        (pass-through valid JSON; wrap plain text as ``{"text": ...}``).
      * ``None``             → ``None`` (jsonb NULL).
    """
    if notes is None:
        return None
    if isinstance(notes, (dict, list)):
        return json.dumps(notes, default=str)
    return _notes_to_jsonb_text(notes)


async def write_row(
    pool: asyncpg.Pool | None,
    *,
    kind: str,
    source: str,
    timestamp: datetime,
    notes: str | dict | list | None,
    latency_ms: int | None = None,
    missing_bars: int | None = None,
    stale: bool | None = None,
    confidence: Decimal | float | None = None,
) -> bool:
    """Low-level INSERT into the redesigned ``platform.data_quality_log``.

    The single write path for EVERY producer of the table (Plan 2
    consolidation). The typed metric columns are VALIDATION-ONLY by CHECK
    (``dql_typed_cols_validation_only``): a non-``validation`` ``kind`` MUST
    leave them ``None`` (callers serialize any extra fields into ``notes``
    jsonb instead). ``notes`` is cast ``::jsonb``.

    Returns ``True`` iff a row was written (``False`` when ``pool is None``).
    Raises ``ValueError`` if a non-``validation`` kind is passed typed columns
    — failing loud locally rather than tripping the DB CHECK at runtime.
    """
    if kind not in DQL_KINDS:
        raise ValueError(f"unknown data_quality_log kind {kind!r}; valid: {sorted(DQL_KINDS)}")
    typed_passed = any(v is not None for v in (latency_ms, missing_bars, stale, confidence))
    if kind != KIND_VALIDATION and typed_passed:
        raise ValueError(
            f"data_quality_log kind={kind!r} forbids typed metric columns "
            "(latency_ms/missing_bars/stale/confidence) — serialize them into notes jsonb"
        )
    if pool is None:
        return False

    sql = """
        INSERT INTO platform.data_quality_log (
            kind, source, timestamp, latency_ms, missing_bars,
            stale, confidence, notes
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
        RETURNING 1
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            sql,
            kind,
            source,
            timestamp,
            latency_ms,
            missing_bars,
            stale,
            (Decimal(str(confidence)) if isinstance(confidence, float) else confidence),
            _coerce_notes_to_jsonb_text(notes),
        )
    wrote = row is not None
    logger.debug(
        "tpcore.data_quality.write_row",
        kind=kind,
        source=source,
        timestamp=timestamp.isoformat(),
        wrote=wrote,
    )
    return wrote


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
        """Insert ``score`` as a ``kind='validation'`` row.

        Thin wrapper over :func:`write_row` (the shared low-level path). Both
        freshness checks AND credibility scores flow through here and carry the
        typed metric columns, so the only CHECK-compliant ``kind`` is
        ``validation`` (the Plan-2 shim decision — the per-``kind`` credibility
        split is deferred; ``tpcore/backtest/credibility.py`` is unchanged)."""
        return await write_row(
            self._pool,
            kind=KIND_VALIDATION,
            source=score.source,
            timestamp=score.timestamp,
            notes=score.notes,
            latency_ms=score.latency_ms,
            missing_bars=score.missing_bars,
            stale=score.stale,
            confidence=score.confidence,
        )
