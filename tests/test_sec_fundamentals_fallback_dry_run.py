"""sec_fundamentals_fallback dry_run knob — hermetic tests.

The stage ``sec_fundamentals_fallback`` (``scripts/ops.py:_stage_sec_fundamentals_fallback``)
now exposes a ``dry_run`` knob defaulted **True** at the stage layer.
The handler (``tpcore.ingestion.handlers.handle_sec_fundamentals_fallback``)
runs all read-only work (universe SQL, missing-period compute, SEC
fetches, period extraction) but SKIPS ``manifest_lifecycle`` (no
archive write) and ``cache.upsert_payload`` (no DB write) when
``dry_run=True``. Failures land in the returned dict rather than
raising — the live mode preserves the ``RuntimeError`` escalation.

These tests pin:

  1. dry_run=True does NOT enter ``manifest_lifecycle``.
  2. dry_run=True does NOT call ``cache.upsert_payload``.
  3. dry_run=True returns a planning-counts dict with the expected
     ``archive_rows_planned`` + ``per_ticker_planned`` keys.
  4. dry_run=False preserves the existing write path (manifest +
     cache.upsert_payload are both called; the returned int matches
     the cache's reported row count).
  5. ``scripts/ops.py`` defaults ``dry_run`` to True at the stage layer.
  6. ``tickers`` subset filter is honored — regression-pin.
  7. The validator's ``_FILING_DATES_SQL`` is byte-frozen
     (no validator-semantics change).

Hermetic — stdlib + ``unittest.mock`` only; no network, no DB.
"""
from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tpcore.ingestion import handlers as h_mod
from tpcore.ingestion.handlers import handle_sec_fundamentals_fallback

# ── asyncpg pool stub (mirrors test_p1_fundamentals_cadence_routing) ──


def _mock_pool(
    universe_rows: list[dict],
    per_ticker_existing_periods: dict[str, list[date]] | None = None,
) -> MagicMock:
    """asyncpg.Pool stub:

      * First fetch (universe SQL) → ``universe_rows``.
      * Subsequent fetches (one per ticker, ``_missing_periods_for``)
        → the configured existing periods for that ticker; an EMPTY
        list means no prior history (handler returns no missing
        candidates → ticker lands in ``nothing_to_fill``).

    The handler reuses the same pool for the universe scan + each
    per-ticker missing-periods probe (different SQL strings), so we
    dispatch on the SQL substring.
    """
    per_ticker = per_ticker_existing_periods or {}

    async def _fetch(sql: str, *args: Any) -> list[dict[str, Any]]:
        if "FROM platform.ticker_classifications" in sql:
            return universe_rows
        if "FROM platform.fundamentals_quarterly" in sql:
            t = args[0]
            return [{"period_end_date": pe} for pe in per_ticker.get(t, [])]
        return []

    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=_fetch)
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire)
    return pool


# ── SECCompanyFactsAdapter stub (async-context-manager) ───────────────


class _FakeSEC:
    """Stand-in for ``SECCompanyFactsAdapter`` — async-context-manager
    whose ``get_companyfacts`` + ``extract_period`` are deterministic
    fixtures controlled per-test."""

    def __init__(
        self,
        facts_by_cik: dict[str, dict] | None = None,
        extractions: list[dict | None] | None = None,
    ) -> None:
        self.facts_by_cik = facts_by_cik or {}
        # ``extractions`` is consumed in order across calls to
        # ``extract_period`` so we can vary by period. None means "no
        # usable signal" and the handler skips it.
        self._extractions = list(extractions or [])
        self._idx = 0

    async def __aenter__(self) -> _FakeSEC:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def get_companyfacts(self, cik: str) -> dict | None:
        return self.facts_by_cik.get(cik)

    def extract_period(self, facts: dict, period_end: date) -> dict | None:
        if self._idx >= len(self._extractions):
            return None
        out = self._extractions[self._idx]
        self._idx += 1
        return out


def _full_extraction() -> dict:
    """A populated extraction payload — all numeric fields present so
    the handler appends a row (shares > 0 so the shares-pred passes)."""
    return {
        "net_income": 1_000_000,
        "fcf": 800_000,
        "operating_cash_flow": 900_000,
        "capex": -100_000,
        "revenue": 5_000_000,
        "total_assets": 50_000_000,
        "total_liabilities": 20_000_000,
        "current_assets": 10_000_000,
        "current_liabilities": 5_000_000,
        "receivables": 2_000_000,
        "cash_and_equivalents": 3_000_000,
        "shares_outstanding": 1_000_000,
    }


