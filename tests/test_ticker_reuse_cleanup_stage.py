"""Stage-level tests for `_stage_cleanup_ticker_reuse_fundamentals`
(PR #440 impl, 2026-06-02).

Pins the 6 hard invariants from plan §5.2 + the manifest schema +
the bulk-first source sentinel. Hermetic: mock pool, in-memory
synthetic data, no DB, no SEC HTTP.
"""
from __future__ import annotations

import os
import re
import uuid
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio  # noqa: F401  (auto-marker via conftest)

os.environ.setdefault("SEC_EDGAR_USER_AGENT", "STE-test test@example.com")


# ────────────────────────────────────────────────────────────────
# Source sentinels (string-match on scripts/ops.py)
# ────────────────────────────────────────────────────────────────


def _ops_text() -> str:
    return (
        Path(__file__).resolve().parents[1] / "scripts" / "ops.py"
    ).read_text(encoding="utf-8")


def test_stage_registered_in_stage_specs() -> None:
    """The stage is registered so `--stage cleanup_ticker_reuse_fundamentals`
    is dispatchable from the CLI. Without registration, the operator
    runbook breaks."""
    from scripts import ops
    names = {n for n, _, _ in ops._STAGE_SPECS}  # noqa: SLF001
    assert "cleanup_ticker_reuse_fundamentals" in names


def test_dry_run_default_is_true() -> None:
    """Operator hard rule: backfills/cleanups dry-by-default. The CLI's
    `--param dry_run=true` is the safe baseline; opt-in `false` is the
    destructive surface. Regressing this default is a backfill anti-
    pattern."""
    text = _ops_text()
    assert 'dry_run = _to_bool(cfg.get("dry_run", True))' in text


def test_delete_after_archive_default_is_false() -> None:
    """The 6 hard invariants (plan §5.2) require DELETE to be
    explicitly opted-in. Default false guards against an operator
    typing `dry_run=false` without considering whether they want the
    destructive step."""
    text = _ops_text()
    assert (
        'delete_after_archive = _to_bool(cfg.get("delete_after_archive", False))'
        in text
    )


def test_use_bulk_zip_default_is_true_in_stage() -> None:
    """Per the operator's standing bulk-before-API-crawl rule.
    Per-CIK HTTP for a 783-ticker cohort would be the killed anti-
    pattern."""
    text = _ops_text()
    assert (
        'use_bulk_zip = _to_bool(cfg.get("use_bulk_zip", True))'
        in text
    )


def test_no_mass_delete_in_stage_source() -> None:
    """The hard invariant: never a blanket DELETE. The stage's DELETEs
    must be bounded by `id = $1` (per-row by original_id), never
    `WHERE period_end_date < ...` or unbounded DELETE FROM
    fundamentals_quarterly."""
    text = _ops_text()
    # Find the stage body region.
    start = text.find("async def _stage_cleanup_ticker_reuse_fundamentals")
    end = text.find("async def _stage_backfill_sec_lifecycle", start)
    assert start > 0 and end > start, (
        "could not locate the cleanup stage body in scripts/ops.py"
    )
    body = text[start:end]
    # No blanket DELETE — must be parameterized by id only.
    # The helper functions use `WHERE id = $1` patterns; check that
    # the body does not introduce any other DELETE shape.
    forbidden_patterns = (
        r"DELETE\s+FROM\s+platform\.fundamentals_quarterly\s+WHERE\s+period_end_date",
        r"DELETE\s+FROM\s+platform\.fundamentals_quarterly\s*;",
        r"DELETE\s+FROM\s+platform\.fundamentals_quarterly\s+WHERE\s+ticker\s*=\s*\$",
    )
    for p in forbidden_patterns:
        assert re.search(p, body, re.IGNORECASE) is None, (
            f"stage body must not contain a non-id-bounded DELETE matching {p}"
        )


def test_archive_before_delete_is_one_cte_statement() -> None:
    """The archive-then-delete CTE encodes the atomicity invariant —
    if the INSERT into the archive table fails for any reason, the
    DELETE doesn't fire. The shape is `WITH inserted AS (INSERT…
    RETURNING) DELETE WHERE id = (SELECT original_id FROM inserted)`."""
    text = _ops_text()
    assert "WITH inserted AS" in text, (
        "archive_row helper must use a CTE pattern so the INSERT and "
        "DELETE are wired through a single statement"
    )
    assert (
        "WHERE id = (SELECT original_id FROM inserted)"
        in text
    ), (
        "DELETE must be bounded by the CTE's RETURNING value (the "
        "archived row's id)"
    )


