"""SEC submissions bulk reader — hermetic tests (2026-06-02).

Pins the provider-priority semantics of ``SECSubmissionsBulkReader``:
local-first, zip-fallback, no per-CIK HTTP. Also pins the
``ensure_zip_cached`` S3-archive cycle (pull from S3 if present;
mirror to S3 after downloading from SEC) so future regressions can't
silently break the operator's "update s3 with any new bulk data"
rule.

All tests are hermetic: synthetic JSON payloads + in-memory zips +
tmp_path. No network. No DB. No live SEC.
"""
from __future__ import annotations

import io
import json
import os
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Adapter requires SEC_EDGAR_USER_AGENT to instantiate ... and tests
# use ensure_zip_cached which can require it for the download branch.
os.environ.setdefault("SEC_EDGAR_USER_AGENT", "STE-test test@example.com")

from tpcore.sec.submissions_bulk_reader import (  # noqa: E402
    DEFAULT_BULK_ZIP_PATH,  # noqa: F401  (re-exported test-stable shape)
    SECSubmissionsBulkReader,
    _archive_filename_for,
    _base_filename,
    _padded_cik,
    ensure_zip_cached,
)


def _build_payload(cik_padded: str, forms_recent: list[str],
                   filing_dates: list[str], report_dates: list[str],
                   shards: list[str] | None = None) -> dict:
    return {
        "cik": cik_padded,
        "fiscalYearEnd": "1231",
        "filings": {
            "recent": {
                "form": forms_recent,
                "filingDate": filing_dates,
                "reportDate": report_dates,
            },
            "files": [{"name": s} for s in (shards or [])],
        },
    }