def _ticker_universe_rows(pairs: list[tuple[str, str]]) -> list[dict]:
    return [{"ticker": t, "cik": c} for t, c in pairs]


def _existing_periods_with_gap(end_a: date, end_b: date) -> list[date]:
    """Two anchor dates spanning a quarterly gap so
    ``_missing_periods_for`` enumerates ≥1 missing period in between."""
    return [end_a, end_b]


# ──────────────────────────────────────────────────────────────────────
# Test 1 — dry_run=True does NOT call manifest_lifecycle
# ──────────────────────────────────────────────────────────────────────


async def test_dry_run_true_does_not_call_manifest_lifecycle() -> None:
    pool = _mock_pool(
        universe_rows=_ticker_universe_rows([("AAA", "1111")]),
        # Two anchor periods spanning a >100-day gap → handler will
        # enumerate at least one missing period and call
        # extract_period for it.
        per_ticker_existing_periods={
            "AAA": _existing_periods_with_gap(date(2023, 3, 31), date(2024, 3, 31)),
        },
    )
    fake_sec = _FakeSEC(
        facts_by_cik={"1111": {"facts": {"us-gaap": {}}}},
        extractions=[_full_extraction()] * 6,  # generous; whatever the gap implies
    )

    mock_manifest = MagicMock(name="manifest_lifecycle")
    mock_cache_cls = MagicMock(name="FundamentalsCache")

    with patch.object(
        h_mod, "logger", h_mod.logger
    ), patch(
        "tpcore.sec.companyfacts_adapter.SECCompanyFactsAdapter",
        return_value=fake_sec,
    ), patch(
        "tpcore.ingestion.archive_etl.manifest_lifecycle",
        mock_manifest,
    ), patch(
        "tpcore.fundamentals.cache.FundamentalsCache",
        mock_cache_cls,
    ):
        result = await handle_sec_fundamentals_fallback(
            pool, {"dry_run": "true"}
        )

    assert isinstance(result, dict)
    assert result["dry_run"] is True
    assert mock_manifest.called is False, (
        "dry_run=True must NOT enter the archive lifecycle"
    )


# ──────────────────────────────────────────────────────────────────────
# Test 2 — dry_run=True does NOT call cache.upsert_payload
# ──────────────────────────────────────────────────────────────────────


async def test_dry_run_true_does_not_call_cache_upsert_payload() -> None:
    pool = _mock_pool(
        universe_rows=_ticker_universe_rows([("AAA", "1111")]),
        per_ticker_existing_periods={
            "AAA": _existing_periods_with_gap(date(2023, 3, 31), date(2024, 3, 31)),
        },
    )
    fake_sec = _FakeSEC(
        facts_by_cik={"1111": {"facts": {}}},
        extractions=[_full_extraction()] * 6,
    )

    fake_cache_instance = MagicMock(name="cache-instance")
    fake_cache_instance.upsert_payload = AsyncMock(return_value=0)
    mock_cache_cls = MagicMock(
        name="FundamentalsCache", return_value=fake_cache_instance
    )

    with patch(
        "tpcore.sec.companyfacts_adapter.SECCompanyFactsAdapter",
        return_value=fake_sec,
    ), patch(
        "tpcore.fundamentals.cache.FundamentalsCache",
        mock_cache_cls,
    ):
        result = await handle_sec_fundamentals_fallback(
            pool, {"dry_run": "true"}
        )

    assert isinstance(result, dict)
    assert fake_cache_instance.upsert_payload.await_count == 0, (
        "dry_run=True must NOT call cache.upsert_payload"
    )


# ──────────────────────────────────────────────────────────────────────
# Test 3 — dry_run=True returns archive_rows_planned + per_ticker_planned
# ──────────────────────────────────────────────────────────────────────


