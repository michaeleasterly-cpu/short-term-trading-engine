"""``platform.ingest_manifest`` writer helpers.

The 2026-05-25 P1 trust-audit remediation: every archive-first feed
must write a manifest row that proves what was pulled, where it was
archived, with what checksum and row count, and what happened during
the production load. The table existed schema-only — no producer
wrote to it until this module landed.

Lifecycle per feed run:

    1. ``create_archived_row`` — after the archive CSV is on disk:
       INSERT a row with status='ARCHIVED', archive_path,
       sha256_checksum, archived_row_count, date_range, and the
       provider/source identity. Returns the manifest_id.

    2. ``mark_loaded`` — after the ETL upsert into production
       succeeds: UPDATE status='LOADED' + actual_rows seen by the
       production write.

    3. ``mark_failed`` — when the production load aborts: UPDATE
       status='FAILED' + a short, operator-actionable notes string.
       The archive stays on disk (immutable) and the manifest's
       provenance is preserved.

Status values are case-stable ASCII strings. They are not constrained
by a CHECK on the column today — keeping the canonical set short and
documented here so the registry-coverage tests can assert the
producer never invents a new one.

This module is intentionally adapter-agnostic. Any feed handler can
call into it; the schema is the single source of truth for what a
manifest row looks like.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

import structlog

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)


# Canonical status values written by this module. Adding a new one
# requires updating ``KNOWN_STATUSES`` here AND the producer-side
# sentinel test.
STATUS_ARCHIVED = "archived"  # archive on disk, checksum recorded, prod load pending
STATUS_LOADED = "loaded"      # archive on disk + prod write succeeded
STATUS_FAILED = "failed"      # archive on disk + prod write failed; investigate

KNOWN_STATUSES: frozenset[str] = frozenset(
    {STATUS_ARCHIVED, STATUS_LOADED, STATUS_FAILED}
)


def compute_sha256(archive_path: Path) -> str:
    """SHA-256 of the archive file's bytes (hex digest).

    Reads the file in 64 KiB chunks so an archive larger than RAM
    doesn't blow up the digest. The file MUST exist; absent files are
    a programmer error and raise FileNotFoundError naturally.
    """
    h = hashlib.sha256()
    with archive_path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


_INSERT_SQL = """
    INSERT INTO platform.ingest_manifest (
        source, provider, pulled_at, source_locator,
        expected_rows, actual_rows,
        status, checksum, date_range_start, date_range_end, notes
    )
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
    RETURNING manifest_id
"""


_UPDATE_SQL = """
    UPDATE platform.ingest_manifest
    SET status = $2, actual_rows = COALESCE($3, actual_rows), notes = $4
    WHERE manifest_id = $1
"""


async def create_archived_row(
    pool: asyncpg.Pool,
    *,
    source: str,
    provider: str,
    archive_path: str,
    archived_row_count: int,
    checksum: str,
    expected_rows: int | None = None,
    date_range_start: datetime | None = None,
    date_range_end: datetime | None = None,
    pulled_at: datetime | None = None,
    notes_payload: dict | None = None,
) -> UUID:
    """Insert a fresh manifest row with status=ARCHIVED.

    Called AFTER the archive write succeeded — the archive is the
    durable substrate this row provenance-proves. Returns the new
    manifest_id (UUID). The caller persists it for the subsequent
    mark_loaded / mark_failed update.

    ``actual_rows`` initialises to the archived count: a successful
    LOAD updates it to the production-write count, a FAILED LOAD
    leaves the archive count visible so the operator can see how
    much we *had* on disk.

    ``provider`` is the concrete data-feed identity from
    ``tpcore.providers.ProviderBinding`` (e.g. ``"fmp"``, ``"alpaca"``,
    ``"sec_edgar"``), distinct from ``source`` which names the logical
    feed (e.g. ``"alpaca_daily_bars"``, ``"fmp_daily_bars"``).

    ``date_range_start``/``end`` should be the publish-date window the
    pull covers — used by ops/audit to query "what was the manifest
    for the X→Y date window?" Always populate when the window is
    deterministic (it is for prices_daily; not for snapshots).
    """
    pulled = pulled_at or datetime.now(UTC)
    notes_str = (
        json.dumps(notes_payload, separators=(",", ":"))
        if notes_payload else None
    )
    async with pool.acquire() as conn:
        manifest_id = await conn.fetchval(
            _INSERT_SQL,
            source, provider, pulled, archive_path,
            expected_rows, archived_row_count,
            STATUS_ARCHIVED, checksum,
            (date_range_start.date() if isinstance(date_range_start, datetime)
             else date_range_start),
            (date_range_end.date() if isinstance(date_range_end, datetime)
             else date_range_end),
            notes_str,
        )
    logger.info(
        "ingest_manifest.archived",
        manifest_id=str(manifest_id), source=source, provider=provider,
        archive_path=archive_path, archived_row_count=archived_row_count,
        checksum=checksum,
    )
    return manifest_id


async def mark_loaded(
    pool: asyncpg.Pool,
    manifest_id: UUID,
    *,
    actual_rows: int,
    notes: str | None = None,
) -> None:
    """Transition manifest from ARCHIVED → LOADED.

    Called after the production-write ETL succeeded. ``actual_rows``
    is what the production table actually received (which may differ
    from the archive's row count: physical-truth-rejection drops bad
    rows; ON CONFLICT no-ops drop duplicates). The difference is
    operator-visible: archived 12345, loaded 12340 → 5 dropped.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            _UPDATE_SQL, manifest_id, STATUS_LOADED, actual_rows, notes,
        )
    logger.info(
        "ingest_manifest.loaded",
        manifest_id=str(manifest_id), actual_rows=actual_rows,
    )


async def mark_failed(
    pool: asyncpg.Pool,
    manifest_id: UUID,
    *,
    error_summary: str,
    actual_rows: int | None = None,
) -> None:
    """Transition manifest from ARCHIVED → FAILED.

    Called when the production-write ETL aborted. The archive on disk
    is preserved (immutable record of what we had); the manifest row
    records why it didn't land. ``actual_rows`` may be NULL if no
    rows were written, or a partial count if some loaded before the
    error.

    ``error_summary`` should be short, operator-actionable, and never
    contain PII or secrets. Truncated at 2000 chars defensively.
    """
    summary = (error_summary or "")[:2000]
    async with pool.acquire() as conn:
        await conn.execute(
            _UPDATE_SQL, manifest_id, STATUS_FAILED, actual_rows, summary,
        )
    logger.warning(
        "ingest_manifest.failed",
        manifest_id=str(manifest_id),
        actual_rows=actual_rows,
        error_summary=summary,
    )


__all__ = [
    "STATUS_ARCHIVED",
    "STATUS_LOADED",
    "STATUS_FAILED",
    "KNOWN_STATUSES",
    "compute_sha256",
    "create_archived_row",
    "mark_loaded",
    "mark_failed",
]