def _build_zip(entries: dict[str, dict]) -> bytes:
    """Build an in-memory submissions.zip with the given entries."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, payload in entries.items():
            zf.writestr(name, json.dumps(payload))
    return buf.getvalue()


# ─── basics ──────────────────────────────────────────────────────────


def test_padded_cik_zfill_logic() -> None:
    assert _padded_cik("19617") == "0000019617"
    assert _padded_cik(19617) == "0000019617"
    assert _padded_cik("0000019617") == "0000019617"


def test_base_filename_convention() -> None:
    assert _base_filename("0000019617") == "CIK0000019617.json"


def test_archive_filename_for_is_sortable_utc() -> None:
    name = _archive_filename_for(now=0)
    assert name == "submissions_19700101T0000Z.zip"


# ─── provider-priority semantics ─────────────────────────────────────


def test_bulk_reader_prefers_local_file_when_present(tmp_path: Path) -> None:
    """Local file exists → local wins; zip not consulted."""
    local_dir = tmp_path / "data" / "sec_submissions"
    local_dir.mkdir(parents=True)
    zip_path = tmp_path / "submissions.zip"

    base = _build_payload("0000019617", ["10-Q"], ["2026-05-01"], ["2026-03-31"])
    (local_dir / "CIK0000019617.json").write_text(json.dumps(base))
    # Zip has a DIFFERENT payload — proves we don't read from it.
    zip_path.write_bytes(_build_zip({
        "CIK0000019617.json": _build_payload(
            "0000019617", ["10-K"], ["2099-01-01"], ["2098-12-31"],
        ),
    }))

    reader = SECSubmissionsBulkReader(local_dir=local_dir, zip_path=zip_path)
    payload = reader.get_merged_submissions("19617")
    assert payload is not None
    assert payload["_source"] == "local"
    assert payload["filings"]["recent"]["form"] == ["10-Q"]
    assert reader.local_hit_count == 1
    assert reader.bulk_hit_count == 0
    assert reader.missing_count == 0
    reader.close()


def test_bulk_reader_falls_back_to_zip_when_local_missing(tmp_path: Path) -> None:
    """No local file → zip is read."""
    local_dir = tmp_path / "data" / "sec_submissions"
    local_dir.mkdir(parents=True)
    zip_path = tmp_path / "submissions.zip"

    base = _build_payload("0000019617", ["10-Q"], ["2026-05-01"], ["2026-03-31"])
    zip_path.write_bytes(_build_zip({"CIK0000019617.json": base}))

    reader = SECSubmissionsBulkReader(local_dir=local_dir, zip_path=zip_path)
    payload = reader.get_merged_submissions("19617")
    assert payload is not None
    assert payload["_source"] == "bulk_zip"
    assert reader.local_hit_count == 0
    assert reader.bulk_hit_count == 1
    reader.close()


def test_bulk_reader_loads_base_json_and_submission_shards(tmp_path: Path) -> None:
    """Base + multiple shards from the zip merge into recent."""
    local_dir = tmp_path / "data" / "sec_submissions"
    local_dir.mkdir(parents=True)
    zip_path = tmp_path / "submissions.zip"

    base = _build_payload(
        "0000019617",
        ["10-Q", "10-Q"],
        ["2026-05-01", "2026-02-01"],
        ["2026-03-31", "2025-12-31"],
        shards=[
            "CIK0000019617-submissions-001.json",
            "CIK0000019617-submissions-002.json",
        ],
    )
    shard_001 = {
        "form": ["10-Q"],
        "filingDate": ["2017-05-01"],
        "reportDate": ["2017-03-31"],
    }
    shard_002 = {
        "form": ["10-K"],
        "filingDate": ["1981-03-30"],
        "reportDate": ["1980-12-31"],
    }
    zip_path.write_bytes(_build_zip({
        "CIK0000019617.json": base,
        "CIK0000019617-submissions-001.json": shard_001,
        "CIK0000019617-submissions-002.json": shard_002,
    }))

    reader = SECSubmissionsBulkReader(local_dir=local_dir, zip_path=zip_path)
    payload = reader.get_merged_submissions("19617")
    assert payload is not None
    recent = payload["filings"]["recent"]
    assert recent["form"] == ["10-Q", "10-Q", "10-Q", "10-K"]
    assert recent["reportDate"] == [
        "2026-03-31", "2025-12-31",
        "2017-03-31",
        "1980-12-31",
    ]
    # files[] consumed.
    assert payload["filings"]["files"] == []
    # Counters: 1 base + 2 shards.
    assert reader.bulk_hit_count == 1
    assert reader.shard_count == 2
    assert reader.shard_error_count == 0
    reader.close()


def test_bulk_reader_reports_missing_cik_cleanly(tmp_path: Path) -> None:
    """No local + no zip entry → returns None + bumps missing_count."""
    local_dir = tmp_path / "data" / "sec_submissions"
    local_dir.mkdir(parents=True)
    zip_path = tmp_path / "submissions.zip"
    zip_path.write_bytes(_build_zip({"CIK0000019617.json": _build_payload(
        "0000019617", ["10-Q"], ["2026-05-01"], ["2026-03-31"],
    )}))

    reader = SECSubmissionsBulkReader(local_dir=local_dir, zip_path=zip_path)
    payload = reader.get_merged_submissions("99999999")
    assert payload is None
    assert reader.missing_count == 1
    reader.close()


def test_bulk_reader_handles_missing_shard_gracefully(tmp_path: Path) -> None:
    """A shard referenced in files[] but absent from the zip lands in
    ``_shard_errors`` without aborting the read."""
    local_dir = tmp_path / "data" / "sec_submissions"
    local_dir.mkdir(parents=True)
    zip_path = tmp_path / "submissions.zip"

    base = _build_payload(
        "0000019617",
        ["10-Q"], ["2026-05-01"], ["2026-03-31"],
        shards=["CIK0000019617-submissions-MISSING.json"],
    )
    zip_path.write_bytes(_build_zip({"CIK0000019617.json": base}))

    reader = SECSubmissionsBulkReader(local_dir=local_dir, zip_path=zip_path)
    payload = reader.get_merged_submissions("19617")
    assert payload is not None
    assert payload["_shard_errors"] == [
        "CIK0000019617-submissions-MISSING.json",
    ]
    assert reader.shard_error_count == 1
    reader.close()


# ─── FPFD regression on JPM-style fixture ────────────────────────────


def test_fpfd_repair_uses_full_bulk_history_for_jpm_style_fixture(tmp_path: Path) -> None:
    """JPM-style fixture: recent shard's earliest date ≠ true earliest.
    The merged payload feeds ``extract_filing_metadata`` and FPFD lands
    on the true earliest from the 1980 shard, not the 2026 recent floor."""
    from tpcore.sec.companyfacts_adapter import SECCompanyFactsAdapter

    local_dir = tmp_path / "data" / "sec_submissions"
    local_dir.mkdir(parents=True)
    zip_path = tmp_path / "submissions.zip"
    base = _build_payload(
        "0000019617",
        ["10-Q"], ["2026-05-01"], ["2026-03-31"],
        shards=["CIK0000019617-submissions-001.json"],
    )
    shard_001 = {
        "form": ["10-K", "10-Q"],
        "filingDate": ["1981-03-30", "1980-11-15"],
        "reportDate": ["1980-12-31", "1980-09-30"],
    }
    zip_path.write_bytes(_build_zip({
        "CIK0000019617.json": base,
        "CIK0000019617-submissions-001.json": shard_001,
    }))
    reader = SECSubmissionsBulkReader(local_dir=local_dir, zip_path=zip_path)
    payload = reader.get_merged_submissions("19617")
    assert payload is not None

    meta = SECCompanyFactsAdapter.extract_filing_metadata(payload)
    # 10-Q is primary (2 vs 10-K's 1). 10-Q reportDates: 2026-03-31 +
    # 1980-09-30 → min = 1980-09-30.
    from datetime import date
    assert meta["first_public_filing_date"] == date(1980, 9, 30)
    reader.close()


# ─── ensure_zip_cached: local + S3 cycle ─────────────────────────────


@pytest.mark.asyncio
async def test_ensure_zip_cached_uses_local_when_fresh(tmp_path: Path) -> None:
    """Fresh local cache → no S3 / SEC traffic."""
    zip_path = tmp_path / "sec_submissions.zip"
    zip_path.write_bytes(b"local-zip-bytes")
    result = await ensure_zip_cached(
        zip_path, user_agent="STE-test test@example.com",
    )
    assert result == zip_path
    # Confirms we did NOT overwrite.
    assert result.read_bytes() == b"local-zip-bytes"


@pytest.mark.asyncio
async def test_ensure_zip_cached_pulls_from_archive_when_local_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No local cache → S3 archive backend has a recent zip → mirror to local."""
    zip_path = tmp_path / "sec_submissions.zip"
    # No local cache.
    assert not zip_path.exists()

    # Fake archive backend.
    fake_body = b"archived-zip-bytes-from-s3"
    fake_backend = MagicMock()
    fake_backend.list_archives = MagicMock(
        return_value=["submissions_20260601T1200Z.zip"],
    )
    fake_backend.read = MagicMock(return_value=fake_body)
    monkeypatch.setattr(
        "tpcore.ingestion.csv_archive_backends.select_backend",
        lambda: fake_backend,
    )
    result = await ensure_zip_cached(
        zip_path, user_agent="STE-test test@example.com",
    )
    assert result == zip_path
    assert result.read_bytes() == fake_body
    fake_backend.list_archives.assert_called_once_with("sec_submissions")
    fake_backend.read.assert_called_once_with(
        "sec_submissions", "submissions_20260601T1200Z.zip",
    )


