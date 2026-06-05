"""Stage-level tests for ``_stage_universe_build`` (Plan 3 Phase 1).

Hermetic: mocked pool, monkeypatched SEC/FMP source-fetch seams (NO
network), in-memory synthetic entries. No real DB, no real API calls.

Pins:
  * stage registered in _STAGE_SPECS + KNOWN_STAGES.
  * stage is in _OFF_CYCLE_STAGES (NOT the child-first --update order).
  * dry_run defaults to True (no INSERT without --param dry_run=false).
  * live path INSERTs via a chunked ON CONFLICT (id) DO NOTHING statement.
  * source-fetch uses tpcore.outage.with_retry (no local asyncio.sleep
    retry loop) — the data-adapter HTTP-retry contract.
"""
from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("SEC_EDGAR_USER_AGENT", "STE-test test@example.com")
os.environ.setdefault("FMP_API_KEY", "test-fmp-api-key")


def _ops_text() -> str:
    return (
        Path(__file__).resolve().parents[1] / "scripts" / "ops.py"
    ).read_text(encoding="utf-8")


def _stage_source() -> str:
    """The universe_build stage body + its private fetch/insert helpers."""
    text = _ops_text()
    start = text.find("# universe_build — survivorship-free")
    end = text.find("async def _stage_classify_tickers", start)
    assert start > 0 and end > start, "could not locate universe_build source"
    return text[start:end]


# ── registration + off-cycle ─────────────────────────────────────────


def test_stage_registered() -> None:
    from scripts import ops
    names = {n for n, _, _ in ops._STAGE_SPECS}  # noqa: SLF001
    assert "universe_build" in names
    assert "universe_build" in ops.KNOWN_STAGES


def test_stage_is_off_cycle_not_in_update_order() -> None:
    """Identity-first: universe_build must NOT run in the child-first
    daily --update cadence (discovery §1/§6)."""
    from scripts import ops
    assert "universe_build" in ops._OFF_CYCLE_STAGES  # noqa: SLF001


def test_stage_not_in_dashboard_ops_update_stages() -> None:
    """Off-cycle stages stay out of the dashboard daily-cadence list."""
    from dashboard_components.health import OPS_UPDATE_STAGES
    assert "universe_build" not in OPS_UPDATE_STAGES


# ── source sentinels (data-adapter HTTP-retry contract) ──────────────


def test_fetch_uses_with_retry_not_local_sleep_loop() -> None:
    src = _stage_source()
    assert "with_retry" in src, "FMP fetch must use tpcore.outage.with_retry"
    # No local retry loop: a while-True + asyncio.sleep is the banned
    # anti-pattern (data-adapter rule + STYLE_GUIDE error-handling).
    assert "while True" not in src
    assert "asyncio.sleep" not in src


def test_insert_uses_issuer_stable_conflict_targets() -> None:
    """Cross-run idempotency (review #2): the upsert conflict target must be
    issuer-stable, NOT ``(id)`` — a TKR-14 id embeds the discovery year, so a
    re-run in a later year mints a DIFFERENT id for the same issuer. SEC rows
    conflict on the ``(cik) WHERE cik IS NOT NULL`` partial unique index;
    FMP-only (cik NULL) rows fall back to ``(id)`` (the id is reused, not
    re-minted, so it is stable across runs)."""
    src = _stage_source()
    assert "ON CONFLICT (cik) WHERE cik IS NOT NULL DO NOTHING" in src
    # FMP-only backstop still present (id reused → stable).
    assert "ON CONFLICT (id) DO NOTHING" in src


def test_dry_run_default_true_source() -> None:
    src = _stage_source()
    assert 'cfg.get("dry_run", True)' in src


def test_no_alpaca_active_source() -> None:
    """The legacy minter's Alpaca-active source (survivorship-violating)
    must NOT appear in the new identity-first universe builder."""
    src = _stage_source()
    assert "/v2/assets" not in src
    assert "fetch_alpaca_assets" not in src


# ── behavioural: dry_run does not write ──────────────────────────────


