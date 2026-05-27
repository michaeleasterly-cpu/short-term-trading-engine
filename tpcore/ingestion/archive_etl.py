"""Archive-first ETL orchestrators for ingestion handlers.

The 2026-05-25 P1 + PR-3 trust-audit remediation: every production
write to a ``platform.*`` feed table must be preceded by an immutable
archive write + manifest row.

Two surfaces:

- :func:`archive_first_load_bars` — prices_daily-specific orchestrator
  (PR-2). Wraps the daily_bars fan-out's archive + manifest + upsert
  contract end-to-end; per-feed knowledge baked in.
- :func:`manifest_lifecycle` — generic async context manager (PR-3).
  Phase 1 (archive write + manifest INSERT) on entry; Phase 3
  (mark loaded / failed) on exit. The caller owns Phase 2 (the
  per-feed read-archive + upsert). Used by the 3 sibling feeds
  (corporate_actions, fundamentals_quarterly, earnings_events)
  whose upsert call signatures differ enough that a single
  orchestrator function would be over-fitting.

Phases for both surfaces:

    1. ARCHIVE — write the gzipped CSV to disk, compute SHA-256,
       INSERT an ``ingest_manifest`` row with status='archived'.

    2. ETL — read the archive CSV BACK FROM DISK (the FILE, not
       the in-memory list — archive-as-substrate invariant), do the
       per-feed upsert.

    3. MARK — on success: UPDATE manifest status='loaded' +
       actual_rows. On exception: UPDATE status='failed' + error
       summary; re-raise (mark_failed best-effort).

A failed Phase 1 (write_archive or manifest INSERT raises) means
no production write happens at all. A failed Phase 2 leaves the
manifest at status='failed', archive preserved on disk, exception
propagates.
"""

from __future__ import annotations

