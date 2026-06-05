"""Stage-level tests for the Plan 3 Phase 1 identity-build stages
(issuers_build, ticker_history_reuse_build, issuer_securities_build,
identity_build orchestrator).

Hermetic: mocked asyncpg pool, monkeypatched SEC source seams (NO
network), in-memory synthetic rows. No real DB, no real API calls.

Pins:
  * the four stages are registered in _STAGE_SPECS + KNOWN_STAGES.
  * all four are off-cycle (NOT the child-first --update order).
  * each defaults dry_run=True (no write without --param dry_run=false).
  * idempotent ON CONFLICT conflict targets (issuer-stable / natural-key).
  * cross-run idempotency: a re-run a year later writes ZERO new rows.
  * the orchestrator runs the four IN ORDER + the BLOCKING gate.

ops-package-shadow discipline: this file imports scripts.ops, so it
carries the xdist_group marker per tests-and-ci rule.
"""
from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("SEC_EDGAR_USER_AGENT", "STE-test test@example.com")

pytestmark = pytest.mark.xdist_group("ops_shadow")


def _ops_text() -> str:
    return (
        Path(__file__).resolve().parents[1] / "scripts" / "ops.py"
    ).read_text(encoding="utf-8")


# ── registration + off-cycle ─────────────────────────────────────────


def test_stages_registered() -> None:
    from scripts import ops
    names = {n for n, _, _ in ops._STAGE_SPECS}  # noqa: SLF001
    for stage in (
        "issuers_build",
        "ticker_history_reuse_build",
        "issuer_securities_build",
        "identity_build",
    ):
        assert stage in names, stage
        assert stage in ops.KNOWN_STAGES, stage


def test_stages_are_off_cycle() -> None:
    from scripts import ops
    for stage in (
        "issuers_build",
        "ticker_history_reuse_build",
        "issuer_securities_build",
        "identity_build",
    ):
        assert stage in ops._OFF_CYCLE_STAGES, stage  # noqa: SLF001


def test_stages_not_in_dashboard_update_cadence() -> None:
    from dashboard_components.health import OPS_UPDATE_STAGES
    for stage in (
        "issuers_build",
        "ticker_history_reuse_build",
        "issuer_securities_build",
        "identity_build",
    ):
        assert stage not in OPS_UPDATE_STAGES, stage


def test_orchestrator_order_is_canonical() -> None:
    """universe_build → issuers_build → ticker_history_reuse_build →
    issuer_securities_build (spec §5.3 identity-first order)."""
    from scripts import ops
    assert ops._IDENTITY_BUILD_ORDER == (  # noqa: SLF001
        "universe_build",
        "issuers_build",
        "ticker_history_reuse_build",
        "issuer_securities_build",
    )


def test_idempotent_conflict_targets_in_source() -> None:
    """Each build's INSERT keys on a stable natural key, never a timestamped
    surrogate — a re-run a year later is a no-op."""
    src = _ops_text()
    assert "ON CONFLICT (cik) DO NOTHING" in src  # issuers
    assert "ON CONFLICT (issuer_id, valid_from) DO NOTHING" in src  # history
    assert (
        "ON CONFLICT (classification_id, valid_from) DO NOTHING" in src
    )  # ticker_history
    assert (
        "ON CONFLICT (issuer_id, classification_id, valid_from) DO NOTHING"
        in src
    )  # issuer_securities


def test_wrapper_script_references_orchestrator() -> None:
    """scripts/run_identity_build.sh wraps the orchestrator (no orphan
    scripts; backfills run through the canonical stage)."""
    text = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "run_identity_build.sh"
    ).read_text(encoding="utf-8")
    assert "--stage identity_build" in text


# ── mocked pool helpers ──────────────────────────────────────────────


