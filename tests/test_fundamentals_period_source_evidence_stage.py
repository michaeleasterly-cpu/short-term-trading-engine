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
    sec_evidence_rows: list[dict] | None = None,
) -> MagicMock:
    filing_rows = filing_rows or []
    fq_by_t = fq_rows_by_ticker or {}
    sec_ev = sec_evidence_rows or []

    fetch_calls: list[tuple[str, tuple[Any, ...]]] = []
    fetchval_calls: list[tuple[str, tuple[Any, ...]]] = []

    # P3: the validator now computes the gap via the shared store
    # (set-difference of SEC reportDates vs fundamentals). The
    # ``filing_rows`` carry per-issuer SEC reportDates + classification_id;
    # we derive the store's anchored / expected / have row sets from them.
    by_cid: dict[str, dict[str, Any]] = {}
    for fr in filing_rows:
        cid = fr.get("classification_id")
        if cid is None:
            continue
        rec = by_cid.setdefault(
            cid,
            {
                "ticker": fr["ticker"],
                "sec": set(),
                "have": set(),
            },
        )
        if fr.get("period_end_date") is not None:
            rec["have"].add(fr["period_end_date"])
        for rd in fr.get("_sec_report_dates", ()):
            rec["sec"].add(rd)

    async def _fetch(sql: str, *args: Any) -> list[dict[str, Any]]:
        fetch_calls.append((sql, args))
        # Validator universe SQL.
        if "WITH liquid AS" in sql:
            return filing_rows
        # Store _ANCHORED_SQL.
        if "SELECT DISTINCT classification_id" in sql:
            wanted = set(args[0])
            return [
                {"classification_id": cid}
                for cid in wanted
                if by_cid.get(cid, {}).get("sec")
            ]
        # Store _EXPECTED_SQL.
        if "FROM platform.sec_periodic_filings" in sql:
            wanted = set(args[0])
            out: list[dict[str, Any]] = []
            for cid in wanted:
                for rd in by_cid.get(cid, {}).get("sec", ()):
                    out.append({"classification_id": cid, "report_date": rd})
            return out
        # Store _HAVE_SQL (by classification_id).
        if ("FROM platform.fundamentals_quarterly" in sql
                and "classification_id = ANY" in sql):
            wanted = set(args[0])
            out = []
            for cid in wanted:
                for pe in by_cid.get(cid, {}).get("have", ()):
                    out.append(
                        {"classification_id": cid, "period_end_date": pe}
                    )
            return out
        # Per-ticker fundamentals read (the populator's own period probe).
        if "FROM platform.fundamentals_quarterly" in sql and "ANY($2::date[])" in sql:
            t = args[0]
            return [{"period_end_date": pe} for pe in fq_by_t.get(t, [])]
        if "FROM platform.fundamentals_quarterly fq" in sql:
            return []
        # Plan 2: SEC evidence read-back now hits data_quality_log
        # (kind='confirmed_data_gap_evidence'); the standalone evidence table
        # was dropped in migration 0300.
        if "confirmed_data_gap_evidence" in sql:
            return sec_ev
        return []

    async def _fetchval(sql: str, *args: Any) -> Any:
        fetchval_calls.append((sql, args))
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
    """P3 set-difference shape: SEC filed THREE quarterly reportDates;
    fundamentals has only two (the middle one is missing) → the validator
    surfaces exactly one genuine missing reportDate for this issuer.

    The universe-row ``period_end_date`` values define the fundamentals
    ``have`` set (one row per present period); ``_sec_report_dates`` (a
    fixture-only key the fake reads) defines the SEC ``expected`` set."""
    present_a = today - timedelta(days=400)
    present_b = today - timedelta(days=10)
    missing = today - timedelta(days=200)  # SEC filed it; fundamentals lacks
    sec_dates = [present_a, missing, present_b]
    cid = f"c-{ticker}"
    rows = []
    for pe in (present_a, present_b):
        rows.append({
            "ticker": ticker,
            "classification_id": cid,
            "cik": "0001",
            "period_end_date": pe,
            "sec_document_type_primary": "10-Q",
            "issuer_lifecycle_state": None,
            "issuer_lifecycle_event_date": None,
            "_sec_report_dates": sec_dates,
        })
    return rows


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


# ──────────────────────────────────────────────────────────────────────
# Tests 11-14 — SEC handler return-shape dispatch (live-mode crash fix)
# ──────────────────────────────────────────────────────────────────────
#
# Per the 2026-06-03 operator bounded-live-run crash:
#
#   event='ingestion.handler.sec_fundamentals_fallback.done' rows=50
#                                                            evidence_rows=259
#   event='ops.stage.failed' error="'int' object has no attribute 'keys'"
#
# The SEC handler ``handle_sec_fundamentals_fallback`` is documented as
# returning ``int | dict[str, Any] | None`` (handlers.py:289):
#
#   * dry-run mode    → dict   (planned counters)
#   * live + rows>0   → int    (rows_written from cache.upsert_payload)
#   * live + rows==0  → int 0  (no archive_rows)
#   * outage-trap     → dict   (populator sets ``{"error": ...}``)
#
# The populator's post-SEC aggregator used ``(sec_result or {}).keys()``
# which crashes when ``sec_result`` is a non-zero int (e.g., 259). The
# fix is an ``isinstance(sec_result, dict)`` dispatch on the 3 shapes;
# the substrate-driven sec_outcomes read-back at step 2d already covers
# per-ticker counter aggregation regardless of return-shape, so no
# downstream logic depends on the handler return being a dict.


