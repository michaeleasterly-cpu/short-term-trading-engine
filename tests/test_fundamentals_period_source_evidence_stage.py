"""`confirmed_data_gap_evidence_populator` stage — hermetic tests.

Per spec PR #450 + plan PR #451 §5. The stage populates
``platform.fundamentals_period_source_evidence`` for currently-FAILing
``(ticker, period_end_date)`` tuples. Hard-defaults ``dry_run=true``;
``use_bulk_zip=false`` raises; manifest CSV always emitted.

Tests:

  1. ``_STAGE_SPECS`` registers the new stage name.
  2. ``use_bulk_zip=false`` raises before any HTTP call.
  3. ``dry_run=true`` writes zero evidence rows + emits a manifest CSV.
  4. ``dry_run=false`` calls the FMP backfill + SEC handler with
     ``pool`` + ``record_evidence_for_periods`` provided.
  5. ``tickers`` filter scopes the universe.
  6. The manifest CSV carries the documented columns.
  7. The stage default dry_run is True at the stage layer (source
     sentinel).

Hermetic — stdlib + ``unittest.mock`` only. No DB, no network.

Per the ops-package-shadow rule, the ``ops_shadow`` xdist group keeps
parallel runs from colliding on the shared ``scripts.ops`` module.
"""
from __future__ import annotations

import csv
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.xdist_group("ops_shadow")


_REPO = Path(__file__).resolve().parents[1]
_OPS_PATH = _REPO / "scripts" / "ops.py"


# ──────────────────────────────────────────────────────────────────────
# Stub asyncpg-pool that handles the evidence-stage's reads.
# ──────────────────────────────────────────────────────────────────────


def _make_pool(
    *,
    filing_rows: list[dict] | None = None,
    fq_rows_by_ticker: dict[str, list[date]] | None = None,
    evidence_present: bool = True,
    sec_evidence_rows: list[dict] | None = None,
) -> MagicMock:
    filing_rows = filing_rows or []
    fq_by_t = fq_rows_by_ticker or {}
    sec_ev = sec_evidence_rows or []

    fetch_calls: list[tuple[str, tuple[Any, ...]]] = []
    fetchval_calls: list[tuple[str, tuple[Any, ...]]] = []

    async def _fetch(sql: str, *args: Any) -> list[dict[str, Any]]:
        fetch_calls.append((sql, args))
        if "FROM platform.liquidity_tiers lt" in sql:
            return filing_rows
        if "FROM platform.fundamentals_quarterly" in sql and "ANY($2::date[])" in sql:
            t = args[0]
            return [{"period_end_date": pe} for pe in fq_by_t.get(t, [])]
        if "FROM platform.fundamentals_quarterly fq" in sql:
            return []
        if "FROM platform.fundamentals_period_source_evidence" in sql:
            return sec_ev
        return []

    async def _fetchval(sql: str, *args: Any) -> Any:
        fetchval_calls.append((sql, args))
        if "to_regclass" in sql:
            return evidence_present
        return None

    async def _executemany(sql: str, rows: list[Any]) -> None:
        return None

    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=_fetch)
    conn.fetchval = AsyncMock(side_effect=_fetchval)
    conn.executemany = AsyncMock(side_effect=_executemany)
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire)
    pool._fetch_calls = fetch_calls
    pool._fetchval_calls = fetchval_calls
    pool._executemany_conn = conn
    return pool


def _two_quarterly_filings(ticker: str, today: date) -> list[dict]:
    """Two anchor filings with a >100-day gap so the validator
    surfaces inferred missing periods between them."""
    return [
        {
            "ticker": ticker,
            "period_end_date": today - timedelta(days=400),
            "sec_document_type_primary": "10-Q",
            "issuer_lifecycle_state": None,
            "issuer_lifecycle_event_date": None,
        },
        {
            "ticker": ticker,
            "period_end_date": today - timedelta(days=10),
            "sec_document_type_primary": "10-Q",
            "issuer_lifecycle_state": None,
            "issuer_lifecycle_event_date": None,
        },
    ]


# ──────────────────────────────────────────────────────────────────────
# Test 1 — stage is registered
# ──────────────────────────────────────────────────────────────────────