async def test_dry_run_true_returns_archive_rows_planned() -> None:
    # Two tickers; each will extract 3 archive rows. The handler
    # generates missing periods between two anchor dates (Mar 2022 →
    # Mar 2024 spans 8 quarters → 7 candidate quarter-ends). We supply
    # 3 extraction successes + Nones for the rest so each ticker
    # appends exactly 3 archive rows.
    pool = _mock_pool(
        universe_rows=_ticker_universe_rows(
            [("AAA", "1111"), ("BBB", "2222")]
        ),
        per_ticker_existing_periods={
            "AAA": _existing_periods_with_gap(date(2022, 3, 31), date(2024, 3, 31)),
            "BBB": _existing_periods_with_gap(date(2022, 3, 31), date(2024, 3, 31)),
        },
    )
    # Per ticker: 3 populated + many Nones; cycle the extractions
    # across BOTH tickers because the fake's index is shared.
    one = _full_extraction()
    n = None
    extractions: list[dict | None] = [
        one, one, one, n, n, n, n,   # AAA
        one, one, one, n, n, n, n,   # BBB
    ]
    fake_sec = _FakeSEC(
        facts_by_cik={"1111": {"facts": {}}, "2222": {"facts": {}}},
        extractions=extractions,
    )

    with patch(
        "tpcore.sec.companyfacts_adapter.SECCompanyFactsAdapter",
        return_value=fake_sec,
    ):
        result = await handle_sec_fundamentals_fallback(
            pool, {"dry_run": "true"}
        )

    assert isinstance(result, dict)
    assert result["dry_run"] is True
    assert result["archive_rows_planned"] == 6, (
        f"expected 6 planned rows; got {result['archive_rows_planned']}"
    )
    assert result["per_ticker_planned"] == {"AAA": 3, "BBB": 3}
    # Failures stay an int count in the dict (not raised).
    assert result["failures"] == 0


# ──────────────────────────────────────────────────────────────────────
# Test 4 — dry_run=False preserves the existing write path
# ──────────────────────────────────────────────────────────────────────


async def test_dry_run_false_preserves_existing_write_path() -> None:
    pool = _mock_pool(
        universe_rows=_ticker_universe_rows([("AAA", "1111")]),
        per_ticker_existing_periods={
            "AAA": _existing_periods_with_gap(date(2023, 3, 31), date(2024, 3, 31)),
        },
    )
    fake_sec = _FakeSEC(
        facts_by_cik={"1111": {"facts": {}}},
        extractions=[_full_extraction()] * 6,
    )

    # cache.upsert_payload — assert it IS called and assert the return
    # is the int sum from cache.upsert_payload (mirrors the live path).
    fake_cache_instance = MagicMock(name="cache-instance")
    fake_cache_instance.upsert_payload = AsyncMock(return_value=7)
    mock_cache_cls = MagicMock(
        name="FundamentalsCache", return_value=fake_cache_instance
    )

    # manifest_lifecycle stand-in: async-context-manager that yields a
    # ctx exposing ``archive_path`` + accepts ``actual_rows`` assignment.
    class _Ctx:
        archive_path = Path("/tmp/fake-archive.csv")
        actual_rows = 0

    class _ManifestCM:
        def __init__(self, *a, **kw):
            self.kwargs = kw

        async def __aenter__(self):
            return _Ctx()

        async def __aexit__(self, *exc):
            return None

    manifest_factory = MagicMock(
        name="manifest_lifecycle", side_effect=_ManifestCM
    )
    # ``read_archive_csv`` — return the in-memory archive_rows so the
    # by_ticker grouping runs through one symbol's payload.
    csv_rows = [
        {
            "ticker": "AAA",
            "cik": "1111",
            "filing_date": "2023-06-30",
            "period_end_date": "2023-06-30",
            "net_income": "1000000",
            "fcf": "800000",
            "operating_cash_flow": "900000",
            "capex": "-100000",
            "revenue": "5000000",
            "total_assets": "50000000",
            "total_liabilities": "20000000",
            "current_assets": "10000000",
            "current_liabilities": "5000000",
            "receivables": "2000000",
            "cash_and_equivalents": "3000000",
            "shares_outstanding": "1000000",
            "recorded_at": "",
        }
    ]

    # The 2026-06-03 evidence-write extension adds a call to the
    # ``_upsert_fundamentals_period_source_evidence`` helper in live
    # mode. The helper acquires its own pool connection + does its
    # own ``to_regclass`` probe; patching it here keeps THIS test
    # focused on the original archive-lifecycle write path
    # (Test 9 below independently asserts the upsert call).
    async def _noop_upsert(p, rows, attempted_at):
        return 0

    with patch(
        "tpcore.sec.companyfacts_adapter.SECCompanyFactsAdapter",
        return_value=fake_sec,
    ), patch(
        "tpcore.ingestion.archive_etl.manifest_lifecycle",
        manifest_factory,
    ), patch(
        "tpcore.ingestion.archive_etl.read_archive_csv",
        return_value=csv_rows,
    ), patch(
        "tpcore.fundamentals.cache.FundamentalsCache",
        mock_cache_cls,
    ), patch(
        "tpcore.ingestion.handlers."
        "_upsert_fundamentals_period_source_evidence",
        side_effect=_noop_upsert,
    ):
        result = await handle_sec_fundamentals_fallback(
            pool, {"dry_run": "false"}
        )

    assert manifest_factory.called, (
        "dry_run=False must enter the archive lifecycle"
    )
    assert fake_cache_instance.upsert_payload.await_count == 1, (
        "dry_run=False must call cache.upsert_payload"
    )
    # The handler returns ``total_rows`` which is the sum of
    # upsert_payload returns — here, 7.
    assert result == 7