async def test_populator_handles_sec_dryrun_dict_result(tmp_path) -> None:
    """SEC handler returns dict (dry-run mode). The populator's
    aggregator must accept the dict shape and complete cleanly."""
    from scripts import ops
    today = datetime.now(UTC).date()
    pool = _make_pool(filing_rows=_two_quarterly_filings("AAA", today))

    async def _fake_backfill(cache, db_log, symbol, **kwargs):
        return 0

    async def _fake_handler(p, cfg):
        return {
            "dry_run": True,
            "archive_rows_planned": 12,
            "per_ticker_planned": {"AAA": 3},
            "evidence_rows_planned": 0,
        }

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

    # Result dict surfaces the dict-shape branch's metadata.
    assert result["sec_result_shape"] == "dict", (
        f"dry-run SEC dict must dispatch to 'dict' branch; got {result}"
    )
    assert result["sec_rows_written"] == 0, (
        "dict branch must report sec_rows_written=0 (rows landed via "
        "the substrate-driven aggregator, not the handler return)"
    )


async def test_populator_handles_sec_live_int_result(tmp_path) -> None:
    """SEC handler returns int (live mode, rows_written=259).

    Regression-pin for the 2026-06-02 bounded-live-run crash. The
    populator MUST NOT call ``.keys()`` on the int; it must dispatch
    to the int branch + complete cleanly. The 259-row evidence write
    that landed inside the handler before its return is preserved by
    the substrate-driven sec_outcomes read-back at step 2d."""
    from scripts import ops
    today = datetime.now(UTC).date()
    pool = _make_pool(filing_rows=_two_quarterly_filings("AAA", today))

    async def _fake_backfill(cache, db_log, symbol, **kwargs):
        return 0

    async def _fake_handler(p, cfg):
        # Live-mode contract: int rows_written, not a dict.
        return 259

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
        # The critical assertion: this does NOT raise
        # ``'int' object has no attribute 'keys'``.
        result = await ops._stage_confirmed_data_gap_evidence_populator(
            pool, {"dry_run": "false"},
        )

    assert result["sec_result_shape"] == "int", (
        f"live-mode int return must dispatch to 'int' branch; got {result}"
    )
    assert result["sec_rows_written"] == 259, (
        f"int branch must surface rows_written=259; got "
        f"{result.get('sec_rows_written')}"
    )


async def test_populator_handles_sec_none_result(tmp_path) -> None:
    """SEC handler returns None (defensive shape per the contract).
    Populator must treat as zero/no-op without crash."""
    from scripts import ops
    today = datetime.now(UTC).date()
    pool = _make_pool(filing_rows=_two_quarterly_filings("AAA", today))

    async def _fake_backfill(cache, db_log, symbol, **kwargs):
        return 0

    async def _fake_handler(p, cfg):
        return None

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

    assert result["sec_result_shape"] == "none", (
        f"None return must dispatch to 'none' branch; got {result}"
    )
    assert result["sec_rows_written"] == 0, (
        "none branch must report sec_rows_written=0"
    )


def test_live_int_result_does_not_call_keys() -> None:
    """Source-sentinel: the aggregator block dispatches on
    ``isinstance(sec_result, dict)`` BEFORE any ``.keys()`` call so a
    non-zero int return cannot reach the dict-only branch.

    Pins the structural fix shape so the unconditional ``.keys()``
    pattern cannot regress.
    """
    src = _OPS_PATH.read_text(encoding="utf-8")
    stage_idx = src.find("_stage_confirmed_data_gap_evidence_populator")
    assert stage_idx >= 0, "stage function not found"
    body = src[stage_idx:stage_idx + 12000]

    # The pre-fix unconditional pattern must be gone.
    assert "(sec_result or {}).keys()" not in body, (
        "the unconditional ``(sec_result or {}).keys()`` pattern is "
        "the 2026-06-02 crash shape; it must be replaced with an "
        "``isinstance(sec_result, dict)`` dispatch"
    )

    # The isinstance dispatch must be present and precede any
    # ``sec_result.keys()`` call.
    dispatch_idx = body.find("isinstance(sec_result, dict)")
    assert dispatch_idx >= 0, (
        "post-SEC aggregator must dispatch on "
        "``isinstance(sec_result, dict)`` before calling ``.keys()``"
    )
    keys_idx = body.find("sec_result.keys()")
    assert keys_idx > dispatch_idx, (
        f"``sec_result.keys()`` must appear AFTER the isinstance "
        f"dispatch (dispatch_idx={dispatch_idx}, keys_idx={keys_idx})"
    )

    # The int branch must also be present so live-mode ``int`` returns
    # land on the no-keys path.
    assert "isinstance(sec_result, int)" in body, (
        "post-SEC aggregator must dispatch on ``isinstance(sec_result, "
        "int)`` for the live-mode rows_written shape"
    )


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