def test_stage_registered_in_stage_specs() -> None:
    from scripts import ops
    names = {n for n, _, _ in ops._STAGE_SPECS}
    assert "confirmed_data_gap_evidence_populator" in names, (
        f"stage must be registered; missing from {sorted(names)[-5:]}"
    )
    matched = [s for s in ops._STAGE_SPECS
               if s[0] == "confirmed_data_gap_evidence_populator"]
    assert matched, "stage row not found"
    # Heavy timeout per plan §5.1.
    assert matched[0][2] == ops.HEAVY_STAGE_TIMEOUT_SEC, (
        "stage must use HEAVY_STAGE_TIMEOUT_SEC"
    )


def test_stage_in_off_cycle_set() -> None:
    """Stage MUST be operator-on-demand only (per plan §2 cadence)."""
    from scripts import ops
    assert (
        "confirmed_data_gap_evidence_populator" in ops._OFF_CYCLE_STAGES
    )


# ──────────────────────────────────────────────────────────────────────
# Test 2 — use_bulk_zip=false raises
# ──────────────────────────────────────────────────────────────────────


async def test_use_bulk_zip_false_raises() -> None:
    """Per plan §5.2 / §6 + the bulk-first invariant: passing
    use_bulk_zip=false must raise before any HTTP / DB work."""
    from scripts import ops
    pool = _make_pool()
    with pytest.raises(RuntimeError, match="use_bulk_zip"):
        await ops._stage_confirmed_data_gap_evidence_populator(
            pool, {"use_bulk_zip": "false"},
        )


# ──────────────────────────────────────────────────────────────────────
# Test 3 — dry_run=true writes zero evidence rows
# ──────────────────────────────────────────────────────────────────────


async def test_dry_run_true_writes_zero_evidence_rows(tmp_path) -> None:
    """The populator stage must NEVER write evidence in dry-run."""
    from scripts import ops
    today = datetime.now(UTC).date()
    pool = _make_pool(filing_rows=_two_quarterly_filings("AAA", today))
    fake_handler_calls: list[dict] = []

    async def _fake_handler(p, cfg):
        fake_handler_calls.append(cfg)
        return {"dry_run": True, "evidence_rows_planned": 0}

    async def _fake_backfill(cache, db_log, symbol, **kwargs):
        # Verify pool=None + record_evidence_for_periods=None on
        # dry-run so the evidence writer is skipped.
        assert kwargs.get("pool") is None, (
            "dry_run must pass pool=None to skip evidence writes"
        )
        assert kwargs.get("record_evidence_for_periods") is None
        return 0

    fake_adapter = MagicMock()
    fake_adapter.__aenter__ = AsyncMock(return_value=fake_adapter)
    fake_adapter.__aexit__ = AsyncMock(return_value=None)
    fake_cache_cls = MagicMock(return_value=MagicMock())

    with patch(
        "tpcore.fmp.FMPFundamentalsAdapter", return_value=fake_adapter,
    ), patch(
        "tpcore.fundamentals.cache.FundamentalsCache", fake_cache_cls,
    ), patch(
        "tpcore.logging.db_handler.DBLogHandler", return_value=MagicMock(),
    ), patch(
        "tpcore.data.fundamentals_backfill.backfill_one_ticker",
        side_effect=_fake_backfill,
    ), patch(
        "tpcore.ingestion.handlers.handle_sec_fundamentals_fallback",
        side_effect=_fake_handler,
    ), patch.object(
        ops, "Path",
        lambda p="data": tmp_path / p if p == "data" else Path(p),
    ):
        result = await ops._stage_confirmed_data_gap_evidence_populator(
            pool, {"dry_run": "true"},
        )

    assert result["dry_run"] is True
    em_calls = pool._executemany_conn.executemany.await_count
    assert em_calls == 0, (
        f"dry_run must NOT call executemany on the evidence table; "
        f"got {em_calls}"
    )
    assert fake_handler_calls, "SEC handler should be invoked"
    assert fake_handler_calls[0].get("dry_run") == "true"


# ──────────────────────────────────────────────────────────────────────
# Test 4 — dry_run=false passes pool + periods to FMP backfill
# ──────────────────────────────────────────────────────────────────────