def _make_pool() -> tuple[MagicMock, list[Any]]:
    """A mocked asyncpg pool capturing conn.execute calls."""
    executed: list[Any] = []

    async def _execute(sql: str, *args: Any) -> str:
        executed.append((sql, args))
        return "INSERT 0 0"

    async def _fetch(sql: str, *args: Any) -> list[Any]:
        return []  # no pre-existing identities → all fresh mints

    conn = MagicMock()
    conn.execute = AsyncMock(side_effect=_execute)
    conn.fetch = AsyncMock(side_effect=_fetch)
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire)
    pool.fetch = AsyncMock(side_effect=_fetch)
    return pool, executed


def _stub_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the network fetch seams with synthetic entries."""
    from scripts import ops
    from tpcore.identity.universe_build import (
        FMPUniverseEntry,
        SECUniverseEntry,
    )

    async def _fake_sec(*, log: Any) -> list[Any]:
        return [
            SECUniverseEntry(
                ticker="AAPL", cik="0000320193", legal_name="Apple Inc.",
                first_public_filing_date=date(1994, 12, 12),
            ),
        ]

    async def _fake_fmp(*, log: Any) -> list[Any]:
        return [
            FMPUniverseEntry(
                ticker="MICRO", company_name="Micro Co",
                earliest_date=date(2020, 1, 1),
            ),
            FMPUniverseEntry(
                ticker="DEAD", company_name="Dead Co",
                earliest_date=date(2005, 1, 1),
                delisted=True, delisting_date=date(2010, 6, 1),
            ),
        ]

    monkeypatch.setattr(ops, "_fetch_sec_universe_entries", _fake_sec)
    monkeypatch.setattr(ops, "_fetch_fmp_universe_entries", _fake_fmp)


@pytest.mark.asyncio
async def test_dry_run_does_not_insert(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts import ops
    _stub_sources(monkeypatch)
    pool, executed = _make_pool()
    # Tiny synthetic dataset → relax the degraded-source floors (review #3);
    # the floor behaviour itself is covered by the dedicated degraded tests.
    result = await ops._stage_universe_build(  # noqa: SLF001
        pool, {"dry_run": True, "min_sec": 1, "min_fmp": 1}
    )
    assert result["dry_run"] is True
    assert result["rows_minted"] == 0
    # 3 securities assembled (AAPL sec + MICRO + DEAD fmp-only).
    assert result["rows_previewed"] == 3
    assert result["n_sec"] == 1
    assert result["n_fmp_only"] == 2
    assert result["n_delisted"] == 1
    # NO INSERT executed in dry-run.
    assert executed == []


def _make_pool_with_existing(
    existing_by_cik: dict[str, str] | None = None,
    existing_fmp_by_ticker: dict[str, str] | None = None,
) -> tuple[MagicMock, list[Any]]:
    """A mocked pool whose ``fetch`` returns pre-existing identity rows.

    Models a cross-run re-mint: ``existing_by_cik`` maps cik→id for SEC rows
    already in ``ticker_classifications``; ``existing_fmp_by_ticker`` maps
    ticker→id for cik-NULL FMP-only rows. ``execute`` is captured as before.
    """
    existing_by_cik = existing_by_cik or {}
    existing_fmp_by_ticker = existing_fmp_by_ticker or {}
    executed: list[Any] = []

    async def _execute(sql: str, *args: Any) -> str:
        executed.append((sql, args))
        return "INSERT 0 0"

    async def _fetch(sql: str, *args: Any) -> list[Any]:
        # Two resolution queries: by cik (SEC), by ticker among cik-NULL (FMP).
        if "cik IS NOT NULL" in sql or "WHERE cik = ANY" in sql:
            return [
                {"cik": cik, "id": _id}
                for cik, _id in existing_by_cik.items()
            ]
        return [
            {"current_ticker": t, "id": _id}
            for t, _id in existing_fmp_by_ticker.items()
        ]

    conn = MagicMock()
    conn.execute = AsyncMock(side_effect=_execute)
    conn.fetch = AsyncMock(side_effect=_fetch)
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire)
    pool.fetch = AsyncMock(side_effect=_fetch)
    return pool, executed


@pytest.mark.asyncio
async def test_live_run_inserts_chunked(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts import ops
    _stub_sources(monkeypatch)
    pool, executed = _make_pool_with_existing()
    result = await ops._stage_universe_build(  # noqa: SLF001
        pool, {"dry_run": False, "chunk_size": 2, "min_sec": 1, "min_fmp": 1}
    )
    assert result["dry_run"] is False
    assert result["rows_minted"] == 3
    insert_calls = [c for c in executed if "INSERT INTO" in c[0]]
    # 1 SEC row (AAPL) → 1 chunk; 2 FMP-only rows (MICRO, DEAD) @ chunk=2 → 1
    # chunk. Two issuer-class statements, each chunked separately.
    assert len(insert_calls) == 2
    all_sql = "\n".join(c[0] for c in insert_calls)
    assert "INSERT INTO platform.ticker_classifications" in all_sql
    # SEC rows conflict on cik; FMP-only on id (reused, stable).
    assert "ON CONFLICT (cik) WHERE cik IS NOT NULL DO NOTHING" in all_sql
    assert "ON CONFLICT (id) DO NOTHING" in all_sql
    # lifetime_start array is the 9th positional arg ($9); every value is
    # a real date, never the forbidden sentinel.
    sentinel = date(1900, 1, 1)
    for _sql, args in insert_calls:
        lifetime_starts = args[8]
        for ls in lifetime_starts:
            assert ls != sentinel
            assert ls is not None


# ── cross-run idempotency (review #2) ────────────────────────────────


def _stub_big_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub fetches that clear the hard-stop floors (review #3)."""
    from scripts import ops
    from tpcore.identity.universe_build import (
        FMPUniverseEntry,
        SECUniverseEntry,
    )

    async def _fake_sec(*, log: Any) -> list[Any]:
        return [
            SECUniverseEntry(
                ticker=f"S{i:05d}", cik=f"{i:010d}",
                legal_name=f"Co {i}",
                first_public_filing_date=date(2000, 1, 1),
            )
            for i in range(1, 8_500)
        ]

    async def _fake_fmp(*, log: Any) -> list[Any]:
        return [
            FMPUniverseEntry(
                ticker=f"F{i:05d}", company_name=f"FMP Co {i}",
                earliest_date=date(2010, 1, 1),
            )
            for i in range(1, 6_000)
        ]

    monkeypatch.setattr(ops, "_fetch_sec_universe_entries", _fake_sec)
    monkeypatch.setattr(ops, "_fetch_fmp_universe_entries", _fake_fmp)


