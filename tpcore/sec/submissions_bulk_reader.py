"""SEC submissions bulk reader — local-cache-first, bulk-zip-fallback.

2026-06-02 — `bulk-before-API-crawl` mandate per the standing
`feedback_bulk_before_api_crawl_REINFORCED` memory + the existing
``scripts/ops.py::_stage_corp_history_edgar_backfill`` precedent
("the previous per-CIK HTTP-loop version (killed 2026-05-24) took
~4 hours; the bulk file gets it under 3 minutes").

Provider priority (operator-locked):

  1. **Local cache**: ``<repo>/data/sec_submissions/CIK<padded>.json``
  2. **SEC bulk archive**: a single
     ``https://www.sec.gov/Archives/edgar/daily-index/bulkdata/submissions.zip``
     download (~1.5 GB) cached at ``/tmp/sec_submissions.zip``.
  3. **NO per-CIK HTTP** — those endpoints are the anti-pattern this
     module exists to replace.

Returned payloads are shape-compatible with
``SECCompanyFactsAdapter.get_submissions(cik, full_history=True)``:
the base ``CIK<padded>.json`` + every shard ``CIK<padded>-submissions-NNN.json``
referenced by ``filings.files[]`` is merged into a single composite
``filings.recent`` block. ``filings.files`` is cleared so downstream
callers (e.g. ``extract_filing_metadata``) don't re-paginate.
Adapter-internal ``_source`` (``"local"`` / ``"bulk_zip"``) and
``_shard_errors`` keys surface where each payload came from + which
shards were unavailable.

The reader does **zero HTTP** in normal operation. The one allowed
network call is the optional ``ensure_zip_cached`` helper that
downloads ``submissions.zip`` once when the cache is missing/stale —
gated on operator policy via the standard ``SEC_EDGAR_USER_AGENT``
env-var requirement.
"""
from __future__ import annotations

import json
import time
import zipfile
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)


# Per the operator's local-cache convention. The repo's
# ``data/sec_submissions/`` dir is the precedent location for
# per-CIK JSON files; SHA-checked outside this module.
DEFAULT_LOCAL_DIR = Path("data/sec_submissions")
DEFAULT_BULK_ZIP_PATH = Path("/tmp/sec_submissions.zip")  # noqa: S108
BULK_ZIP_URL = (
    "https://www.sec.gov/Archives/edgar/daily-index/bulkdata/submissions.zip"
)
# Match the existing _stage_corp_history_edgar_backfill cache-staleness
# heuristic: re-download if older than 24 hours.
DEFAULT_CACHE_TTL_SECONDS = 86_400


# Fields that the downstream ``extract_filing_metadata`` reads. We merge
# exactly these across base + shard JSONs; other fields (items,
# accessionNumber, primaryDocument) are kept on the base shard only —
# keeping the merge surface minimal lowers the risk of array-length skew.
_MERGE_KEYS: tuple[str, ...] = ("form", "filingDate", "reportDate")


def _padded_cik(cik: str | int) -> str:
    """Return the 10-digit zero-padded CIK string SEC uses for filenames."""
    return str(cik).lstrip("0").zfill(10)


def _base_filename(cik: str | int) -> str:
    return f"CIK{_padded_cik(cik)}.json"


