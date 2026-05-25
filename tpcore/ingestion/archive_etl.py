"""Archive-first ETL orchestrator for prices_daily ingestion.

The 2026-05-25 P1 trust-audit remediation: every production write to
``platform.prices_daily`` must be preceded by an immutable archive
write + manifest row. This module enforces that contract for the
daily_bars stage.

Phases (per stage run):

    1. ARCHIVE — caller has the in-memory ``archive_rows`` list (one
       dict per bar). ``archive_first_load_bars`` writes the gzipped
       CSV to ``data/<source>_archive/<source>_<stamp>.csv.gz``,
       computes the SHA-256 of the on-disk file, INSERTs an
       ``ingest_manifest`` row with status='ARCHIVED'.

    2. ETL — read the archive CSV back from disk (the FILE, not the
       in-memory list — proves the substrate goes through archive),
       group by ticker, call ``_upsert_bars`` per ticker. Track the
       total rows inserted.

    3. MARK — on success: UPDATE the manifest row to
       status='LOADED' + actual_rows = total upserted. On any
       exception: UPDATE to status='FAILED' + error summary, then
       re-raise so the caller's exit-code contract holds.

A failed archive write (Phase 1 raises) means no production write
happens at all — the manifest row is never created, the upsert
loop never starts. A failed ETL (Phase 2 raises) means the
manifest row stays as FAILED (Phase 3), the archive on disk is
preserved (immutable), and the next ops run can either re-attempt
the ETL from the archive (rebuild_from_archive) or re-pull the
window (new manifest).
"""

from __future__ import annotations

import csv
import gzip
import io
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from tpcore.ingestion.csv_archive import write_archive
from tpcore.ingestion.manifest import (
    compute_sha256,
    create_archived_row,
    mark_failed,
    mark_loaded,
)

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)


_BARS_FIELDS: list[str] = [
    "ticker", "date", "open", "high", "low", "close", "volume", "vwap",
]


def _bars_validator(r: dict) -> bool:
    """write_archive validator — both ticker AND date must be populated."""
    return bool(r.get("ticker")) and r.get("date") not in ("", None)


def _read_archive_csv(archive_path: Path) -> list[dict[str, str]]:
    """Parse the on-disk gzipped CSV back into row dicts.

    The function deliberately reads from the FILE — never from the
    caller's in-memory list — so the archive-as-substrate contract
    is enforced structurally. If the file is missing, this raises
    FileNotFoundError (a programmer error: write_archive must have
    landed before this is called).
    """
    with gzip.open(archive_path, "rb") as gz:
        text = io.TextIOWrapper(gz, encoding="utf-8", newline="")
        return list(csv.DictReader(text))


def _row_to_alpaca_bar_dict(row: dict[str, str]) -> dict:
    """Convert a CSV-archive row back to the in-memory bar shape that
    ``_upsert_bars`` consumes (``{"t","o","h","l","c","v","vw"}``).

    The CSV columns are the canonical ``ticker, date, open, high,
    low, close, volume, vwap``; the production upsert expects the
    Alpaca/FMP-shape keys with the per-vendor short letters. OHLCV
    fields are coerced back to numeric — CSV reads them as strings,
    but ``_upsert_bars``'s physical-truth gate compares numerically.
    Empty fields stay empty (``_upsert_bars`` already handles those).
    """
    def _num(s: str) -> float | str:
        if s == "" or s is None:
            return ""
        try:
            return float(s)
        except (ValueError, TypeError):
            return s

    return {
        "t": row.get("date", ""),
        "o": _num(row.get("open", "")),
        "h": _num(row.get("high", "")),
        "l": _num(row.get("low", "")),
        "c": _num(row.get("close", "")),
        "v": _num(row.get("volume", "")),
        "vw": _num(row.get("vwap", "")),
    }