@pytest.mark.asyncio
async def test_rerun_reuses_ids_no_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A re-run with existing identity rows must REUSE the stored ids and
    mint NO new/duplicate rows for already-known issuers (review #2)."""
    from scripts import ops
    _stub_sources(monkeypatch)
    # Pre-existing identity: AAPL (SEC, by cik) + MICRO (FMP-only, by ticker)
    # already minted in a PRIOR year with stored ids.
    pool, executed = _make_pool_with_existing(
        existing_by_cik={"0000320193": "USSO94SAAPL07"},
        existing_fmp_by_ticker={"MICRO": "USSO20FMICR09"},
    )
    result = await ops._stage_universe_build(  # noqa: SLF001
        pool, {"dry_run": False, "chunk_size": 100_000, "min_sec": 1, "min_fmp": 1}
    )
    # Re-used ids appear verbatim in the INSERT id arrays (no re-mint).
    insert_calls = [c for c in executed if "INSERT INTO" in c[0]]
    all_ids: list[str] = []
    for _sql, args in insert_calls:
        all_ids.extend(args[0])
    assert "USSO94SAAPL07" in all_ids  # SEC id reused, not re-minted
    assert "USSO20FMICR09" in all_ids  # FMP-only id reused
    assert result["n_reused"] == 2


# ── producer hard-stop on degraded source (review #3) ────────────────


@pytest.mark.asyncio
async def test_degraded_sec_source_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty/short SEC fetch (degraded 200 / quota) must hard-stop the
    stage rather than silently mint a truncated universe (review #3)."""
    from scripts import ops
    _stub_sources(monkeypatch)  # only 1 SEC entry → below the ~8000 floor
    pool, _ = _make_pool_with_existing()
    with pytest.raises(RuntimeError, match="SEC universe.*degraded|floor"):
        await ops._stage_universe_build(pool, {"dry_run": True})  # noqa: SLF001


@pytest.mark.asyncio
async def test_degraded_fmp_source_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A healthy SEC fetch but a short FMP stock-list also hard-stops."""
    from scripts import ops
    from tpcore.identity.universe_build import (
        FMPUniverseEntry,
        SECUniverseEntry,
    )

    async def _fake_sec(*, log: Any) -> list[Any]:
        return [
            SECUniverseEntry(
                ticker=f"S{i:05d}", cik=f"{i:010d}", legal_name=f"Co {i}",
                first_public_filing_date=date(2000, 1, 1),
            )
            for i in range(1, 8_500)
        ]

    async def _fake_fmp(*, log: Any) -> list[Any]:
        return [
            FMPUniverseEntry(
                ticker="ONLYONE", company_name="One", earliest_date=date(2020, 1, 1),
            )
        ]

    monkeypatch.setattr(ops, "_fetch_sec_universe_entries", _fake_sec)
    monkeypatch.setattr(ops, "_fetch_fmp_universe_entries", _fake_fmp)
    pool, _ = _make_pool_with_existing()
    with pytest.raises(RuntimeError, match="FMP.*degraded|floor"):
        await ops._stage_universe_build(pool, {"dry_run": True})  # noqa: SLF001


@pytest.mark.asyncio
async def test_healthy_sources_clear_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Above the floors, the stage assembles + previews without raising."""
    from scripts import ops
    _stub_big_sources(monkeypatch)
    pool, _ = _make_pool_with_existing()
    result = await ops._stage_universe_build(pool, {"dry_run": True})  # noqa: SLF001
    assert result["dry_run"] is True
    assert result["rows_previewed"] >= 14_000


# ── FMP delisted pagination (review #1) ──────────────────────────────


@pytest.mark.asyncio
async def test_fmp_fetch_paginates_delisted_but_not_stock_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_fetch_fmp_universe_entries`` must paginate the delisted endpoint
    until an empty page (a single-page read silently caps delisted rows at
    ~100 and defeats survivorship-freeness — review #1) but must fetch the
    stock-list with a SINGLE GET. ``/stable/stock-list`` is NOT paginated:
    it returns the full roster in one response and IGNORES the ``page`` param
    (verified against the live API 2026-06-05), so paging it would loop on the
    same full list until the page ceiling raises."""
    import httpx

    from scripts import ops

    # delisted: 3 non-empty pages then empty (paginated).
    def _delisted_page(page: int) -> list[dict[str, Any]]:
        if page >= 3:
            return []
        return [
            {"symbol": f"DEL{page}_{i}", "companyName": f"DEL {i}",
             "delistedDate": "2010-06-01"}
            for i in range(100)
        ]

    # stock-list: full roster returned EVERY call regardless of page (the
    # endpoint ignores ``page``) — faithful to the live API behaviour.
    _STOCK_FULL = [
        {"symbol": f"STK{i}", "companyName": f"STK {i}"} for i in range(250)
    ]

    captured_pages: dict[str, list[int | None]] = {"stock": [], "delisted": []}

    class _Resp:
        def __init__(self, data: list[dict[str, Any]]) -> None:
            self._data = data

        def raise_for_status(self) -> None:
            return None

        def json(self) -> list[dict[str, Any]]:
            return self._data

    class _Client:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *a: Any) -> None:
            return None

        async def get(self, url: str, params: dict[str, Any]) -> _Resp:
            page = params.get("page")
            if "delisted" in url:
                captured_pages["delisted"].append(page)
                return _Resp(_delisted_page(int(page or 0)))
            captured_pages["stock"].append(page)
            return _Resp(_STOCK_FULL)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)

    class _Log:
        def info(self, *a: Any, **k: Any) -> None:
            return None

        def warning(self, *a: Any, **k: Any) -> None:
            return None

    entries = await ops._fetch_fmp_universe_entries(log=_Log())  # noqa: SLF001
    # delisted: pages 0,1,2 fetched then page 3 (empty) stops the loop.
    assert captured_pages["delisted"] == [0, 1, 2, 3]
    # stock-list: fetched EXACTLY ONCE, with NO page param (single GET — would
    # otherwise loop to the _FMP_MAX_PAGES ceiling and raise).
    assert captured_pages["stock"] == [None]
    # 3 pages × 100 rows of delisted symbols are all present — survivorship-
    # free, not capped at one page.
    delisted = [e for e in entries if e.delisted]
    assert len(delisted) >= 300
    # the full stock roster (250) is present as live (non-delisted) entries.
    live = [e for e in entries if not e.delisted]
    assert len(live) >= 250


# ── SEC shard-error → untrusted FPFD (review #4) ─────────────────────


@pytest.mark.asyncio
async def test_sec_shard_error_yields_untrusted_fpfd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A CIK whose merged submissions payload carries non-empty
    ``_shard_errors`` (a missing OLDEST shard pulls min(filingDate) forward
    → look-ahead) must yield FPFD=None, NOT a confidently-wrong early date
    (review #4)."""
    from scripts import ops

    class _Reader:
        def __enter__(self) -> _Reader:
            return self

        def __exit__(self, *a: Any) -> None:
            return None

        def get_merged_submissions(self, cik: str) -> dict[str, Any]:
            if cik == "0000000002":
                # Shard error → the merged min(filingDate) is untrustworthy.
                return {"_shard_errors": ["CIK0000000002-submissions-001.json"],
                        "filings": {"recent": {"filingDate": ["2015-01-01"]}}}
            return {"filings": {"recent": {"filingDate": ["1994-01-01"]}}}

        def stats(self) -> dict[str, int]:
            return {"shard_error_count": 1}

    class _CFA:
        @staticmethod
        def extract_filing_metadata(payload: dict[str, Any]) -> dict[str, Any]:
            fds = payload["filings"]["recent"]["filingDate"]
            return {"first_public_filing_date": date.fromisoformat(min(fds))}

    class _MapEntry:
        def __init__(self, cik: str, name: str) -> None:
            self.cik = cik
            self.company_name = name

    class _Map:
        async def fetch(self) -> dict[str, Any]:
            return {
                "AAA": _MapEntry("0000000001", "Clean Co"),
                "BBB": _MapEntry("0000000002", "Shard-Broken Co"),
            }

    async def _ensure(*a: Any, **k: Any) -> None:
        return None

    import tpcore.sec.companyfacts_adapter as cfa_mod
    import tpcore.sec.submissions_bulk_reader as sbr_mod
    import tpcore.sec.ticker_cik_map as tcm_mod

    monkeypatch.setattr(sbr_mod, "SECSubmissionsBulkReader", _Reader)
    monkeypatch.setattr(sbr_mod, "ensure_zip_cached", _ensure)
    monkeypatch.setattr(cfa_mod, "SECCompanyFactsAdapter", _CFA)
    monkeypatch.setattr(tcm_mod, "SECTickerCIKMap", _Map)

    class _Log:
        def __init__(self) -> None:
            self.warns: list[tuple[Any, ...]] = []

        def info(self, *a: Any, **k: Any) -> None:
            return None

        def warning(self, *a: Any, **k: Any) -> None:
            self.warns.append((a, k))

    log = _Log()
    entries = await ops._fetch_sec_universe_entries(log=log)  # noqa: SLF001
    by_ticker = {e.ticker: e for e in entries}
    assert by_ticker["AAA"].first_public_filing_date == date(1994, 1, 1)
    # Shard-broken CIK: FPFD untrusted → None (NOT the wrong 2015 date).
    assert by_ticker["BBB"].first_public_filing_date is None