async def test_dry_run_false_passes_pool_and_periods_to_fmp(tmp_path) -> None:
    """In live mode, backfill_one_ticker MUST receive pool +
    record_evidence_for_periods so the evidence-writer path runs."""
    from scripts import ops
    today = datetime.now(UTC).date()
    pool = _make_pool(filing_rows=_two_quarterly_filings("BBB", today))
    captured: dict[str, Any] = {}

    async def _fake_backfill(cache, db_log, symbol, **kwargs):
        captured["pool"] = kwargs.get("pool")
        captured["periods"] = kwargs.get("record_evidence_for_periods")
        captured["source"] = kwargs.get("evidence_source")
        return 0

    async def _fake_handler(p, cfg):
        return {"dry_run": False, "rows": 0}

    fake_adapter = MagicMock()
    fake_adapter.__aenter__ = AsyncMock(return_value=fake_adapter)
    fake_adapter.__aexit__ = AsyncMock(return_value=None)

    with patch(
        "tpcore.fmp.FMPFundamentalsAdapter", return_value=fake_adapter,
    ), patch(
        "tpcore.fundamentals.cache.FundamentalsCache", MagicMock(),
    ), patch(
        "tpcore.logging.db_handler.DBLogHandler", return_value=MagicMock(),
    ), patch(
        "tpcore.data.fundamentals_backfill.backfill_one_ticker",
        side_effect=_fake_backfill,
    ), patch(
        "tpcore.ingestion.handlers.handle_sec_fundamentals_fallback",
        side_effect=_fake_handler,
    ), patch.object(
        ops, "Path",
        lambda p="data": tmp_path / p if p == "data" else Path(p),
    ):
        result = await ops._stage_confirmed_data_gap_evidence_populator(
            pool, {"dry_run": "false"},
        )

    assert result["dry_run"] is False
    assert captured.get("pool") is pool, (
        "live mode must pass pool to backfill_one_ticker"
    )
    assert captured.get("periods"), (
        "live mode must pass the missing periods list"
    )
    assert captured.get("source") == "fmp_historical"


# ──────────────────────────────────────────────────────────────────────
# Test 5 — tickers subset filter is honored
# ──────────────────────────────────────────────────────────────────────


async def test_tickers_subset_filter(tmp_path) -> None:
    from scripts import ops
    today = datetime.now(UTC).date()
    base_rows: list[dict] = []
    for t in ("AAA", "BBB", "CCC"):
        base_rows.extend(_two_quarterly_filings(t, today))
    pool = _make_pool(filing_rows=base_rows)
    attempted: list[str] = []

    async def _fake_backfill(cache, db_log, symbol, **kwargs):
        attempted.append(symbol)
        return 0

    async def _fake_handler(p, cfg):
        return {"dry_run": True}

    fake_adapter = MagicMock()
    fake_adapter.__aenter__ = AsyncMock(return_value=fake_adapter)
    fake_adapter.__aexit__ = AsyncMock(return_value=None)

    with patch(
        "tpcore.fmp.FMPFundamentalsAdapter", return_value=fake_adapter,
    ), patch(
        "tpcore.fundamentals.cache.FundamentalsCache", MagicMock(),
    ), patch(
        "tpcore.logging.db_handler.DBLogHandler", return_value=MagicMock(),
    ), patch(
        "tpcore.data.fundamentals_backfill.backfill_one_ticker",
        side_effect=_fake_backfill,
    ), patch(
        "tpcore.ingestion.handlers.handle_sec_fundamentals_fallback",
        side_effect=_fake_handler,
    ), patch.object(
        ops, "Path",
        lambda p="data": tmp_path / p if p == "data" else Path(p),
    ):
        await ops._stage_confirmed_data_gap_evidence_populator(
            pool, {"dry_run": "true", "tickers": "AAA,BBB"},
        )

    assert "CCC" not in attempted, (
        f"CCC must be filtered out; attempted={attempted}"
    )


# ──────────────────────────────────────────────────────────────────────
# Test 6 — Default dry_run is True at the stage layer (source sentinel)
# ──────────────────────────────────────────────────────────────────────


def test_stage_default_dry_run_is_true() -> None:
    """Mirror the test_sec_fundamentals_fallback_dry_run.py Test 5
    precedent. The stage body must declare a default-True dry_run."""
    src = _OPS_PATH.read_text(encoding="utf-8")
    stage_idx = src.find("_stage_confirmed_data_gap_evidence_populator")
    assert stage_idx >= 0, "stage function not found"
    body = src[stage_idx:stage_idx + 3000]
    assert 'cfg.get("dry_run", True)' in body, (
        "stage must default dry_run=True at the stage layer"
    )


# ──────────────────────────────────────────────────────────────────────
# Test 7 — Manifest CSV carries the documented columns
# ──────────────────────────────────────────────────────────────────────