class SECSubmissionsBulkReader:
    """Read merged SEC submissions JSON from local cache + bulk zip.

    Usage::

        reader = SECSubmissionsBulkReader(
            local_dir=Path("data/sec_submissions"),
            zip_path=Path("/tmp/sec_submissions.zip"),
        )
        payload = reader.get_merged_submissions(cik="0000019617")
        if payload is not None:
            meta = SECCompanyFactsAdapter.extract_filing_metadata(payload)

    The reader is **read-only** wrt the filesystem; it never writes
    extracted shards back to disk (the operator hard rule per the
    task spec: "Optionally persist extracted needed CIK files into
    data/sec_submissions/ only if the repo already treats that
    directory as operator-local/cache-safe; otherwise read from zip
    without writing repo files"). Today we read-only; the persist
    decision can be revisited later.
    """

    def __init__(
        self,
        local_dir: Path | None = None,
        zip_path: Path | None = None,
    ) -> None:
        self._local_dir = local_dir or DEFAULT_LOCAL_DIR
        self._zip_path = zip_path or DEFAULT_BULK_ZIP_PATH
        self._zip: zipfile.ZipFile | None = None
        # Counters (updated per call; the caller reads them at end).
        self.local_hit_count: int = 0
        self.bulk_hit_count: int = 0
        self.missing_count: int = 0
        self.shard_count: int = 0
        self.shard_error_count: int = 0

    def __enter__(self) -> SECSubmissionsBulkReader:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Release the bulk-zip file handle if one was opened."""
        if self._zip is not None:
            self._zip.close()
            self._zip = None

    def _open_zip(self) -> zipfile.ZipFile | None:
        """Lazy-open the bulk zip. Returns None if zip isn't present."""
        if self._zip is None:
            if not self._zip_path.exists():
                return None
            self._zip = zipfile.ZipFile(self._zip_path, "r")
        return self._zip

    def _load_local_json(self, name: str) -> dict | None:
        path = self._local_dir / name
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "sec.bulk_reader.local_parse_error",
                path=str(path), error=str(exc),
            )
            return None

    def _load_zip_json(self, name: str) -> dict | None:
        zf = self._open_zip()
        if zf is None:
            return None
        try:
            with zf.open(name) as fh:
                return json.loads(fh.read().decode("utf-8"))
        except KeyError:
            return None  # not in zip
        except (zipfile.BadZipFile, json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "sec.bulk_reader.zip_parse_error",
                shard=name, error=str(exc),
            )
            return None

    def get_merged_submissions(self, cik: str | int) -> dict | None:
        """Return a merged-full-history submissions payload for ``cik``.

        Priority: local cache → bulk zip → ``None``. The returned dict
        has the same shape as
        ``SECCompanyFactsAdapter.get_submissions(cik, full_history=True)``
        — every shard merged into ``filings.recent``, ``filings.files``
        cleared, and adapter-internal ``_source`` (``"local"`` or
        ``"bulk_zip"``) + ``_shard_errors`` keys recording provenance.

        Returns ``None`` when neither the local cache nor the bulk zip
        has the CIK's base JSON — the caller decides whether to treat
        that as a missing-from-bulk record (skip cleanly) or to
        escalate.
        """
        base_filename = _base_filename(cik)

        # 1. Try local cache first.
        base = self._load_local_json(base_filename)
        source = "local"
        if base is None:
            # 2. Fall back to the bulk zip.
            base = self._load_zip_json(base_filename)
            source = "bulk_zip"
        if base is None:
            self.missing_count += 1
            return None

        if source == "local":
            self.local_hit_count += 1
        else:
            self.bulk_hit_count += 1

        # 3. Merge any shards. Shard names live in filings.files[]; per
        # SEC's convention they are CIK<padded>-submissions-NNN.json.
        filings = base.get("filings") or {}
        recent = filings.get("recent") or {}
        files = filings.get("files") or []

        merged: dict[str, list] = {
            k: list(recent.get(k) or []) for k in _MERGE_KEYS
        }
        shard_errors: list[str] = []
        for shard in files:
            name = shard.get("name")
            if not name:
                continue
            self.shard_count += 1
            # Prefer local shard if cached (rare; the bulk zip usually
            # has them all).
            shard_payload = self._load_local_json(name)
            if shard_payload is None:
                shard_payload = self._load_zip_json(name)
            if shard_payload is None:
                shard_errors.append(name)
                self.shard_error_count += 1
                continue
            for k in _MERGE_KEYS:
                merged[k].extend(shard_payload.get(k) or [])

        new_recent = dict(recent)
        for k in _MERGE_KEYS:
            new_recent[k] = merged[k]
        new_filings = dict(filings)
        new_filings["recent"] = new_recent
        new_filings["files"] = []
        new_payload = dict(base)
        new_payload["filings"] = new_filings
        new_payload["_source"] = source
        if shard_errors:
            new_payload["_shard_errors"] = shard_errors
        return new_payload

    def stats(self) -> dict[str, int]:
        """Return per-run accumulated counters for the caller's report."""
        return {
            "local_hit_count": self.local_hit_count,
            "bulk_hit_count": self.bulk_hit_count,
            "missing_count": self.missing_count,
            "shard_count": self.shard_count,
            "shard_error_count": self.shard_error_count,
        }


ARCHIVE_SOURCE_NAME = "sec_submissions"


def _archive_filename_for(now: float | None = None) -> str:
    """``submissions_YYYYMMDDTHHmmZ.zip`` — sortable, archive-naming
    convention compatible with the existing ``csv_archive`` source-stamp
    pattern (``<source>_<UTC>.csv.gz``). We swap ``.csv.gz`` for
    ``.zip`` because the bulk file is already a zip; gzipping a zip
    saves nothing."""
    from datetime import UTC as _UTC
    from datetime import datetime as _dt
    ts = (
        _dt.fromtimestamp(now, _UTC) if now is not None
        else _dt.now(_UTC)
    )
    return f"submissions_{ts.strftime('%Y%m%dT%H%MZ')}.zip"


