"""P0-003 — ``backfill_sec_metadata`` stage tests.

Tests:
  * TEST-007 backfill_idempotent — dry_run preview + structure
  * TEST-009 coverage_report_emitted — coverage_before / coverage_after
    are present in the output payload
  * registration sentinel — the stage appears in _STAGE_SPECS so the
    CLI can dispatch it.
"""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("SEC_EDGAR_USER_AGENT", "STE-test test@example.com")


# Stage registration is the bare-minimum integration assertion — if
# this regresses, ``scripts/ops.py --stage backfill_sec_metadata`` 404s.
def test_stage_registered_in_stage_specs() -> None:
    from scripts import ops
    names = {n for n, _, _ in ops._STAGE_SPECS}
    assert "backfill_sec_metadata" in names


def _mock_pool(snapshot: dict, scope_rows: list[dict] | None = None) -> MagicMock:
    """Mock asyncpg pool whose ``acquire().fetchrow(...)`` returns
    the coverage snapshot and ``acquire().fetch(...)`` returns the
    scope rows when asked."""
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=snapshot)
    conn.fetch = AsyncMock(return_value=scope_rows or [])
    conn.execute = AsyncMock(return_value=None)
    conn.executemany = AsyncMock(return_value=None)
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
async def test_009_coverage_report_emitted_when_scope_empty() -> None:
    """An empty-scope dry-run still emits coverage_before +
    coverage_after — these are the operator-facing audit signal."""
    from scripts.ops import _stage_backfill_sec_metadata
    snapshot = {
        "total": 13840, "has_cik": 10028,
        "has_sec_document_type_primary": 0,
        "has_first_public_filing_date": 0,
        "has_last_filing_date": 0,
        "has_fiscal_year_end_month": 0,
        "has_metadata_source": 0,
        "has_cik_source": 0,
    }
    pool = _mock_pool(snapshot, scope_rows=[])
    out = await _stage_backfill_sec_metadata(
        pool, {"dry_run": True, "tickers": "NONEXISTENT_TICKER"},
    )
    assert "coverage_before" in out
    assert "coverage_after" in out
    assert out["coverage_before"]["total"] == 13840
    assert out["coverage_after"]["total"] == 13840
    assert out["dry_run"] is True
    assert out["scope_size"] == 0


@pytest.mark.asyncio
async def test_007_backfill_idempotent_dry_run_makes_no_writes() -> None:
    """dry_run=True must NEVER call conn.execute / executemany on the
    table — the idempotency invariant. Re-running the same stage with
    the same scope produces the same output."""
    from scripts.ops import _stage_backfill_sec_metadata
    snapshot = {
        "total": 100, "has_cik": 80,
        "has_sec_document_type_primary": 50,
        "has_first_public_filing_date": 50,
        "has_last_filing_date": 50,
        "has_fiscal_year_end_month": 50,
        "has_metadata_source": 50,
        "has_cik_source": 30,
    }
    pool = _mock_pool(snapshot, scope_rows=[])
    out1 = await _stage_backfill_sec_metadata(
        pool, {"dry_run": True, "tickers": ""},
    )
    out2 = await _stage_backfill_sec_metadata(
        pool, {"dry_run": True, "tickers": ""},
    )
    # No table-write call paths should have been invoked.
    # (fetchrow + fetch are read-only; executemany is the write.)
    conn = pool.acquire.return_value.__aenter__.return_value
    assert conn.executemany.await_count == 0
    assert conn.execute.await_count == 0
    assert out1["dry_run"] is True
    assert out2["dry_run"] is True
    # Same shape — keys must be identical even on empty scope.
    assert set(out1.keys()) == set(out2.keys())


@pytest.mark.asyncio
async def test_010_dry_run_default_is_true() -> None:
    """Operator hard rule: backfill stages default to dry_run=True
    unless the caller explicitly passes False."""
    from scripts.ops import _stage_backfill_sec_metadata
    snapshot = {
        "total": 1, "has_cik": 0,
        "has_sec_document_type_primary": 0,
        "has_first_public_filing_date": 0,
        "has_last_filing_date": 0,
        "has_fiscal_year_end_month": 0,
        "has_metadata_source": 0,
        "has_cik_source": 0,
    }
    pool = _mock_pool(snapshot, scope_rows=[])
    # Empty cfg → defaults apply.
    out = await _stage_backfill_sec_metadata(pool, {})
    assert out["dry_run"] is True


@pytest.mark.asyncio
async def test_011_explicit_tickers_scope_resolves() -> None:
    """When --param tickers=A,B,C and rows exist for them, the scope
    size matches the rows-pulled-by-fetch count."""
    from scripts.ops import _stage_backfill_sec_metadata
    snapshot = {
        "total": 3, "has_cik": 3,
        "has_sec_document_type_primary": 3,
        "has_first_public_filing_date": 3,
        "has_last_filing_date": 3,
        "has_fiscal_year_end_month": 3,
        "has_metadata_source": 3,
        "has_cik_source": 0,
    }
    scope_rows = [
        {"ticker": "AAPL", "cik": "0000320193", "country": "US",
         "sec_document_type_primary": "10-Q",
         "first_public_filing_date": None, "last_filing_date": None,
         "fiscal_year_end_month": 9, "metadata_source": "sec_submissions"},
        {"ticker": "AZO", "cik": "0000866787", "country": "US",
         "sec_document_type_primary": "10-Q",
         "first_public_filing_date": None, "last_filing_date": None,
         "fiscal_year_end_month": 8, "metadata_source": "sec_submissions"},
    ]
    pool = _mock_pool(snapshot, scope_rows=scope_rows)
    out = await _stage_backfill_sec_metadata(
        pool, {"dry_run": True, "tickers": "AAPL,AZO",
               "do_cik": False, "do_metadata": False},
    )
    assert out["scope_size"] == 2
    # do_cik=False AND do_metadata=False → no candidates touched.
    assert out["cik"]["candidates"] == 0
    assert out["metadata"]["candidates"] == 0
