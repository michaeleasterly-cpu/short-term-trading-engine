"""D2 shrinkage detector — durable per-source rolling-median substrate.

The R3 substrate (S3 object storage, PR #235) moved the archive
recovery path off the local filesystem. D2 — the still-pending half
of the LOCKED 2026-05-18 archive-substrate migration — moves the
DETECTION path: shrinkage is no longer judged against a single-prior
CSV that may not even exist on a fresh Railway deploy, but against
the ROLLING MEDIAN of durable per-source run history.

Plan 2 consolidation (2026-06-04): the dedicated ``platform.ingestion_metrics``
table is DROPPED (migration 0300); D2 run-count history now lives in
``platform.ingest_manifest`` — the existing per-ingest reconciliation
substrate (P4, 2026-05-25). D2 metric rows are tagged
``source_locator = 'd2_metrics'`` + ``provider = 'd2_metrics'`` so they are
trivially separable from the archive-first lifecycle rows
(``manifest_lifecycle`` / ``tpcore.ingestion.manifest``) that share the
table. The reader filters on that tag so the rolling median is computed over
D2 rows ONLY — no double-count against the lifecycle rows.

Two surfaces:

* :func:`record_ingestion_metrics` — call AFTER every successful
  archive write. Persists ``actual_rows`` + optional date range for the
  source as a ``status='ok'`` D2-metric ``ingest_manifest`` row. One INSERT
  per call; ``pulled_at`` server-side ``now()`` keeps it append-only.

* :func:`check_shrinkage_vs_rolling_median` — read the last N D2 rows
  for the source, compute the median, return a ``ShrinkageVerdict``.
  A run that lands materially below the median (default 20%) is
  flagged ``shrunk=True``. The threshold mirrors the v1 single-prior
  detector's default so the two detectors are directly comparable
  during the soak period.

The v1 ``csv_archive.detect_shrinkage`` STAYS in place for this PR.
Both detectors run in parallel — when they disagree the caller
emits a ``SHRINKAGE_DETECTORS_DISAGREE`` event for forensic visibility.
A v2 PR retires the old detector after a defined soak period.

Separability principle: this module reads ONLY the D2-tagged
``platform.ingest_manifest`` rows. The R3 S3 substrate is for recovery —
never read here. The OLD CSV-archive directory is detection-irrelevant
once this module is wired everywhere.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import date
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


# Default rolling window — 10 recent runs absorb single-outlier
# variance without smearing legitimate growth across months. Mirrors
# the existing single-prior detector's 20% threshold default so the
# two detectors are directly comparable during the soak period.
DEFAULT_ROLLING_WINDOW = 10
DEFAULT_SHRINKAGE_THRESHOLD_PCT = 0.20

# Tag distinguishing D2 run-count rows from the archive-first lifecycle rows
# that share platform.ingest_manifest. Written into BOTH provider +
# source_locator (the latter is NOT NULL) so the reader's filter is unambiguous.
_D2_TAG = "d2_metrics"


_INSERT_SQL = """
INSERT INTO platform.ingest_manifest
    (source, provider, source_locator, actual_rows, status,
     date_range_start, date_range_end)
VALUES
    ($1, $2, $2, $3, 'ok', $4, $5)