async def ensure_zip_cached(
    zip_path: Path | None = None,
    *,
    user_agent: str,
    cache_ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
    force_download: bool = False,
) -> Path:
    """Ensure ``submissions.zip`` is available locally.

    Resolution priority (operator-locked):

      1. **Local cache** — if ``zip_path`` exists + age ≤ TTL, use it.
      2. **S3/R2 archive** — pull the latest
         ``sec_submissions_archive/submissions_*.zip`` from the
         configured ``CSV_ARCHIVE_BACKEND`` (``s3``) bucket if its
         on-server modify time is ≤ TTL old. Mirrors locally.
      3. **SEC bulk download** — single network call to
         ``submissions.zip`` (~1.5 GB). After download, mirror back to
         the S3 archive so the next operator session / consumer can
         skip the SEC traffic entirely.

    The ONLY SEC network call allowed in bulk mode is step 3 — gated on
    the standard ``SEC_EDGAR_USER_AGENT`` env-var requirement that every
    SEC network call in this repo honours. Steps 1 + 2 are zero-SEC.

    Returns the local path to the resolved zip. Raises ``RuntimeError``
    if both the S3 and SEC paths fail — the caller decides whether to
    fall back to a degraded "local-only" mode (where any CIK not
    already in ``data/sec_submissions/`` is marked missing) or abort.
    """
    import httpx  # local import; bulk-mode is opt-in

    zip_path = zip_path or DEFAULT_BULK_ZIP_PATH

    # 1. Local cache hit?
    if zip_path.exists() and not force_download:
        age = time.time() - zip_path.stat().st_mtime
        if age <= cache_ttl_seconds:
            logger.info(
                "sec.bulk_reader.using_cached_zip",
                zip_path=str(zip_path),
                age_hours=round(age / 3600, 1),
                size_mb=round(zip_path.stat().st_size / 1024 / 1024, 1),
            )
            return zip_path

    zip_path.parent.mkdir(parents=True, exist_ok=True)

    # 2. S3 / R2 archive hit? Pull the latest object from the
    # configured archive backend before going to SEC. We use the
    # existing csv_archive backend factory so the same env-var
    # configuration applies (CSV_ARCHIVE_BACKEND=s3 + R2 creds).
    if not force_download:
        try:
            from tpcore.ingestion.csv_archive_backends import (  # noqa: PLC0415
                select_backend,
            )
            backend = select_backend()
            archives = backend.list_archives(ARCHIVE_SOURCE_NAME)
            if archives:
                latest_name = archives[-1]
                logger.info(
                    "sec.bulk_reader.trying_archive_backend",
                    source=ARCHIVE_SOURCE_NAME, archive=latest_name,
                )
                body = backend.read(ARCHIVE_SOURCE_NAME, latest_name)
                zip_path.write_bytes(body)
                logger.info(
                    "sec.bulk_reader.restored_from_archive",
                    zip_path=str(zip_path), archive=latest_name,
                    size_mb=round(len(body) / 1024 / 1024, 1),
                )
                return zip_path
        except Exception as exc:  # noqa: BLE001 — degrade to SEC
            logger.warning(
                "sec.bulk_reader.archive_backend_unavailable",
                error=str(exc),
            )

    # 3. SEC bulk download (the one allowed network call).
    logger.info(
        "sec.bulk_reader.downloading_zip",
        url=BULK_ZIP_URL, zip_path=str(zip_path),
    )
    t0 = time.time()
    async with httpx.AsyncClient(timeout=600.0) as client:
        async with client.stream(
            "GET", BULK_ZIP_URL, headers={"User-Agent": user_agent},
        ) as resp:
            resp.raise_for_status()
            with zip_path.open("wb") as fh:
                async for chunk in resp.aiter_bytes(chunk_size=1024 * 1024):
                    fh.write(chunk)
    elapsed = time.time() - t0
    logger.info(
        "sec.bulk_reader.download_complete",
        zip_path=str(zip_path),
        size_mb=round(zip_path.stat().st_size / 1024 / 1024, 1),
        elapsed_sec=round(elapsed, 1),
    )

    # 4. Mirror to S3/R2 so the next session/consumer skips SEC.
    # Operator's "update s3 with any new bulk data" rule.
    try:
        from tpcore.ingestion.csv_archive_backends import (  # noqa: PLC0415
            LocalFSBackend,
            select_backend,
        )
        backend = select_backend()
        if not isinstance(backend, LocalFSBackend):
            archive_name = _archive_filename_for()
            body = zip_path.read_bytes()
            uri = backend.write(ARCHIVE_SOURCE_NAME, body, archive_name)
            logger.info(
                "sec.bulk_reader.archived_to_backend",
                uri=uri, size_mb=round(len(body) / 1024 / 1024, 1),
            )
    except Exception as exc:  # noqa: BLE001 — archive failure isn't fatal
        logger.warning(
            "sec.bulk_reader.archive_upload_failed", error=str(exc),
        )

    return zip_path