def test_quarantine_disposition_validated_against_enum() -> None:
    """The quarantine helper rejects dispositions outside the
    migration's CHECK constraint enum at Python level. Belt-and-
    braces against a coding mistake that would otherwise fail with
    a misleading Postgres constraint-violation error."""
    text = _ops_text()
    assert "_QUARANTINE_DISPOSITIONS" in text, (
        "quarantine disposition enum must be exposed at module level"
    )
    # The 4 enum values must appear in the frozenset definition.
    for v in (
        "ambiguous_predecessor_unknown",
        "corp_history_substrate_sparse",
        "cik_null",
        "operator_review_pending",
    ):
        assert f'"{v}"' in text, (
            f"_QUARANTINE_DISPOSITIONS must include {v!r}"
        )


def test_weak_evidence_never_deleted_in_stage_flow() -> None:
    """The disposition tree in the stage body must route
    weak_evidence_keep to a no-mutation branch. We sentinel by
    checking the stage body never calls `_archive_row` from the
    weak_evidence_keep branch."""
    text = _ops_text()
    start = text.find("async def _stage_cleanup_ticker_reuse_fundamentals")
    end = text.find("async def _stage_backfill_sec_lifecycle", start)
    body = text[start:end]
    # The weak-evidence else branch must NOT call _archive_row.
    # Match the else branch loosely: `else:  # weak_evidence_keep`
    weak_branch_idx = body.find('else:  # weak_evidence_keep')
    assert weak_branch_idx > 0, (
        "stage body must explicitly handle weak_evidence_keep as the "
        "else branch"
    )
    # No archive call in that branch's next ~5 lines.
    weak_branch_block = body[weak_branch_idx:weak_branch_idx + 400]
    assert "_archive_row" not in weak_branch_block, (
        "weak_evidence_keep branch must not call _archive_row"
    )
    assert "_quarantine_row" not in weak_branch_block, (
        "weak_evidence_keep branch must not call _quarantine_row"
    )


def test_fpfd_drift_skip_path_present() -> None:
    """When the bulk-extracted FPFD disagrees with the stored FPFD,
    the row is skipped + counted as `fpfd_drift_detected_skipped`.
    No row touches on a drifted-FPFD ticker — the cleanup arc
    requires FPFD to be trustworthy first."""
    text = _ops_text()
    assert "fpfd_drift_detected_skipped" in text, (
        "stage must surface fpfd_drift_detected_skipped disposition"
    )
    assert "extracted_fpfd != stored_fpfd" in text, (
        "stage must compare bulk-extracted FPFD against stored FPFD "
        "and skip on drift"
    )


# ────────────────────────────────────────────────────────────────
# Manifest schema sentinel
# ────────────────────────────────────────────────────────────────


def test_manifest_csv_schema_pinned() -> None:
    """The manifest CSV column set is the operator-facing contract.
    A future column rename would silently break the live-stage's
    re-validation read; this sentinel pins the order."""
    from scripts.ops import _ticker_reuse_manifest_columns  # noqa: PLC0415
    cols = _ticker_reuse_manifest_columns()
    assert cols == (
        "ticker",
        "period_end_date",
        "original_id",
        "current_cik",
        "current_fpfd",
        "proposed_disposition",
        "evidence_rank_used",
        "evidence_summary",
    )


def test_fq_mirror_column_list_matches_main_table() -> None:
    """The 20-column mirror list is the contract for the archive
    INSERT … SELECT shape. Drift here means archive rows missing
    columns that the main table actually has."""
    from scripts.ops import _FQ_MIRROR_COLUMNS  # noqa: PLC0415
    expected = (
        "ticker", "filing_date", "period_end_date", "period_label",
        "net_income", "fcf", "operating_cash_flow", "capex", "revenue",
        "total_assets", "total_liabilities", "current_assets",
        "current_liabilities", "receivables", "cash_and_equivalents",
        "shares_outstanding", "recorded_at", "pb", "de",
        "classification_id",
    )
    assert _FQ_MIRROR_COLUMNS == expected


# ────────────────────────────────────────────────────────────────
# Behaviour: hermetic dry-run + live shape
# ────────────────────────────────────────────────────────────────