import csv
import gzip
import io
from collections import defaultdict
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID  # noqa: F401 — forward-ref usage inside dataclass

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

    # Backend-agnostic checksum (2026-05-27 R3/S3 fix): write_archive
    # caches the sha256 of the gzipped body on the result, so we no
    # longer need a local Path to checksum from. Falls back to the
    # legacy compute_sha256(path) read for unit-test fakes that
    # don't populate sha256 on their ArchiveWriteResult.
    checksum = archive_result.sha256
    if checksum is None:
        if not isinstance(archive_path, Path):
            raise RuntimeError(
                "archive_first_load_bars: write_archive returned no sha256 "
                f"AND non-Path archive_path={archive_path!r} — cannot checksum"
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

    # Phase 2 — ETL via the P3 stage-then-promote write path. Each
    # symbol's bars stage into ``platform.prices_daily_staging``
    # tagged with ``staging_run_id = manifest_id``, get batch-
    # validated, then promote into ``platform.prices_daily`` via SQL
    # INSERT...SELECT honoring the P4 provenance-downgrade guard.
    # ``stage_then_promote_bars`` is imported inside the function so
    # importing this module doesn't pull a live-DB-only dependency at
    # module load time (the hermetic-CI lesson).
    try:
        from tpcore.data.ingest_alpaca_bars import stage_then_promote_bars

        # Backend-agnostic CSV read. Local-FS path keeps reading from
        # disk (preserves the archive-as-substrate invariant — a
        # clobbered file is what the ETL must see, NOT the in-memory
        # input list). S3 backend has no local file we can re-open;
        # fall back to the body bytes write_archive cached.
        if isinstance(archive_path, Path):
            csv_rows = _read_archive_csv(archive_path)
        elif archive_result.body is not None:
            csv_rows = _read_archive_csv_from_bytes(archive_result.body)
        else:
            raise RuntimeError(
                "archive_first_load_bars: non-Path archive_path="
                f"{archive_path!r} AND no body bytes — cannot read archive"
            )
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
            promoted = await stage_then_promote_bars(
                pool, ticker, bars,
                staging_run_id=manifest_id,
                delisted=False, source=upsert_source,
            )
            rows_loaded += promoted

        # Phase 3a — SUCCESS
        await mark_loaded(pool, manifest_id, actual_rows=rows_loaded)
        logger.info(
            "ingestion.archive_first.loaded",
            source=source, provider=provider,
            archive_path=str(archive_path),
            archived_rows=archive_result.rows_written,
            rows_loaded=rows_loaded,
            distinct_tickers=len(by_ticker),
            staging_run_id=str(manifest_id),
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


@dataclass
class _LifecycleCtx:
    """Object yielded from :func:`manifest_lifecycle`.

    Carries the archive path so the caller's Phase 2 reads from there
    (the archive-as-substrate contract), plus the manifest_id so a
    caller that wants to update notes mid-flight can. ``actual_rows``
    defaults to ``archived_row_count``; the caller may overwrite it
    before exit to record what production actually accepted. That value
    is what ``mark_loaded`` persists.

    The 2026-05-26 R3 / S3 fix added ``body``: the gzipped CSV bytes
    the archive backend received. Local-FS callers can keep using
    ``read_archive_csv(ctx.archive_path)``; S3-backend callers (where
    archive_path is an ``s3://...`` URI, not a real local Path) MUST
    use ``read_archive_csv(ctx)`` which falls back to ``ctx.body``
    instead of trying to open the URI as a file.
    """
    archive_path: Path | str
    archived_row_count: int
    manifest_id: UUID
    actual_rows: int = 0
    body: bytes | None = None


@asynccontextmanager
async def manifest_lifecycle(
    pool: asyncpg.Pool,
    *,
    source: str,
    provider: str,
    archive_rows: list[dict],
    fieldnames: list[str],
    validator: Callable[[dict], bool] | None = None,
    date_range_start: date | None = None,
    date_range_end: date | None = None,
    expected_rows: int | None = None,
):
    """Generic archive-first lifecycle for any feed.

    Caller pre-fetches everything into ``archive_rows`` (no DB write).
    On ``async with`` entry: archive lands on disk, manifest row
    INSERTed at status='archived'. Inside the with block: caller
    reads ``ctx.archive_path``, does per-feed upsert, sets
    ``ctx.actual_rows``. On normal exit: manifest → 'loaded'. On
    exception: manifest → 'failed', exception re-raises.

    The caller MUST do production writes by reading the archive
    file (``ctx.archive_path``), not the in-memory ``archive_rows``
    they passed in. This is the archive-as-substrate invariant —
    the prices_daily orchestrator has a dedicated test
    (``test_etl_sees_archive_file_content_not_input_list``) pinning
    this for the bars path; the sibling feeds rely on convention.
    """
    archive_result = write_archive(
        source, archive_rows,
        fieldnames=fieldnames,
        validator=validator,
    )
    archive_path = archive_result.path

    # Backend-agnostic checksum (2026-05-26 fix): write_archive now
    # caches the sha256 of the gzipped body on the result, so we no
    # longer need a local Path to checksum from. Falls back to the
    # legacy compute_sha256(path) read for unit-test fakes that
    # don't populate `sha256` on their ArchiveWriteResult.
    checksum = archive_result.sha256
    if checksum is None:
        if not isinstance(archive_path, Path):
            raise RuntimeError(
                "manifest_lifecycle: write_archive returned no sha256 AND "
                f"non-Path archive_path={archive_path!r} — cannot checksum"
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
        date_range_start=(
            datetime.combine(date_range_start, datetime.min.time())
            if date_range_start else None
        ),
        date_range_end=(
            datetime.combine(date_range_end, datetime.min.time())
            if date_range_end else None
        ),
    )

    ctx = _LifecycleCtx(
        archive_path=archive_path,
        archived_row_count=archive_result.rows_written,
        manifest_id=manifest_id,
        actual_rows=archive_result.rows_written,
    )
    # Stash the gzipped body on the context so backend-agnostic
    # callers can read it via read_archive_csv(ctx) without a
    # backend round-trip. Local-FS callers that pass the path to
    # read_archive_csv(path) keep working unchanged.
    ctx.body = archive_result.body
    try:
        yield ctx
        await mark_loaded(pool, manifest_id, actual_rows=ctx.actual_rows)
        logger.info(
            "ingestion.manifest_lifecycle.loaded",
            source=source, provider=provider,
            archive_path=str(archive_path),
            archived_rows=archive_result.rows_written,
            actual_rows=ctx.actual_rows,
        )
    except Exception as exc:
        try:
            await mark_failed(
                pool, manifest_id,
                error_summary=f"{type(exc).__name__}: {exc!s}",
                actual_rows=ctx.actual_rows or None,
            )
        except Exception as mark_exc:  # noqa: BLE001
            logger.exception(
                "ingestion.manifest_lifecycle.mark_failed_error",
                manifest_id=str(manifest_id),
                mark_failed_error=str(mark_exc),
            )
        raise


def read_archive_csv(source: Path | _LifecycleCtx) -> list[dict[str, str]]:
    """Public wrapper around the internal CSV-archive reader.

    Accepts either:
      * a local ``Path`` (legacy LocalFSBackend caller — reads from disk)
      * a ``_LifecycleCtx`` (backend-agnostic — reads from ctx.body bytes
        captured at write-time, no backend round-trip). The S3-backend
        cutover (R3, 2026-05-21) makes ctx.archive_path an ``s3://``
        URI that cannot be opened as a local file; the body-cache is
        the substrate readers should use going forward.

    Strings that look like local paths fall back to disk read for back-
    compat with the small number of test fixtures that pass a string.
    """
    if isinstance(source, _LifecycleCtx):
        if source.body is not None:
            return _read_archive_csv_from_bytes(source.body)
        # Backend-aware fallback for legacy ctx without body: local-FS
        # callers still have a real Path; S3-backend with no body is a
        # programmer error (write_archive should have populated it).
        if isinstance(source.archive_path, Path):
            return _read_archive_csv(source.archive_path)
        raise RuntimeError(
            "read_archive_csv(ctx): ctx.body is None and archive_path "
            f"={source.archive_path!r} is not a local Path. The S3-"
            "backend handler must rely on body — verify write_archive "
            "populated ArchiveWriteResult.body."
        )
    return _read_archive_csv(source)


def _read_archive_csv_from_bytes(body: bytes) -> list[dict[str, str]]:
    """In-memory equivalent of :func:`_read_archive_csv` for the body
    bytes write_archive captured at write time. Backend-agnostic."""
    with gzip.open(io.BytesIO(body), "rb") as gz:
        text = io.TextIOWrapper(gz, encoding="utf-8", newline="")
        return list(csv.DictReader(text))


__all__ = [
    "archive_first_load_bars",
    "manifest_lifecycle",
    "read_archive_csv",
]