async def archive_first_load_bars(
    pool: asyncpg.Pool,
    *,
    archive_rows: list[dict],
    source: str,
    provider: str,
    date_range_start: date,
    date_range_end: date,
    expected_rows: int | None = None,
) -> tuple[int, str]:
    """Archive-first orchestrator for the daily_bars stage.

    Phases: archive on disk → manifest ARCHIVED → ETL from archive →
    manifest LOADED (success) or FAILED (re-raise). Returns
    ``(rows_loaded, archive_path)``.

    Args:
        pool: production DB pool.
        archive_rows: in-memory rows (one bar dict per row); ALREADY
            pulled by the caller (the function does no network IO).
        source: canonical archive label
            (e.g. ``"fmp_daily_bars"``, ``"alpaca_daily_bars"``).
        provider: concrete ProviderBinding identity
            (e.g. ``"fmp"``, ``"alpaca"``).
        date_range_start/end: the publish-date window pulled (for
            the manifest row, audit reads).
        expected_rows: if known up-front (rarely is for variable-
            window pulls), passed through to the manifest.

    Returns:
        Tuple ``(rows_loaded, archive_path)``.

    Raises:
        Re-raises any exception from Phase 1 (archive write) or
        Phase 2 (ETL upsert) — the caller's exit-code contract is
        unchanged. Phase 3 (mark_failed) is best-effort and never
        masks the original exception.
    """
    # Phase 1 — ARCHIVE (no DB write until this succeeds).
    # Per-source literal call sites so the
    # ``adapter_contract.contract_drift`` regex still detects the feed
    # name (the regex matches the canonical-source literal as the
    # first arg). Generic dispatch on a variable source would silently
    # disable the drift sentinel for these two feeds.
    if source == "fmp_daily_bars":
        archive_result = write_archive(
            "fmp_daily_bars", archive_rows,
            fieldnames=_BARS_FIELDS,
            validator=_bars_validator,
        )
    elif source == "alpaca_daily_bars":
        archive_result = write_archive(
            "alpaca_daily_bars", archive_rows,
            fieldnames=_BARS_FIELDS,
            validator=_bars_validator,
        )
    else:
        archive_result = write_archive(
            source, archive_rows,
            fieldnames=_BARS_FIELDS,
            validator=_bars_validator,
        )
    archive_path = archive_result.path
    if not isinstance(archive_path, Path):
        # S3 backend returns a URI string; bail with a structured
        # error rather than try to checksum a remote object — the
        # archive-first invariant needs a local file we can hash.
        # The R3 substrate (object-storage backend) needs its own
        # manifest helper; tracked but out of P1 scope.
        raise NotImplementedError(
            "archive_first_load_bars requires LocalFSBackend; "
            f"got non-Path archive_path={archive_path!r} — "
            "S3/object-storage manifest path is the R3 follow-up"
        )

    checksum = compute_sha256(archive_path)
    manifest_id = await create_archived_row(
        pool,
        source=source,
        provider=provider,
        archive_path=str(archive_path),
        archived_row_count=archive_result.rows_written,
        checksum=checksum,
        expected_rows=expected_rows,
        date_range_start=datetime.combine(date_range_start, datetime.min.time()),
        date_range_end=datetime.combine(date_range_end, datetime.min.time()),
    )

    # Phase 2 — ETL (read archive file, group by ticker, upsert).
    # _upsert_bars is imported inside the function so importing this
    # module doesn't pull a live-DB-only dependency at module load
    # time (the hermetic-CI lesson).
    try:
        from tpcore.data.ingest_alpaca_bars import _upsert_bars

        csv_rows = _read_archive_csv(archive_path)
        by_ticker: dict[str, list[dict]] = defaultdict(list)
        for r in csv_rows:
            t = r.get("ticker", "").strip()
            if not t:
                continue
            by_ticker[t].append(_row_to_alpaca_bar_dict(r))

        # Provider-specific source label propagates to the
        # ``prices_daily.source`` column. "fmp" / "alpaca" honor the
        # provenance contract; rebuild_from_archive stays the
        # historical path and is intentionally separate.
        upsert_source = (
            "fmp" if provider == "fmp"
            else "alpaca" if provider == "alpaca"
            else provider
        )
        rows_loaded = 0
        for ticker, bars in by_ticker.items():
            inserted = await _upsert_bars(
                pool, ticker, bars, delisted=False, source=upsert_source,
            )
            rows_loaded += inserted

        # Phase 3a — SUCCESS
        await mark_loaded(pool, manifest_id, actual_rows=rows_loaded)
        logger.info(
            "ingestion.archive_first.loaded",
            source=source, provider=provider,
            archive_path=str(archive_path),
            archived_rows=archive_result.rows_written,
            rows_loaded=rows_loaded,
            distinct_tickers=len(by_ticker),
        )
        return rows_loaded, str(archive_path)
    except Exception as exc:
        # Phase 3b — FAIL. Best-effort manifest update; never swallow
        # the original error.
        try:
            await mark_failed(
                pool, manifest_id,
                error_summary=f"{type(exc).__name__}: {exc!s}",
            )
        except Exception as mark_exc:  # noqa: BLE001
            logger.exception(
                "ingestion.archive_first.mark_failed_error",
                manifest_id=str(manifest_id),
                mark_failed_error=str(mark_exc),
            )
        raise


__all__ = [
    "archive_first_load_bars",
]