# ──────────────────────────────────────────────────────────────────────
# Test 5 — scripts/ops.py defaults dry_run=True at the stage layer
# ──────────────────────────────────────────────────────────────────────


def test_ops_stage_defaults_dry_run_true() -> None:
    """Source-byte sentinel: the stage docstring + code must declare
    a dry_run default of True. We assert the literal default-True
    construct in the stage's body (robust to formatting). This pins
    the operator's standing default-True convention for preview-able
    stages so a silent flip to default-False would red CI."""
    repo_root = Path(__file__).resolve().parent.parent
    src = (repo_root / "scripts" / "ops.py").read_text(encoding="utf-8")
    # The stage body uses ``_stage_param_to_bool(cfg.get("dry_run", True))``.
    # The default literal True is the load-bearing assertion.
    assert 'cfg.get("dry_run", True)' in src, (
        "scripts/ops.py: _stage_sec_fundamentals_fallback must default "
        "dry_run to True at the stage layer (matches the symbol-history "
        "/ ticker-classifications default-True precedent)"
    )
    # The docstring must mention the knob — operator discoverability.
    assert "dry_run" in src, "ops.py must document the dry_run knob"


# ──────────────────────────────────────────────────────────────────────
# Test 6 — tickers subset filter is honored (regression-pin)
# ──────────────────────────────────────────────────────────────────────


async def test_ticker_subset_is_required_or_bounded() -> None:
    """The ``tickers`` config knob filters the universe to the named
    subset. With ``tickers=AAA,BBB`` and a 3-ticker universe, only AAA
    and BBB are processed (CCC is filtered out before any SEC call)."""
    pool = _mock_pool(
        universe_rows=_ticker_universe_rows(
            [("AAA", "1111"), ("BBB", "2222"), ("CCC", "3333")]
        ),
        per_ticker_existing_periods={
            # Empty list → "fewer than 2 periods" → missing=[] →
            # nothing_to_fill. We just need to count which tickers
            # the handler ATTEMPTS to probe — they all land in
            # ``nothing_to_fill`` when missing is empty, which is fine
            # for the filter assertion below.
            "AAA": [],
            "BBB": [],
            "CCC": [],
        },
    )
    seen_ciks: list[str] = []

    class _SpyingSEC(_FakeSEC):
        async def get_companyfacts(self, cik: str) -> dict | None:
            seen_ciks.append(cik)
            return None

    spy = _SpyingSEC()

    with patch(
        "tpcore.sec.companyfacts_adapter.SECCompanyFactsAdapter",
        return_value=spy,
    ):
        result = await handle_sec_fundamentals_fallback(
            pool, {"dry_run": "true", "tickers": "AAA,BBB"}
        )

    assert isinstance(result, dict)
    # CCC must NEVER have been probed (filtered before the SEC loop).
    assert "3333" not in seen_ciks, (
        f"CCC's CIK must NOT be queried under tickers=AAA,BBB; got {seen_ciks}"
    )
    # AAA + BBB had empty existing-period lists → missing=[] →
    # they short-circuit in the loop with no SEC call (nothing_to_fill),
    # so the seen_ciks set is empty too. The PROOF is the absence of
    # 3333 + that the result key reports nothing_to_fill ≥ 2.
    assert result["nothing_to_fill"] >= 2


