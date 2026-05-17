"""Shared CSV-first archive helpers for ingestion handlers.

The platform pulls data from third-party vendors that may retroactively
truncate, alter, or revoke historical data — the FRED BAMLH0A0HYM2
truncation on 2026-05-15 is the canonical incident. The CSV-first
sub-protocol (docs/superpowers/pipelines/data_adapter_pipeline.md §1
ingest sub-protocol) defends against this by writing every successful
ingest to a local CSV archive *before* loading into Postgres, then
gzipping the CSV on success. The archive becomes the permanent record
of what the source returned at a given moment.

This module factors the archive-write + gzip + shrinkage-detection
logic out of `handle_sec_filings` so the other four ingestion handlers
(handle_daily_bars, handle_macro_indicators, handle_fundamentals_refresh,
handle_corporate_actions) can adopt the same pattern with minimal code.

Archive layout
--------------

``data/<source>_archive/<source>_<YYYYMMDDTHHMMSSZ>.csv.gz``

* ``<source>`` is the canonical name (``alpaca_daily_bars``, ``fred_macro``,
  ``fmp_fundamentals``, ``fmp_corporate_actions``, ``fmp_earnings_events``).
* Run stamp is UTC, second precision — collision-safe enough for daily
  pipelines and human-readable for the operator.

Shrinkage detection
-------------------

After a successful write, ``detect_shrinkage`` compares the new
archive's row count against the immediately-prior archive (if any). If
the row count dropped by more than ``shrinkage_threshold_pct`` (default
20%) it returns a warning payload. The handler can then log a structured
warning, write to ``platform.application_log``, or fail the run
depending on severity policy. **This is the BAMLH0A0HYM2 detector** —
if a vendor silently truncates, the next ingest's CSV row count will be
materially smaller than the prior archive's and we surface it
immediately instead of waiting for the operator to notice the DB
shrink.

#185 Phase 4 decision (kept, not retired): this is a cheap CSV-stage
fail-fast PRE-FILTER, NOT the authoritative definition of "is the feed
good". The canonical per-feed `check_<feed>` (run on-completion via the
#185 Phase 2/3 tripwire and at the end-of-cycle monolithic gate) is
authoritative. Keep this as defense-in-depth catching vendor truncation
before the row even lands; do NOT accrete bespoke validity logic here —
extend the canonical check instead so the two cannot diverge.
"""

from __future__ import annotations

import csv as _csv
import gzip
import shutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)


def repo_data_dir() -> Path:
    """Return the absolute path to ``<repo_root>/data/``."""
    return Path(__file__).resolve().parent.parent.parent / "data"


def archive_dir_for(source: str) -> Path:
    """Return ``data/<source>_archive/`` — creates if missing."""
    d = repo_data_dir() / f"{source}_archive"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _run_stamp(now: datetime | None = None) -> str:
    n = now or datetime.now(UTC)
    return n.strftime("%Y%m%dT%H%M%SZ")


# ─── Write path ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ArchiveWriteResult:
    path: Path           # final ``.csv.gz`` path
    rows_written: int    # rows after physical-truth filtering
    rows_rejected: int   # rows that failed validation


def write_archive(
    source: str,
    rows: Iterable[dict],
    fieldnames: list[str],
    *,
    validator: Callable[[dict], bool] | None = None,
    now: datetime | None = None,
) -> ArchiveWriteResult:
    """Write rows to a fresh archive CSV, gzip on success.

    Args:
        source: canonical source name (e.g. ``"fred_macro"``).
        rows: iterable of dicts; each row is one record.
        fieldnames: explicit column order; mismatched dict keys go in
            (unused) and missing keys default to empty string.
        validator: optional predicate; when supplied, rows for which it
            returns False are *rejected* (not written, counted under
            ``rows_rejected``).
        now: timestamp override for tests.

    Returns:
        :class:`ArchiveWriteResult` with the final ``.csv.gz`` path and
        row counts. Empty input still produces an archive file (zero
        rows) so the operator can prove the ingest ran.
    """
    archive = archive_dir_for(source)
    stamp = _run_stamp(now)
    csv_path = archive / f"{source}_{stamp}.csv"
    written = rejected = 0
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = _csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            if validator is not None:
                try:
                    if not validator(row):
                        rejected += 1
                        continue
                except Exception:  # noqa: BLE001 — bad row → reject, don't crash
                    rejected += 1
                    continue
            # Fill missing keys with empty string.
            w.writerow({k: row.get(k, "") for k in fieldnames})
            written += 1
    # Gzip in place.
    gz_path = _gzip_in_place(csv_path)
    logger.info(
        "csv_archive.write_done",
        source=source, rows_written=written, rows_rejected=rejected,
        path=str(gz_path),
    )
    return ArchiveWriteResult(path=gz_path, rows_written=written, rows_rejected=rejected)


def _gzip_in_place(path: Path) -> Path:
    """Compress ``path`` → ``path.gz`` and remove the original.

    Idempotent: if ``path.gz`` already exists, the source CSV is removed
    and the existing gzip kept. Returns the final ``.csv.gz`` path.
    """
    if not path.exists():
        return path
    gz = path.with_suffix(path.suffix + ".gz")
    if gz.exists():
        path.unlink()
        return gz
    with path.open("rb") as src, gzip.open(gz, "wb") as dst:
        shutil.copyfileobj(src, dst)
    path.unlink()
    return gz