def _make_pool(
    fetch_rows: list[dict[str, Any]] | None = None,
    fetchval_answers: dict[str, int] | None = None,
) -> tuple[MagicMock, list[Any]]:
    """A mocked asyncpg pool capturing conn.execute calls.

    ``fetch_rows`` is returned for every pool.fetch / conn.fetch.
    ``fetchval_answers`` maps SQL-substring → int for pool.fetchval (the
    identity gate)."""
    fetch_rows = fetch_rows or []
    fetchval_answers = fetchval_answers or {}
    executed: list[Any] = []

    async def _execute(sql: str, *args: Any) -> str:
        executed.append((sql, args))
        return "INSERT 0 0"

    async def _fetch(sql: str, *args: Any) -> list[Any]:
        return fetch_rows

    async def _fetchval(sql: str, *args: Any) -> int:
        for needle, val in fetchval_answers.items():
            if needle in sql:
                return val
        return 0

    conn = MagicMock()
    conn.execute = AsyncMock(side_effect=_execute)
    conn.fetch = AsyncMock(side_effect=_fetch)
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire)
    pool.fetch = AsyncMock(side_effect=_fetch)
    pool.fetchval = AsyncMock(side_effect=_fetchval)
    return pool, executed


class _FakeReader:
    """A SECSubmissionsBulkReader stand-in with scripted payloads."""

    payloads: dict[str, dict[str, Any]] = {}

    def __enter__(self) -> _FakeReader:
        return self

    def __exit__(self, *a: Any) -> None:
        return None

    def get_merged_submissions(self, cik: str) -> dict[str, Any] | None:
        return self.payloads.get(cik)

    def stats(self) -> dict[str, int]:
        return {"missing_count": 0, "shard_error_count": 0}


def _stub_sec_reader(
    monkeypatch: pytest.MonkeyPatch, payloads: dict[str, dict[str, Any]]
) -> None:
    import tpcore.sec.submissions_bulk_reader as sbr_mod

    reader_cls = type("_R", (_FakeReader,), {"payloads": payloads})

    async def _ensure(*a: Any, **k: Any) -> None:
        return None

    # The stage imports these names IN-BODY from the module, so patch the
    # source module (the import resolves at call time).
    monkeypatch.setattr(sbr_mod, "SECSubmissionsBulkReader", reader_cls)
    monkeypatch.setattr(sbr_mod, "ensure_zip_cached", _ensure)


# ── issuers_build stage ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_issuers_build_dry_run_no_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import ops

    payloads = {
        "0000320193": {
            "name": "Apple Inc.",
            "formerNames": [],
            "fiscalYearEnd": "0930",
            "stateOfIncorporation": "CA",
            "filings": {"recent": {"filingDate": ["1994-12-12"], "form": ["10-K"]}},
        },
    }
    _stub_sec_reader(monkeypatch, payloads)
    pool, executed = _make_pool(
        fetch_rows=[{"cik": "0000320193", "country": "US"}]
    )
    result = await ops._stage_issuers_build(  # noqa: SLF001
        pool, {"dry_run": True, "min_resolved": 1}
    )
    assert result["dry_run"] is True
    assert result["issuers_upserted"] == 0
    assert result["issuers_previewed"] == 1
    assert executed == []


@pytest.mark.asyncio
async def test_issuers_build_live_inserts_and_idempotency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import ops

    payloads = {
        "0000320193": {
            "name": "Apple Inc.",
            "formerNames": [
                {"name": "Apple Computer, Inc.", "from": "1994-12-12",
                 "to": "2007-01-09"},
            ],
            "fiscalYearEnd": "0930",
            "filings": {"recent": {"filingDate": ["1994-12-12"], "form": ["10-K"]}},
        },
    }
    _stub_sec_reader(monkeypatch, payloads)
    pool, executed = _make_pool(
        fetch_rows=[{"cik": "0000320193", "country": "US"}]
    )
    result = await ops._stage_issuers_build(  # noqa: SLF001
        pool, {"dry_run": False, "min_resolved": 1}
    )
    assert result["dry_run"] is False
    assert result["issuers_upserted"] == 1
    # two history rows (former + current open).
    assert result["history_rows"] == 2
    all_sql = "\n".join(c[0] for c in executed)
    assert "ON CONFLICT (cik) DO NOTHING" in all_sql  # idempotent issuers
    assert "ON CONFLICT (issuer_id, valid_from) DO NOTHING" in all_sql
    # issuer_id is the CIK convention.
    issuer_insert = next(c for c in executed if "INTO platform.issuers" in c[0])
    assert issuer_insert[1][0] == ["CIK0000320193"]