def _mock_pool(
    candidate_rows: list[dict],
    issuer_rows: list[dict] | None = None,
    classify_responses: list[tuple[str, int, str]] | None = None,
    archive_responses: list[int] | None = None,
    quarantine_responses: list[int] | None = None,
) -> MagicMock:
    """Build a mock asyncpg pool with fetch / fetchval / fetchrow
    pre-wired to return the synthetic responses."""
    conn = MagicMock()

    # `fetch` returns candidate_rows on the cohort query, then
    # issuer_rows on the issuer-resolution query. Beyond that, we
    # don't track ordering — the classifier mocks handle those.
    fetch_responses = [candidate_rows, issuer_rows or []]
    fetch_call_idx = [0]

    async def _fetch(*_args, **_kw):
        idx = fetch_call_idx[0]
        if idx < len(fetch_responses):
            fetch_call_idx[0] += 1
            return fetch_responses[idx]
        return []

    conn.fetch = AsyncMock(side_effect=_fetch)

    # fetchrow used by the classifier — return None to fall through
    # to ambiguous unless the test overrides via classify_responses.
    conn.fetchrow = AsyncMock(return_value=None)

    # fetchval is used by the archive_row + quarantine_row helpers.
    av_responses = list(archive_responses or []) + list(quarantine_responses or [])
    av_idx = [0]

    async def _fetchval(*_args, **_kw):
        if av_idx[0] < len(av_responses):
            v = av_responses[av_idx[0]]
            av_idx[0] += 1
            return v
        return 0

    conn.fetchval = AsyncMock(side_effect=_fetchval)
    conn.execute = AsyncMock(return_value=None)

    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=None)
    txn.__aexit__ = AsyncMock(return_value=None)
    conn.transaction = MagicMock(return_value=txn)

    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire)
    return pool


@pytest.mark.asyncio
async def test_dry_run_writes_manifest_zero_db_mutations(tmp_path) -> None:
    """`dry_run=true` produces a manifest CSV. Mutation helpers
    (_archive_row, _quarantine_row) are never called: invariant via
    `fetchval` call count == 0."""
    from scripts.ops import _stage_cleanup_ticker_reuse_fundamentals

    pool = _mock_pool(
        candidate_rows=[],  # empty cohort — manifest is empty
        issuer_rows=[],
    )
    manifest_path = tmp_path / "manifest.csv"
    out = await _stage_cleanup_ticker_reuse_fundamentals(
        pool,
        {
            "dry_run": True,
            "use_bulk_zip": False,
            "manifest_path": str(manifest_path),
        },
    )
    assert out["dry_run"] is True
    # No mutations.
    acquired_conn = await pool.acquire.return_value.__aenter__()
    assert acquired_conn.fetchval.await_count == 0
    # Manifest was written (header present even when empty).
    assert manifest_path.exists()
    content = manifest_path.read_text(encoding="utf-8")
    assert "ticker,period_end_date,original_id" in content


@pytest.mark.asyncio
async def test_live_default_delete_after_archive_false_skips_archive(tmp_path) -> None:
    """Even with `dry_run=false`, the default `delete_after_archive=False`
    skips the archive INSERT entirely for high_confidence_ticker_reuse
    rows. Operator must explicitly opt into the destructive step.

    Fixture: rank-3 classifier hits (different issuer_id at
    period_end_date) → disposition = high_confidence_ticker_reuse →
    default `delete_after_archive=False` short-circuits before
    `_archive_row` is called → fetchval never invoked."""
    from scripts.ops import _stage_cleanup_ticker_reuse_fundamentals

    candidate = {
        "id": 101,
        "ticker": "TESTX",
        "period_end_date": date(2020, 6, 30),
        "current_cik": "0000123456",
        "current_fpfd": date(2024, 6, 30),
    }
    pool = _mock_pool(
        candidate_rows=[candidate],
        issuer_rows=[{"ticker": "TESTX", "issuer_id": "I_CURRENT"}],
    )
    # Mock fetchrow side_effect to drive the classifier:
    #   call 1: issuer_history → None (rank 2 miss)
    #   call 2: ticker_history → returns classification_id
    #   call 3: issuer_securities → returns issuer_id != I_CURRENT
    acquired_conn = await pool.acquire.return_value.__aenter__()
    acquired_conn.fetchrow = AsyncMock(side_effect=[
        None,
        {"classification_id": "C1", "valid_from": date(2018, 1, 1),
         "valid_to": date(2023, 12, 31)},
        {"issuer_id": "I_OLD"},  # different from I_CURRENT → rank 3 hit
    ])
    out = await _stage_cleanup_ticker_reuse_fundamentals(
        pool,
        {
            "dry_run": False,
            "use_bulk_zip": False,  # bypass bulk reader
            "delete_after_archive": False,  # default; explicit here
            "manifest_path": str(tmp_path / "manifest.csv"),
        },
    )
    assert out["dry_run"] is False
    # No archive INSERTs — _archive_row uses fetchval; never invoked.
    assert acquired_conn.fetchval.await_count == 0
    assert out["high_confidence_archive_count"] == 0


@pytest.mark.asyncio
async def test_quarantine_disposition_rejects_unknown_value() -> None:
    """The _quarantine_row helper rejects dispositions outside the
    migration's CHECK constraint enum."""
    from scripts.ops import _quarantine_row

    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=1)
    with pytest.raises(ValueError, match="not in allowed set"):
        await _quarantine_row(
            conn,
            original_id=1,
            disposition="invalid_disposition",
            decided_by_run_id=str(uuid.uuid4()),
            evidence_summary="test",
        )