# ─── Read path ──────────────────────────────────────────────────────────


def latest_archive(source: str) -> Path | None:
    """Return the most-recent ``.csv.gz`` for ``source``, or None."""
    archive = archive_dir_for(source)
    candidates = sorted(archive.glob(f"{source}_*.csv.gz"), reverse=True)
    return candidates[0] if candidates else None


def count_archive_rows(path: Path) -> int:
    """Return row count in a ``.csv.gz`` archive (excluding header)."""
    if not path.exists():
        return 0
    with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
        # Count newlines minus header. Faster than full csv parse.
        n = sum(1 for _ in fh) - 1
    return max(0, n)


def read_archive_rows(path: Path) -> Iterable[dict]:
    """Yield rows from a ``.csv.gz`` archive as dicts.

    Lazy — closes the file on generator exhaustion.
    """
    if not path.exists():
        return
    with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
        reader = _csv.DictReader(fh)
        yield from reader


# ─── Shrinkage detection ────────────────────────────────────────────────


@dataclass(frozen=True)
class ShrinkageReport:
    source: str
    current_rows: int
    previous_rows: int
    previous_archive: str
    shrinkage_pct: float    # 0.20 = the new archive is 20% smaller
    over_threshold: bool


def detect_shrinkage(
    source: str,
    current_rows: int,
    *,
    shrinkage_threshold_pct: float = 0.20,
    exclude_path: Path | None = None,
) -> ShrinkageReport | None:
    """Compare ``current_rows`` to the previous archive's row count.

    Args:
        source: canonical source name.
        current_rows: row count of the just-written archive.
        shrinkage_threshold_pct: fraction below which the report flags
            as ``over_threshold=True``. Default 20% — anything tighter
            tends to false-flag during partial-window pulls; anything
            looser misses real truncation events.
        exclude_path: skip this archive when looking for "previous"
            (typically the just-written one).

    Returns:
        :class:`ShrinkageReport` if a previous archive exists; ``None``
        on the first run. Caller decides severity — a positive
        ``over_threshold`` is almost always worth surfacing to
        ``platform.application_log`` at WARNING.
    """
    archive = archive_dir_for(source)
    candidates = sorted(archive.glob(f"{source}_*.csv.gz"), reverse=True)
    if exclude_path is not None:
        candidates = [c for c in candidates if c != exclude_path]
    if not candidates:
        return None
    prev = candidates[0]
    prev_rows = count_archive_rows(prev)
    if prev_rows == 0:
        return None  # avoid div-by-zero; nothing useful to compare
    delta = (prev_rows - current_rows) / prev_rows
    return ShrinkageReport(
        source=source,
        current_rows=current_rows,
        previous_rows=prev_rows,
        previous_archive=str(prev),
        shrinkage_pct=delta,
        over_threshold=delta > shrinkage_threshold_pct,
    )


def log_shrinkage_warning(report: ShrinkageReport) -> None:
    """Emit a structured warning when shrinkage exceeds the threshold.

    The audit script and operator dashboard both surface
    ``csv_archive.shrinkage_detected`` events automatically — no
    additional wiring needed.
    """
    if not report.over_threshold:
        return
    logger.warning(
        "csv_archive.shrinkage_detected",
        source=report.source,
        current_rows=report.current_rows,
        previous_rows=report.previous_rows,
        shrinkage_pct=round(report.shrinkage_pct, 4),
        previous_archive=report.previous_archive,
    )


class ProducerShrinkageError(RuntimeError):
    """A full-snapshot ingest came back materially short of its prior
    archive — a producer defect (broken/partial pull, vendor truncation),
    not legitimate variance. Raised so the stage fails loudly
    (INGESTION_FAILED → no DATA_OPERATIONS_COMPLETE → self-heal /
    escalation), instead of the WARNING being eyeballed past. This is
    the daily_bars producer-guard pattern generalised to every
    full-snapshot source via the EXISTING shrinkage detector — no new
    per-source thresholds."""


def assert_not_shrunk(report: ShrinkageReport | None) -> None:
    """Producer hard-stop: raise if a full-snapshot pull shrank past
    the detector's threshold.

    No-op when ``report`` is None (first run — nothing to compare) or
    not ``over_threshold`` (within tolerated variance). Pair with
    ``log_shrinkage_warning`` so the structured WARNING is still
    emitted for observability before the raise.
    """
    if report is None or not report.over_threshold:
        return
    raise ProducerShrinkageError(
        f"{report.source}: full-snapshot ingest shrank "
        f"{report.shrinkage_pct:.1%} ({report.previous_rows:,} → "
        f"{report.current_rows:,}) vs {report.previous_archive} — "
        f"refusing to report OK on a likely broken/partial pull or "
        f"vendor truncation. Investigate before re-running."
    )


__all__ = [
    "ArchiveWriteResult", "ProducerShrinkageError", "ShrinkageReport",
    "archive_dir_for", "assert_not_shrunk",
    "count_archive_rows", "detect_shrinkage",
    "latest_archive", "log_shrinkage_warning", "read_archive_rows",
    "repo_data_dir", "write_archive",
]