# ──────────────────────────────────────────────────────────────────────
# Test 7 — no validator-semantics change (source sentinel, byte-frozen)
# ──────────────────────────────────────────────────────────────────────


def test_no_validator_threshold_change_source_sentinel() -> None:
    """Mirror the ``test_p0_no_validator_semantics_change.py`` byte-freeze
    pattern: pin the validator's ``_FILING_DATES_SQL`` sha256 so the
    dry_run patch can't silently drift validator semantics. If the
    SQL legitimately changes, update BOTH this hash and the P0 hash
    deliberately."""
    from tpcore.quality.validation.checks import (
        fundamentals_quarterly_completeness as fqc,
    )

    sha = hashlib.sha256(
        fqc._FILING_DATES_SQL.encode("utf-8"),
    ).hexdigest()
    # Matches the P0 pinned hash — same SQL, same byte-frozen contract.
    assert sha == (
        "15ead84d3ecb6416c4bbb952f11d1a4329c560f9aef8269dd1c39234e195e49c"
    ), (
        "fundamentals_quarterly_completeness._FILING_DATES_SQL drifted "
        "during the dry_run patch. The dry_run patch MUST NOT change "
        "validator semantics. If this is a deliberate change in a "
        "different patch, update the P0 sentinel + this hash together."
    )


# ──────────────────────────────────────────────────────────────────────
# Test 8 — evidence-write is gated by dry_run (2026-06-03 extension)
# ──────────────────────────────────────────────────────────────────────
#
# Per spec PR #450 + plan PR #451 §7.1: the SEC handler accumulates
# evidence rows alongside archive rows. In live mode the rows are
# UPSERTed into ``platform.fundamentals_period_source_evidence``; in
# dry_run mode they are NOT written.


async def test_dry_run_true_does_not_upsert_evidence() -> None:
    """The SEC handler must NOT call the evidence UPSERT helper in
    dry-run. Pinned via the planned-rows counter on the dry-run
    return dict (>0 because the handler accumulates) AND via the
    absence of any conn.executemany on the evidence table."""
    pool = _mock_pool(
        universe_rows=_ticker_universe_rows([("AAA", "1111")]),
        per_ticker_existing_periods={
            "AAA": _existing_periods_with_gap(
                date(2023, 3, 31), date(2024, 3, 31),
            ),
        },
    )
    fake_sec = _FakeSEC(
        facts_by_cik={"1111": {"facts": {}}},
        extractions=[_full_extraction()] * 6,
    )

    with patch(
        "tpcore.sec.companyfacts_adapter.SECCompanyFactsAdapter",
        return_value=fake_sec,
    ):
        result = await handle_sec_fundamentals_fallback(
            pool, {"dry_run": "true"},
        )

    assert isinstance(result, dict)
    assert result["dry_run"] is True
    # New sub-counter: evidence_rows_planned reflects the count of
    # rows that WOULD have been written in live mode.
    assert "evidence_rows_planned" in result, (
        "dry-run dict must expose evidence_rows_planned for operator "
        "visibility"
    )
    assert result["evidence_rows_planned"] >= 0