"""


_RECENT_SQL = """
SELECT actual_rows AS row_count
FROM platform.ingest_manifest
WHERE source = $1 AND source_locator = $2
ORDER BY pulled_at DESC
LIMIT $3
"""


@dataclass(frozen=True)
class ShrinkageVerdict:
    """Result of a rolling-median shrinkage check.

    Fields:
      * ``source`` — canonical source name.
      * ``current_rows`` — what THIS run landed.
      * ``median_rows`` — rolling median of the most recent
        ``rolling_window`` runs (excluding the current run).
      * ``samples_used`` — how many prior rows the median was
        computed over. < ``rolling_window`` during cold-start.
      * ``shrinkage_pct`` — fractional shortfall vs the median.
        Positive means a smaller current run; negative means growth.
      * ``shrunk`` — True iff ``shrinkage_pct >
        shrinkage_threshold_pct`` AND we had at least one prior row
        to compare against.
      * ``cold_start`` — True iff no prior history exists for this
        source — verdict is informational only (never ``shrunk``).
    """
    source: str
    current_rows: int
    median_rows: float
    samples_used: int
    shrinkage_pct: float
    shrunk: bool
    cold_start: bool


async def record_ingestion_metrics(
    pool: Any,
    source: str,
    row_count: int,
    *,
    min_date: date | None = None,
    max_date: date | None = None,
    coverage_pct: float | None = None,
) -> None:
    """Persist a single ingestion-run metric row.

    Call AFTER a successful archive write. Lands a ``status='ok'`` D2-metric
    row in ``platform.ingest_manifest`` (tagged ``source_locator='d2_metrics'``).
    Append-only: ``pulled_at`` is server-side ``now()`` so two simultaneous
    calls land two rows with distinct timestamps; neither overwrites the other.

    ``coverage_pct`` is accepted for back-compat but no longer persisted —
    ``ingest_manifest`` has no such column and D2 only reads ``actual_rows``;
    callers that need coverage should use the validation suite, not this hook.

    Errors are logged + swallowed (parity with ``DBLogHandler.log``).
    A metrics-write failure must never block the producer — the
    upstream archive write is the source of truth for "the data
    landed"; this is observability for the detector.
    """
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                _INSERT_SQL,
                source,
                _D2_TAG,
                int(row_count),
                min_date,
                max_date,
            )
        logger.info(
            "d2_metrics.recorded",
            source=source,
            row_count=int(row_count),
            min_date=min_date.isoformat() if min_date else None,
            max_date=max_date.isoformat() if max_date else None,
            coverage_pct=coverage_pct,
        )
    except Exception as exc:  # noqa: BLE001 — observability, do not block
        logger.warning(
            "d2_metrics.record_failed",
            source=source,
            row_count=int(row_count),
            error=str(exc),
        )


async def check_shrinkage_vs_rolling_median(
    pool: Any,
    source: str,
    current_rows: int,
    *,
    rolling_window: int = DEFAULT_ROLLING_WINDOW,
    shrinkage_threshold_pct: float = DEFAULT_SHRINKAGE_THRESHOLD_PCT,
) -> ShrinkageVerdict:
    """Return a :class:`ShrinkageVerdict` for THIS run's row count
    against the rolling median of recent history for ``source``.

    The current run is excluded from the median — the caller is
    expected to record its metrics row only AFTER the verdict is
    returned (or in parallel; the PK makes it racy-safe). Either
    ordering is fine; this query reads the most recent ``rolling_window``
    rows by ``ingested_at`` regardless.

    Returns a verdict even on cold-start (no prior history): the
    caller can inspect ``cold_start`` to decide whether to emit a
    "no baseline" log entry. ``shrunk`` is FALSE on cold-start by
    construction — there's nothing legitimate to flag.
    """
    if rolling_window < 1:
        raise ValueError(
            f"rolling_window={rolling_window} must be >= 1"
        )

    async with pool.acquire() as conn:
        rows = await conn.fetch(_RECENT_SQL, source, _D2_TAG, rolling_window)
    history = [int(r["row_count"]) for r in rows]

    if not history:
        return ShrinkageVerdict(
            source=source,
            current_rows=int(current_rows),
            median_rows=0.0,
            samples_used=0,
            shrinkage_pct=0.0,
            shrunk=False,
            cold_start=True,
        )

    median = statistics.median(history)
    # Same shape as csv_archive.detect_shrinkage: (median - current)
    # / median — positive means the current is smaller. Guard
    # div-by-zero (an all-zero history is degenerate; treat as
    # cold-start in the verdict).
    if median <= 0:
        return ShrinkageVerdict(
            source=source,
            current_rows=int(current_rows),
            median_rows=float(median),
            samples_used=len(history),
            shrinkage_pct=0.0,
            shrunk=False,
            cold_start=True,
        )
    shrinkage_pct = (median - float(current_rows)) / float(median)
    return ShrinkageVerdict(
        source=source,
        current_rows=int(current_rows),
        median_rows=float(median),
        samples_used=len(history),
        shrinkage_pct=shrinkage_pct,
        shrunk=shrinkage_pct > shrinkage_threshold_pct,
        cold_start=False,
    )


def detectors_disagree(
    v1_over_threshold: bool, v2_verdict: ShrinkageVerdict,
) -> bool:
    """True iff the v1 (single-prior CSV) detector and the v2
    (rolling-median Postgres) detector reach different conclusions
    about whether THIS run shrunk past threshold.

    Soak-period contract: when they disagree the caller emits
    ``SHRINKAGE_DETECTORS_DISAGREE`` for forensic visibility. Once the
    operator declares a stable soak the v1 detector is retired in a
    follow-up PR.

    Cold-start (v2 has no prior history) cannot disagree — neither
    detector flags a fresh source.
    """
    if v2_verdict.cold_start:
        return False
    return v1_over_threshold != v2_verdict.shrunk


__all__ = [
    "DEFAULT_ROLLING_WINDOW",
    "DEFAULT_SHRINKAGE_THRESHOLD_PCT",
    "ShrinkageVerdict",
    "check_shrinkage_vs_rolling_median",
    "detectors_disagree",
    "record_ingestion_metrics",
]