@pytest.mark.asyncio
async def test_ensure_zip_cached_mirrors_to_s3_after_sec_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No local + no S3 → SEC download → mirror to S3.

    The 'mirror to s3' step is the operator's standing 'update s3 with
    any new bulk data' rule. We assert backend.write() is called with
    the source name 'sec_submissions' and a submissions_*.zip
    filename."""
    zip_path = tmp_path / "sec_submissions.zip"

    # Archive backend: list returns empty; write captures the call.
    fake_backend = MagicMock()
    fake_backend.list_archives = MagicMock(return_value=[])
    fake_backend.write = MagicMock(
        return_value="s3://bucket/sec_submissions_archive/submissions.zip",
    )
    monkeypatch.setattr(
        "tpcore.ingestion.csv_archive_backends.select_backend",
        lambda: fake_backend,
    )
    # Force `not isinstance(backend, LocalFSBackend)` to be True even
    # though our fake backend is a MagicMock — the production guard
    # only writes when the backend is non-local.
    from tpcore.ingestion import csv_archive_backends
    monkeypatch.setattr(
        csv_archive_backends, "LocalFSBackend",
        type("_NeverMatches", (), {}),
    )

    # Fake httpx download.
    fake_resp = MagicMock()
    fake_resp.raise_for_status = MagicMock()

    async def _aiter(*_a, **_k):
        for chunk in [b"sec-", b"download-", b"bytes"]:
            yield chunk

    fake_resp.aiter_bytes = _aiter
    fake_stream_cm = MagicMock()
    fake_stream_cm.__aenter__ = AsyncMock(return_value=fake_resp)
    fake_stream_cm.__aexit__ = AsyncMock(return_value=None)
    fake_client = MagicMock()
    fake_client.stream = MagicMock(return_value=fake_stream_cm)
    fake_client_cm = MagicMock()
    fake_client_cm.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client_cm.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "httpx.AsyncClient",
        lambda *a, **k: fake_client_cm,
    )

    result = await ensure_zip_cached(
        zip_path, user_agent="STE-test test@example.com",
    )
    assert result == zip_path
    assert result.read_bytes() == b"sec-download-bytes"

    # The s3 mirror call happened with the right source name + a
    # submissions_*.zip filename + the downloaded bytes.
    assert fake_backend.write.call_count == 1
    args, _ = fake_backend.write.call_args
    assert args[0] == "sec_submissions"
    assert args[1] == b"sec-download-bytes"
    assert args[2].startswith("submissions_")
    assert args[2].endswith(".zip")


@pytest.mark.asyncio
async def test_ensure_zip_cached_force_download_bypasses_local_and_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``force_download=True`` skips the local-fresh + archive-fresh
    checks and always pulls from SEC."""
    zip_path = tmp_path / "sec_submissions.zip"
    zip_path.write_bytes(b"old-local")

    fake_backend = MagicMock()
    fake_backend.list_archives = MagicMock(
        return_value=["submissions_20260601T1200Z.zip"],
    )
    fake_backend.read = MagicMock(return_value=b"old-archive")
    fake_backend.write = MagicMock(return_value="s3://bucket/...")
    monkeypatch.setattr(
        "tpcore.ingestion.csv_archive_backends.select_backend",
        lambda: fake_backend,
    )
    from tpcore.ingestion import csv_archive_backends
    monkeypatch.setattr(
        csv_archive_backends, "LocalFSBackend",
        type("_NeverMatches", (), {}),
    )

    fake_resp = MagicMock()
    fake_resp.raise_for_status = MagicMock()

    async def _aiter(*_a, **_k):
        yield b"fresh-from-sec"

    fake_resp.aiter_bytes = _aiter
    fake_stream_cm = MagicMock()
    fake_stream_cm.__aenter__ = AsyncMock(return_value=fake_resp)
    fake_stream_cm.__aexit__ = AsyncMock(return_value=None)
    fake_client = MagicMock()
    fake_client.stream = MagicMock(return_value=fake_stream_cm)
    fake_client_cm = MagicMock()
    fake_client_cm.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client_cm.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr("httpx.AsyncClient", lambda *a, **k: fake_client_cm)

    result = await ensure_zip_cached(
        zip_path, user_agent="STE-test test@example.com",
        force_download=True,
    )
    assert result.read_bytes() == b"fresh-from-sec"
    # Archive read NEVER attempted because force_download is True.
    fake_backend.list_archives.assert_not_called()