@pytest.mark.asyncio
async def test_issuers_build_degraded_source_hard_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A universe CIK whose payload is missing from the (degraded) bulk file
    resolves 0 issuers → producer hard-stop (review #3)."""
    from scripts import ops

    _stub_sec_reader(monkeypatch, {})  # empty bulk file
    pool, _ = _make_pool(fetch_rows=[{"cik": "0000320193", "country": "US"}])
    with pytest.raises(RuntimeError, match="issuers resolved|floor"):
        await ops._stage_issuers_build(  # noqa: SLF001
            pool, {"dry_run": True, "min_resolved": 1}
        )


@pytest.mark.asyncio
async def test_issuers_build_empty_universe_hard_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import ops
    _stub_sec_reader(monkeypatch, {})
    pool, _ = _make_pool(fetch_rows=[])  # universe_build not run yet
    with pytest.raises(RuntimeError, match="0 cik-bearing"):
        await ops._stage_issuers_build(pool, {"dry_run": True})  # noqa: SLF001


@pytest.mark.asyncio
async def test_issuers_build_shard_error_untrusted_fpfd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A CIK with _shard_errors yields FPFD=None (untrusted), not a wrong
    early date (review #4)."""
    from scripts import ops

    payloads = {
        "0000000002": {
            "name": "Shard-Broken Co",
            "formerNames": [],
            "_shard_errors": ["CIK0000000002-submissions-001.json"],
            "filings": {"recent": {"filingDate": ["2015-01-01"], "form": ["10-K"]}},
        },
    }
    _stub_sec_reader(monkeypatch, payloads)
    pool, _ = _make_pool(fetch_rows=[{"cik": "0000000002", "country": "US"}])
    result = await ops._stage_issuers_build(  # noqa: SLF001
        pool, {"dry_run": True, "min_resolved": 1}
    )
    assert result["n_untrusted_fpfd"] == 1


# ── ticker_history_reuse_build stage ─────────────────────────────────


@pytest.mark.asyncio
async def test_ticker_history_reuse_build_dry_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import ops
    rows = [
        {"classification_id": "ID_A", "ticker": "XYZ",
         "lifetime_start": date(2000, 1, 1), "lifetime_end": date(2008, 1, 1)},
        {"classification_id": "ID_B", "ticker": "XYZ",
         "lifetime_start": date(2010, 1, 1), "lifetime_end": None},
    ]
    pool, executed = _make_pool(fetch_rows=rows)
    result = await ops._stage_ticker_history_reuse_build(  # noqa: SLF001
        pool, {"dry_run": True}
    )
    assert result["dry_run"] is True
    # G3: a reused ticker → 2 rows.
    assert result["rows_previewed"] == 2
    assert executed == []


@pytest.mark.asyncio
async def test_ticker_history_reuse_build_live_inserts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import ops
    rows = [
        {"classification_id": "ID_A", "ticker": "XYZ",
         "lifetime_start": date(2000, 1, 1), "lifetime_end": date(2008, 1, 1)},
        {"classification_id": "ID_B", "ticker": "XYZ",
         "lifetime_start": date(2010, 1, 1), "lifetime_end": None},
    ]
    pool, executed = _make_pool(fetch_rows=rows)
    result = await ops._stage_ticker_history_reuse_build(  # noqa: SLF001
        pool, {"dry_run": False, "chunk_size": 100}
    )
    assert result["rows_inserted"] == 2
    all_sql = "\n".join(c[0] for c in executed)
    assert "ON CONFLICT (classification_id, valid_from) DO NOTHING" in all_sql


@pytest.mark.asyncio
async def test_ticker_history_reuse_build_overlap_hard_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Overlapping windows for the same ticker → hard-stop (the EXCLUDE
    would reject; surface the defect)."""
    from scripts import ops
    rows = [
        {"classification_id": "ID_1", "ticker": "OV",
         "lifetime_start": date(2000, 1, 1), "lifetime_end": date(2006, 1, 1)},
        {"classification_id": "ID_2", "ticker": "OV",
         "lifetime_start": date(2005, 1, 1), "lifetime_end": None},
    ]
    pool, _ = _make_pool(fetch_rows=rows)
    with pytest.raises(ValueError, match="overlap"):
        await ops._stage_ticker_history_reuse_build(  # noqa: SLF001
            pool, {"dry_run": True}
        )


# ── issuer_securities_build stage ────────────────────────────────────


@pytest.mark.asyncio
async def test_issuer_securities_build_fanout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GOOG + GOOGL under one CIK → 2 links to the same issuer."""
    from scripts import ops
    rows = [
        {"classification_id": "ID_GOOG", "cik": "0001652044",
         "lifetime_start": date(2014, 4, 3), "lifetime_end": None},
        {"classification_id": "ID_GOOGL", "cik": "0001652044",
         "lifetime_start": date(2004, 8, 19), "lifetime_end": None},
    ]
    pool, executed = _make_pool(fetch_rows=rows)
    result = await ops._stage_issuer_securities_build(  # noqa: SLF001
        pool, {"dry_run": False, "chunk_size": 100}
    )
    assert result["links_inserted"] == 2
    link_insert = next(
        c for c in executed if "INTO platform.issuer_securities" in c[0]
    )
    assert set(link_insert[1][0]) == {"CIK0001652044"}  # one issuer
    all_sql = "\n".join(c[0] for c in executed)
    assert (
        "ON CONFLICT (issuer_id, classification_id, valid_from) DO NOTHING"
        in all_sql
    )


@pytest.mark.asyncio
async def test_issuer_securities_build_dry_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import ops
    rows = [
        {"classification_id": "ID_SEC", "cik": "0000320193",
         "lifetime_start": date(1994, 12, 12), "lifetime_end": None},
    ]
    pool, executed = _make_pool(fetch_rows=rows)
    result = await ops._stage_issuer_securities_build(  # noqa: SLF001
        pool, {"dry_run": True}
    )
    assert result["dry_run"] is True
    assert result["links_previewed"] == 1
    assert executed == []


# ── identity_build orchestrator ──────────────────────────────────────


@pytest.mark.asyncio
async def test_identity_build_orchestrator_runs_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The orchestrator calls the four sub-stages IN ORDER and (dry-run)
    skips the gate."""
    from scripts import ops

    calls: list[str] = []

    async def _fake(name: str) -> Any:
        async def _stage(pool: Any, cfg: Any) -> dict[str, Any]:
            calls.append(name)
            return {"dry_run": True}
        return _stage

    monkeypatch.setattr(ops, "_stage_universe_build", await _fake("universe_build"))
    monkeypatch.setattr(ops, "_stage_issuers_build", await _fake("issuers_build"))
    monkeypatch.setattr(
        ops, "_stage_ticker_history_reuse_build",
        await _fake("ticker_history_reuse_build"),
    )
    monkeypatch.setattr(
        ops, "_stage_issuer_securities_build",
        await _fake("issuer_securities_build"),
    )
    pool, _ = _make_pool()
    result = await ops._stage_identity_build(pool, {"dry_run": True})  # noqa: SLF001
    assert calls == [
        "universe_build",
        "issuers_build",
        "ticker_history_reuse_build",
        "issuer_securities_build",
    ]
    # dry-run → gate skipped.
    assert result["identity_gate"] is None


@pytest.mark.asyncio
async def test_identity_build_live_runs_blocking_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live (dry_run=false) build runs the BLOCKING gate; a violation
    aborts the orchestrator."""
    from scripts import ops

    async def _noop(pool: Any, cfg: Any) -> dict[str, Any]:
        return {"dry_run": False}

    for attr in (
        "_stage_universe_build",
        "_stage_issuers_build",
        "_stage_ticker_history_reuse_build",
        "_stage_issuer_securities_build",
    ):
        monkeypatch.setattr(ops, attr, _noop)

    # gate sees a NULL lifetime_start → raises.
    pool, _ = _make_pool(fetchval_answers={"lifetime_start IS NULL": 2})
    with pytest.raises(RuntimeError, match="identity gate"):
        await ops._stage_identity_build(  # noqa: SLF001
            pool, {"dry_run": False}
        )


@pytest.mark.asyncio
async def test_identity_build_live_passes_clean_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import ops

    async def _noop(pool: Any, cfg: Any) -> dict[str, Any]:
        return {"dry_run": False}

    for attr in (
        "_stage_universe_build",
        "_stage_issuers_build",
        "_stage_ticker_history_reuse_build",
        "_stage_issuer_securities_build",
    ):
        monkeypatch.setattr(ops, attr, _noop)

    pool, _ = _make_pool()  # all gate probes return 0 → clean
    result = await ops._stage_identity_build(  # noqa: SLF001
        pool, {"dry_run": False}
    )
    assert result["identity_gate"]["passed"] is True