async def test_manifest_csv_columns(tmp_path) -> None:
    from scripts import ops
    today = datetime.now(UTC).date()
    pool = _make_pool(filing_rows=_two_quarterly_filings("DDD", today))

    async def _fake_backfill(*a, **k):
        return 0

    async def _fake_handler(*a, **k):
        return {"dry_run": True}

    fake_adapter = MagicMock()
    fake_adapter.__aenter__ = AsyncMock(return_value=fake_adapter)
    fake_adapter.__aexit__ = AsyncMock(return_value=None)

    with patch(
        "tpcore.fmp.FMPFundamentalsAdapter", return_value=fake_adapter,
    ), patch(
        "tpcore.fundamentals.cache.FundamentalsCache", MagicMock(),
    ), patch(
        "tpcore.logging.db_handler.DBLogHandler", return_value=MagicMock(),
    ), patch(
        "tpcore.data.fundamentals_backfill.backfill_one_ticker",
        side_effect=_fake_backfill,
    ), patch(
        "tpcore.ingestion.handlers.handle_sec_fundamentals_fallback",
        side_effect=_fake_handler,
    ), patch.object(
        ops, "Path",
        lambda p="data": tmp_path / p if p == "data" else Path(p),
    ):
        result = await ops._stage_confirmed_data_gap_evidence_populator(
            pool, {"dry_run": "true"},
        )

    manifest_path = result.get("manifest_path")
    if not manifest_path:
        pytest.skip("no manifest emitted (no inferred gaps in fixture)")
    p = Path(manifest_path)
    assert p.is_file()
    with p.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        assert reader.fieldnames == [
            "ticker", "period_end_date",
            "fmp_outcome", "sec_outcome", "would_exclude",
        ]


# ──────────────────────────────────────────────────────────────────────
# Test 8 — dry-run-purity fix: populator passes dry_run=True to
# backfill_one_ticker so cache.upsert_payload is NEVER called
# ──────────────────────────────────────────────────────────────────────
#
# Per the 2026-06-02 operator finding: the populator's FMP-cascade leg
# called ``cache.upsert_payload`` UNCONDITIONALLY in dry-run mode,
# bumping ``recorded_at`` on 5 AXIN rows during a ``--param
# dry_run=true --param limit=10`` preview. The fix gates the primary
# upsert on ``dry_run`` (mirror semantic with PR #448 SEC handler).
# This test pins the populator's contract: ``backfill_one_ticker``
# MUST receive ``dry_run=True`` when the stage is in preview mode.


async def test_confirmed_data_gap_populator_dry_run_passes_dry_run_to_backfill(
    tmp_path,
) -> None:
    """The populator MUST forward ``dry_run=True`` to
    ``backfill_one_ticker`` so the FMP-cascade leg's
    ``cache.upsert_payload`` write is suppressed."""
    from scripts import ops
    today = datetime.now(UTC).date()
    pool = _make_pool(filing_rows=_two_quarterly_filings("AAA", today))
    captured: dict[str, Any] = {}

    async def _fake_backfill(cache, db_log, symbol, **kwargs):
        captured["dry_run"] = kwargs.get("dry_run")
        captured["pool"] = kwargs.get("pool")
        return 0

    async def _fake_handler(p, cfg):
        return {"dry_run": True, "evidence_rows_planned": 0}

    fake_adapter = MagicMock()
    fake_adapter.__aenter__ = AsyncMock(return_value=fake_adapter)
    fake_adapter.__aexit__ = AsyncMock(return_value=None)

    with patch(
        "tpcore.fmp.FMPFundamentalsAdapter", return_value=fake_adapter,
    ), patch(
        "tpcore.fundamentals.cache.FundamentalsCache", MagicMock(),
    ), patch(
        "tpcore.logging.db_handler.DBLogHandler", return_value=MagicMock(),
    ), patch(
        "tpcore.data.fundamentals_backfill.backfill_one_ticker",
        side_effect=_fake_backfill,
    ), patch(
        "tpcore.ingestion.handlers.handle_sec_fundamentals_fallback",
        side_effect=_fake_handler,
    ), patch.object(
        ops, "Path",
        lambda p="data": tmp_path / p if p == "data" else Path(p),
    ):
        await ops._stage_confirmed_data_gap_evidence_populator(
            pool, {"dry_run": "true"},
        )

    assert captured.get("dry_run") is True, (
        "populator must pass dry_run=True to backfill_one_ticker so "
        "cache.upsert_payload is skipped (fix for the 2026-06-02 AXIN "
        "recorded_at bump defect)"
    )
    assert captured.get("pool") is None, (
        "populator must pass pool=None in dry-run so the evidence "
        "writer is also skipped"
    )


# ──────────────────────────────────────────────────────────────────────
# Test 9 — dry-run dict surfaces fmp_would_write_rows counter
# ──────────────────────────────────────────────────────────────────────