# ─── backfill_sec_metadata bulk-mode sentinels (in scripts/ops.py) ───


def test_backfill_sec_metadata_bulk_mode_makes_no_per_cik_http_calls() -> None:
    """Sentinel: scripts/ops.py loop must guard per-CIK HTTP behind the
    `bulk_reader is None` branch. If this string-match breaks (e.g.
    someone removes the bulk branch), CI reds."""
    source = Path(__file__).resolve().parents[1] / "scripts" / "ops.py"
    text = source.read_text(encoding="utf-8")
    # The bulk branch must exist.
    assert "if bulk_reader is not None:" in text
    assert "bulk_reader.get_merged_submissions(cik)" in text
    # And the per-CIK HTTP call must be inside an `else:` branch.
    assert "subs = await sec.get_submissions(" in text


def test_use_bulk_zip_false_preserves_existing_behavior() -> None:
    """Sentinel: ``use_bulk_zip`` defaults to False so existing
    incremental callers don't silently start using the bulk pipeline.
    Looking at the cfg.get default in the stage source."""
    source = Path(__file__).resolve().parents[1] / "scripts" / "ops.py"
    text = source.read_text(encoding="utf-8")
    needle = 'use_bulk_zip = _to_bool(cfg.get("use_bulk_zip", False))'
    assert needle in text, (
        f"backfill_sec_metadata must read use_bulk_zip from cfg with "
        f"default False; couldn't find {needle!r} in scripts/ops.py"
    )