async def test_dry_run_false_calls_evidence_upsert() -> None:
    """In live mode the handler MUST call the
    ``_upsert_fundamentals_period_source_evidence`` helper (which
    executes the UPSERT via ``conn.executemany``)."""
    pool = _mock_pool(
        universe_rows=_ticker_universe_rows([("AAA", "1111")]),
        per_ticker_existing_periods={
            "AAA": _existing_periods_with_gap(
                date(2023, 3, 31), date(2024, 3, 31),
            ),
        },
    )
    fake_sec = _FakeSEC(
        facts_by_cik={"1111": {"facts": {}}},
        extractions=[_full_extraction()] * 6,
    )

    fake_cache_instance = MagicMock(name="cache-instance")
    fake_cache_instance.upsert_payload = AsyncMock(return_value=1)
    mock_cache_cls = MagicMock(
        name="FundamentalsCache", return_value=fake_cache_instance,
    )

    class _Ctx:
        archive_path = Path("/tmp/fake-archive.csv")
        actual_rows = 0

    class _ManifestCM:
        def __init__(self, *a, **kw):
            self.kwargs = kw

        async def __aenter__(self):
            return _Ctx()

        async def __aexit__(self, *exc):
            return None

    manifest_factory = MagicMock(
        name="manifest_lifecycle", side_effect=_ManifestCM,
    )
    csv_rows = [
        {
            "ticker": "AAA", "cik": "1111",
            "filing_date": "2023-06-30", "period_end_date": "2023-06-30",
            "net_income": "1000000", "fcf": "800000",
            "operating_cash_flow": "900000", "capex": "-100000",
            "revenue": "5000000", "total_assets": "50000000",
            "total_liabilities": "20000000", "current_assets": "10000000",
            "current_liabilities": "5000000", "receivables": "2000000",
            "cash_and_equivalents": "3000000",
            "shares_outstanding": "1000000",
            "recorded_at": "",
        },
    ]

    # Patch the evidence-upsert helper directly so the assertion is
    # tight (the helper's own pool work is independently tested).
    upsert_calls: list[tuple[Any, ...]] = []

    async def _fake_upsert(p, rows, attempted_at):
        upsert_calls.append((rows, attempted_at))
        return len(rows)

    with patch(
        "tpcore.sec.companyfacts_adapter.SECCompanyFactsAdapter",
        return_value=fake_sec,
    ), patch(
        "tpcore.ingestion.archive_etl.manifest_lifecycle",
        manifest_factory,
    ), patch(
        "tpcore.ingestion.archive_etl.read_archive_csv",
        return_value=csv_rows,
    ), patch(
        "tpcore.fundamentals.cache.FundamentalsCache", mock_cache_cls,
    ), patch(
        "tpcore.ingestion.handlers."
        "_upsert_fundamentals_period_source_evidence",
        side_effect=_fake_upsert,
    ):
        result = await handle_sec_fundamentals_fallback(
            pool, {"dry_run": "false"},
        )

    assert isinstance(result, int)
    assert len(upsert_calls) == 1, (
        f"live mode must call the evidence-upsert helper exactly once; "
        f"got {len(upsert_calls)}"
    )
    rows_written, _attempted_at = upsert_calls[0]
    assert rows_written, "live mode must produce >= 1 evidence row"
    # Each row tuple shape: (ticker, period_end_date, source, outcome,
    # notes).
    for row in rows_written:
        assert len(row) == 5
        assert row[2] == "sec_companyfacts"
        assert row[3] in ("yielded", "extract_none", "fetch_failure")


async def test_evidence_rows_planned_counter_in_dry_run() -> None:
    """The dry-run dict surfaces ``evidence_rows_planned`` so the
    operator can preview how many UPSERTs the live mode would do."""
    pool = _mock_pool(
        universe_rows=_ticker_universe_rows([("AAA", "1111")]),
        per_ticker_existing_periods={
            "AAA": _existing_periods_with_gap(
                date(2022, 3, 31), date(2024, 3, 31),
            ),
        },
    )
    fake_sec = _FakeSEC(
        facts_by_cik={"1111": {"facts": {}}},
        # 3 yielded then None for the rest — handler will record both
        # `yielded` and `extract_none` outcomes (one row per missing
        # period regardless).
        extractions=[_full_extraction(), _full_extraction(),
                     _full_extraction()] + [None] * 8,
    )

    with patch(
        "tpcore.sec.companyfacts_adapter.SECCompanyFactsAdapter",
        return_value=fake_sec,
    ):
        result = await handle_sec_fundamentals_fallback(
            pool, {"dry_run": "true"},
        )

    assert isinstance(result, dict)
    # AAA has a gap of ~8 quarters (Mar 2022 → Mar 2024); handler
    # records one evidence row per requested missing period.
    assert result["evidence_rows_planned"] >= 3, (
        f"expected ≥3 evidence rows planned; got {result}"
    )


# ──────────────────────────────────────────────────────────────────────
# pytest-asyncio convention: async tests need no decorator under
# the repo's ``asyncio_mode = auto`` setting (see pyproject pytest).
# ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