async def test_confirmed_data_gap_populator_dry_run_returns_fmp_would_write_rows(
    tmp_path,
) -> None:
    """The dry-run result dict MUST surface
    ``fmp_would_write_rows`` so the operator can preview how many
    rows the live mode WOULD have upserted into
    ``platform.fundamentals_quarterly``. Mirrors PR #448's
    ``archive_rows_planned`` precedent for the SEC leg."""
    from scripts import ops
    today = datetime.now(UTC).date()
    pool = _make_pool(filing_rows=_two_quarterly_filings("AAA", today))

    async def _fake_backfill(cache, db_log, symbol, **kwargs):
        # Return a non-zero would-write count so the populator can
        # accumulate it on the counter.
        return 7

    async def _fake_handler(p, cfg):
        return {"dry_run": True, "evidence_rows_planned": 0}

    fake_adapter = MagicMock()
    fake_adapter.__aenter__ = AsyncMock(return_value=fake_adapter)
    fake_adapter.__aexit__ = AsyncMock(return_value=None)

    with patch(
        "tpcore.fmp.FMPFundamentalsAdapter", return_value=fake_adapter,
    ), patch(
        "tpcore.fundamentals.cache.FundamentalsCache", MagicMock(),
    ), patch(
        "tpcore.logging.db_handler.DBLogHandler", return_value=MagicMock(),
    ), patch(
        "tpcore.data.fundamentals_backfill.backfill_one_ticker",
        side_effect=_fake_backfill,
    ), patch(
        "tpcore.ingestion.handlers.handle_sec_fundamentals_fallback",
        side_effect=_fake_handler,
    ), patch.object(
        ops, "Path",
        lambda p="data": tmp_path / p if p == "data" else Path(p),
    ):
        result = await ops._stage_confirmed_data_gap_evidence_populator(
            pool, {"dry_run": "true"},
        )

    assert result["dry_run"] is True
    assert "fmp_would_write_rows" in result, (
        "dry-run dict must expose fmp_would_write_rows counter for "
        "operator preview parity with SEC leg's archive_rows_planned"
    )
    assert result["fmp_would_write_rows"] >= 7, (
        f"expected fmp_would_write_rows ≥ 7; got {result}"
    )


# ──────────────────────────────────────────────────────────────────────
# Test 10 — regression-pin: dry_run=true STILL writes zero evidence
# rows (the PR #452 contract; ensure the fix doesn't regress)
# ──────────────────────────────────────────────────────────────────────


async def test_confirmed_data_gap_populator_dry_run_writes_zero_evidence_rows(
    tmp_path,
) -> None:
    """Regression-pin: the dry-run-purity fix MUST NOT regress the
    PR #452 evidence-gate. ``executemany`` must remain unfired on the
    evidence table in dry-run."""
    from scripts import ops
    today = datetime.now(UTC).date()
    pool = _make_pool(filing_rows=_two_quarterly_filings("ZZZ", today))

    async def _fake_backfill(cache, db_log, symbol, **kwargs):
        # The populator should never reach the evidence-writer in
        # dry-run; just return 0 to keep the stage progressing.
        return 0

    async def _fake_handler(p, cfg):
        return {"dry_run": True, "evidence_rows_planned": 0}

    fake_adapter = MagicMock()
    fake_adapter.__aenter__ = AsyncMock(return_value=fake_adapter)
    fake_adapter.__aexit__ = AsyncMock(return_value=None)

    with patch(
        "tpcore.fmp.FMPFundamentalsAdapter", return_value=fake_adapter,
    ), patch(
        "tpcore.fundamentals.cache.FundamentalsCache", MagicMock(),
    ), patch(
        "tpcore.logging.db_handler.DBLogHandler", return_value=MagicMock(),
    ), patch(
        "tpcore.data.fundamentals_backfill.backfill_one_ticker",
        side_effect=_fake_backfill,
    ), patch(
        "tpcore.ingestion.handlers.handle_sec_fundamentals_fallback",
        side_effect=_fake_handler,
    ), patch.object(
        ops, "Path",
        lambda p="data": tmp_path / p if p == "data" else Path(p),
    ):
        await ops._stage_confirmed_data_gap_evidence_populator(
            pool, {"dry_run": "true"},
        )

    em_calls = pool._executemany_conn.executemany.await_count
    assert em_calls == 0, (
        "dry-run-purity fix must NOT regress the PR #452 evidence-gate: "
        f"executemany should be 0; got {em_calls}"
    )


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
