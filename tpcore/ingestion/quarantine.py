"""``platform.ingest_quarantine`` writer helper.

The 2026-05-25 P5 trust-audit remediation: rows rejected by an
ingestion handler's physical-truth gate (and any other "this row
won't go to production" code path) must land in the quarantine
table — not just be counted in a log line. The quarantine row
carries the raw payload, the error message + kind, and a retry
timeline; operators can audit failed records over time and decide
whether to re-attempt or abandon.

Schema (per migration ``20260525_0200``):

    quarantine_id    UUID PK (default gen_random_uuid)
    source           TEXT — canonical feed name (e.g. "fmp_daily_bars")
    target_table     TEXT — fully-qualified table that rejected the row
                            (e.g. "platform.prices_daily")
    payload          JSONB — the row as the producer saw it (pre-cleanup)
    error_message    TEXT — the human-readable reason
    error_kind       TEXT — one of: parse / validation / fk_violation /
                            unique_violation / check_violation /
                            type_coercion / other (CHECK constraint)
    rejected_at      TIMESTAMPTZ
    retry_count      INT default 0
    retry_status     TEXT — pending / retried_ok / retried_failed /
                            abandoned (CHECK constraint)
    manifest_id      UUID NULLABLE — links back to the producing
                            ``ingest_manifest`` row for end-to-end
                            provenance

Error kinds defined here as named constants so callers don't
stringly-type into the CHECK-constraint allowed set.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from uuid import UUID

import structlog

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)


# CHECK-constraint allowed values for ``error_kind``. Adding a new
# kind requires a migration to extend the CHECK as well.
ERROR_PARSE = "parse"               # JSON / CSV parse failure
ERROR_VALIDATION = "validation"     # physical-truth / business-rule reject
ERROR_FK_VIOLATION = "fk_violation"
ERROR_UNIQUE_VIOLATION = "unique_violation"
ERROR_CHECK_VIOLATION = "check_violation"
ERROR_TYPE_COERCION = "type_coercion"  # bad cast, e.g. non-numeric "ratio"
ERROR_OTHER = "other"

KNOWN_ERROR_KINDS: frozenset[str] = frozenset({
    ERROR_PARSE, ERROR_VALIDATION, ERROR_FK_VIOLATION,
    ERROR_UNIQUE_VIOLATION, ERROR_CHECK_VIOLATION,
    ERROR_TYPE_COERCION, ERROR_OTHER,
})


_INSERT_SQL = """
    INSERT INTO platform.ingest_quarantine (
        source, target_table, payload, error_message, error_kind, manifest_id
    )
    VALUES ($1, $2, $3::jsonb, $4, $5, $6)
    RETURNING quarantine_id
"""


def _coerce_jsonb_safe(payload: object) -> str:
    """Serialize ``payload`` for the JSONB column, tolerating
    non-trivial types (Decimal, date, datetime). ``default=str`` makes
    the function never raise on the producer side — quarantine writes
    are best-effort; a serialization failure should never abort an
    already-failing ingest path."""
    return json.dumps(payload, default=str)


async def record_rejection(
    pool: asyncpg.Pool,
    *,
    source: str,
    target_table: str,
    payload: object,
    error_message: str,
    error_kind: str = ERROR_VALIDATION,
    manifest_id: UUID | None = None,
) -> UUID | None:
    """INSERT one quarantine row.

    Returns the new ``quarantine_id`` on success, or ``None`` if the
    write itself fails (logged + swallowed; the caller's primary
    error path is more important than this audit row).

    ``payload`` is JSON-encoded with ``default=str`` so Decimals,
    dates, and datetimes round-trip without raising. ``error_kind``
    must be one of ``KNOWN_ERROR_KINDS``; the DB CHECK constraint
    would reject an unknown value and this assertion makes the
    failure visible at the producer rather than as a CheckViolation
    surfacing as a swallowed log entry.
    """
    if error_kind not in KNOWN_ERROR_KINDS:
        raise ValueError(
            f"quarantine.record_rejection: error_kind={error_kind!r} not in "
            f"KNOWN_ERROR_KINDS={sorted(KNOWN_ERROR_KINDS)}"
        )
    payload_json = _coerce_jsonb_safe(payload)
    try:
        async with pool.acquire() as conn:
            qid = await conn.fetchval(
                _INSERT_SQL, source, target_table, payload_json,
                error_message, error_kind, manifest_id,
            )
    except Exception as exc:  # noqa: BLE001 — best-effort audit write
        logger.exception(
            "ingest_quarantine.record_rejection_failed",
            source=source, target_table=target_table,
            error_kind=error_kind, error=str(exc),
        )
        return None
    logger.info(
        "ingest_quarantine.recorded",
        quarantine_id=str(qid), source=source,
        target_table=target_table, error_kind=error_kind,
    )
    return qid


__all__ = [
    "ERROR_PARSE",
    "ERROR_VALIDATION",
    "ERROR_FK_VIOLATION",
    "ERROR_UNIQUE_VIOLATION",
    "ERROR_CHECK_VIOLATION",
    "ERROR_TYPE_COERCION",
    "ERROR_OTHER",
    "KNOWN_ERROR_KINDS",
    "record_rejection",
]
