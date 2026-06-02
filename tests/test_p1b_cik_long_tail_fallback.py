"""P1b — CIK long-tail FMP /profile fallback (hermetic tests).

12 tests pinning the spec + plan contract:

  * tpcore/fmp/profile_adapter.py — terminal-state classification +
    CIK normalisation (TEST 1, 5–7).
  * scripts/ops.py::_stage_backfill_sec_metadata FMP sub-leg —
    behavioural contract (TEST 2–4, 8–11).
  * Schema sentinel — no migration required (TEST 12).

No live network. No live DB. Injects ``httpx.MockTransport`` for the
adapter calls and the ``_mock_pool`` pattern from
``tests/test_backfill_sec_metadata_stage.py`` for the asyncpg surface.

Spec: docs/superpowers/specs/2026-06-01-p1b-cik-long-tail-backfill.md
Plan: docs/superpowers/plans/2026-06-01-p1b-cik-long-tail-backfill-plan.md
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

os.environ.setdefault("SEC_EDGAR_USER_AGENT", "STE-test test@example.com")
os.environ.setdefault("FMP_API_KEY", "TEST_KEY_NOT_REAL")


_REPO = Path(__file__).resolve().parents[1]


# ─────────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────────


def _baseline_snapshot() -> dict[str, int]:
    return {
        "total": 100, "has_cik": 50,
        "has_sec_document_type_primary": 0,
        "has_first_public_filing_date": 0,
        "has_last_filing_date": 0,
        "has_fiscal_year_end_month": 0,
        "has_metadata_source": 0,
        "has_cik_source": 30,
    }


def _ticker_row(
    ticker: str, *, cik: str | None = None, lifetime_end: object = None,
) -> dict:
    """Mirrors the scope-resolution SELECT in scripts/ops.py."""
    return {
        "ticker": ticker,
        "cik": cik,
        "country": None,
        "sec_document_type_primary": None,
        "first_public_filing_date": None,
        "last_filing_date": None,
        "fiscal_year_end_month": None,
        "metadata_source": None,
        # lifetime_end isn't in the scope query today, but the
        # adapter's defensive scan tolerates extra keys.
    }


def _mock_pool(snapshot: dict, scope_rows: list[dict] | None = None) -> MagicMock:
    """Mock asyncpg pool. Records every ``executemany`` and ``execute``
    call against the conn so tests can assert what was written.

    Extended from tests/test_backfill_sec_metadata_stage.py::_mock_pool
    to also record per-call ``execute`` payloads for the FMP per-row
    UPDATE pattern.
    """
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=snapshot)
    conn.fetch = AsyncMock(return_value=scope_rows or [])
    conn.executemany = AsyncMock(return_value=None)
    # Default execute returns "UPDATE 1" so FMP per-row writes count
    # as successful. Individual tests override for skip scenarios.
    conn.execute = AsyncMock(return_value="UPDATE 1")
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


def _mock_sec_ticker_cik_map(
    *,
    resolved: dict | None = None,
    unresolved: list[str] | None = None,
    skipped: list[str] | None = None,
    monkeypatch,
) -> None:
    """Patch the SEC ticker→CIK map used by the stage so the SEC leg
    behaves deterministically."""
    from tpcore.sec import ticker_cik_map as _mod

    async def _fake_resolve(self, tickers, existing_ciks):
        return _mod.CIKResolveResult(
            resolved=resolved or {},
            unresolved=unresolved or [],
            skipped_already_set=skipped or [],
        )

    monkeypatch.setattr(
        _mod.SECTickerCIKMap, "resolve_missing_ciks", _fake_resolve,
    )


def _fmp_profile_handler(per_ticker: dict[str, dict]):
    """Build an httpx.MockTransport request handler returning the
    response JSON for ``per_ticker[symbol]``. Missing → empty list."""

    def _handler(request: httpx.Request) -> httpx.Response:
        sym = request.url.params.get("symbol", "")
        entry = per_ticker.get(sym, {})
        status = entry.get("status", 200)
        body = entry.get("body", [])
        return httpx.Response(status, json=body)

    return _handler


# ─────────────────────────────────────────────────────────────────────
# TEST 1 — adapter normalises CIK from FMP response
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fmp_profile_adapter_extracts_cik() -> None:
    from tpcore.fmp.profile_adapter import fetch_profile

    handler = _fmp_profile_handler({
        "FOREIGN1": {"body": [{"symbol": "FOREIGN1", "cik": "123456", "country": "BR"}]},
    })
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await fetch_profile(client, "FOREIGN1", api_key="K")
    assert result.state == "resolved"
    assert result.cik == "0000123456"           # 10-padded
    assert result.country == "BR"
    assert result.profiles_count == 1
    assert result.returned_symbol == "FOREIGN1"


# ─────────────────────────────────────────────────────────────────────
# TEST 2 — happy-path resolves unresolved ticker through the stage
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fallback_resolves_unresolved_ticker_with_fmp_cik(
    monkeypatch,
) -> None:
    from scripts.ops import _stage_backfill_sec_metadata

    _mock_sec_ticker_cik_map(
        resolved={}, unresolved=["FOREIGN1"], monkeypatch=monkeypatch,
    )

    handler = _fmp_profile_handler({
        "FOREIGN1": {"body": [{"symbol": "FOREIGN1", "cik": "987654", "country": "BR"}]},
    })
    transport = httpx.MockTransport(handler)
    import httpx as _httpx_mod
    real_async_client = _httpx_mod.AsyncClient

    def _patched(*args, **kwargs):
        return real_async_client(transport=transport, **{k: v for k, v in kwargs.items() if k != "transport"})

    monkeypatch.setattr(_httpx_mod, "AsyncClient", _patched)

    pool = _mock_pool(
        _baseline_snapshot(),
        scope_rows=[_ticker_row("FOREIGN1", cik=None)],
    )
    out = await _stage_backfill_sec_metadata(
        pool,
        {
            "dry_run": False,
            "do_cik": True,
            "do_metadata": False,
            "do_fmp_fallback": True,
            "fmp_rate_limit_sleep_s": 0.0,
            "tickers": "FOREIGN1",
        },
    )
    assert out["cik_fmp_fallback"]["candidates"] == 1
    assert out["cik_fmp_fallback"]["resolved"] == 1
    assert out["cik_fmp_fallback"]["written"] == 1
    conn = pool.acquire.return_value.__aenter__.return_value
    # FMP write uses per-row execute, not executemany (per the plan's
    # observable-row-count design). Confirm the call landed.
    assert conn.execute.await_count >= 1
    fmp_calls = [
        c for c in conn.execute.await_args_list
        if "cik_source = 'fmp'" in c.args[0]
    ]
    assert len(fmp_calls) == 1
    assert fmp_calls[0].args[1] == "FOREIGN1"
    assert fmp_calls[0].args[2] == "0000987654"


# ─────────────────────────────────────────────────────────────────────
# TEST 3 — existing non-NULL CIK is never overwritten
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fallback_never_overwrites_existing_cik(
    monkeypatch,
) -> None:
    from scripts.ops import _stage_backfill_sec_metadata

    # SEC map says FOREIGN1 has a CIK already (skipped_already_set)
    # and DOES NOT include it in unresolved. The FMP sub-leg's
    # unresolved input is empty → no FMP call possible.
    _mock_sec_ticker_cik_map(
        resolved={}, unresolved=[],
        skipped=["FOREIGN1"], monkeypatch=monkeypatch,
    )

    # Even if FMP would return something, the unresolved list is
    # empty so the FMP loop never iterates. We still set up a
    # transport that would return a bogus CIK to prove no call goes
    # out for FOREIGN1.
    call_log: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        call_log.append(request.url.params.get("symbol", ""))
        return httpx.Response(200, json=[{"symbol": "FOREIGN1", "cik": "111111"}])

    transport = httpx.MockTransport(_handler)
    import httpx as _httpx_mod
    real = _httpx_mod.AsyncClient
    monkeypatch.setattr(
        _httpx_mod, "AsyncClient",
        lambda *a, **kw: real(
            transport=transport,
            **{k: v for k, v in kw.items() if k != "transport"},
        ),
    )

    pool = _mock_pool(
        _baseline_snapshot(),
        scope_rows=[_ticker_row("FOREIGN1", cik="0000999999")],
    )
    out = await _stage_backfill_sec_metadata(
        pool,
        {
            "dry_run": False,
            "do_cik": True,
            "do_metadata": False,
            "do_fmp_fallback": True,
            "fmp_rate_limit_sleep_s": 0.0,
            "tickers": "FOREIGN1",
        },
    )
    # The unresolved list was empty (FOREIGN1 was in
    # skipped_already_set, not unresolved) so the FMP loop made no
    # candidates.
    assert out["cik_fmp_fallback"]["candidates"] == 0
    assert out["cik_fmp_fallback"]["resolved"] == 0
    assert out["cik_fmp_fallback"]["written"] == 0
    # The transport must NEVER have been invoked.
    assert call_log == []
    # The existing-CIK row was preserved — no execute on
    # ticker_classifications for FOREIGN1 with the FMP write shape.
    conn = pool.acquire.return_value.__aenter__.return_value
    fmp_calls = [
        c for c in conn.execute.await_args_list
        if "cik_source = 'fmp'" in c.args[0]
    ]
    assert len(fmp_calls) == 0


# ─────────────────────────────────────────────────────────────────────
# TEST 4 — symbol mismatch fails closed + writes divergence event
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_symbol_mismatch_fails_closed_and_logs_divergence(
    monkeypatch,
) -> None:
    from scripts.ops import _stage_backfill_sec_metadata

    _mock_sec_ticker_cik_map(
        resolved={}, unresolved=["FOREIGN1"], monkeypatch=monkeypatch,
    )

    handler = _fmp_profile_handler({
        "FOREIGN1": {"body": [{"symbol": "OTHER", "cik": "123"}]},
    })
    transport = httpx.MockTransport(handler)
    import httpx as _httpx_mod
    real = _httpx_mod.AsyncClient
    monkeypatch.setattr(
        _httpx_mod, "AsyncClient",
        lambda *a, **kw: real(
            transport=transport,
            **{k: v for k, v in kw.items() if k != "transport"},
        ),
    )

    pool = _mock_pool(
        _baseline_snapshot(),
        scope_rows=[_ticker_row("FOREIGN1", cik=None)],
    )
    out = await _stage_backfill_sec_metadata(
        pool,
        {
            "dry_run": False,
            "do_cik": True,
            "do_metadata": False,
            "do_fmp_fallback": True,
            "fmp_rate_limit_sleep_s": 0.0,
            "tickers": "FOREIGN1",
        },
    )
    assert out["cik_fmp_fallback"]["symbol_mismatch"] == 1
    assert out["cik_fmp_fallback"]["resolved"] == 0
    assert out["cik_fmp_fallback"]["written"] == 0
    assert out["cik_fmp_fallback"]["divergence_events_written"] == 1

    # Confirm an INSERT INTO platform.application_log with
    # IDENTITY_DIVERGENCE_INVESTIGATE event_type was issued.
    conn = pool.acquire.return_value.__aenter__.return_value
    appl_calls = [
        c for c in conn.executemany.await_args_list
        if "platform.application_log" in c.args[0]
        and "IDENTITY_DIVERGENCE_INVESTIGATE" in c.args[0]
    ]
    assert len(appl_calls) == 1
    # Payload assertion.
    rows = appl_calls[0].args[1]
    assert len(rows) == 1
    payload = json.loads(rows[0][2])
    assert payload["ticker"] == "FOREIGN1"
    assert payload["returned_symbol"] == "OTHER"
    assert payload["reason"] == "fmp_symbol_mismatch"
    assert payload["source"] == "p1b_fmp_fallback"


# ─────────────────────────────────────────────────────────────────────
# TEST 5 — no_cik_in_profile terminal state
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_cik_in_profile_terminal_state() -> None:
    from tpcore.fmp.profile_adapter import fetch_profile

    handler = _fmp_profile_handler({
        "FOREIGN1": {"body": [{"symbol": "FOREIGN1", "cik": ""}]},
    })
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await fetch_profile(client, "FOREIGN1", api_key="K")
    assert result.state == "no_cik_in_profile"
    assert result.cik is None
    assert result.profiles_count == 1


# ─────────────────────────────────────────────────────────────────────
# TEST 6 — no_match terminal state
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_profile_terminal_state() -> None:
    from tpcore.fmp.profile_adapter import fetch_profile

    handler = _fmp_profile_handler({"FOREIGN1": {"body": []}})
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await fetch_profile(client, "FOREIGN1", api_key="K")
    assert result.state == "no_match"
    assert result.cik is None
    assert result.profiles_count == 0


# ─────────────────────────────────────────────────────────────────────
# TEST 7 — ambiguous (multi-profile) terminal state
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_multiple_profiles_ambiguous_terminal_state() -> None:
    from tpcore.fmp.profile_adapter import fetch_profile

    handler = _fmp_profile_handler({
        "FOREIGN1": {"body": [
            {"symbol": "FOREIGN1", "cik": "111"},
            {"symbol": "FOREIGN1", "cik": "222"},
        ]},
    })
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await fetch_profile(client, "FOREIGN1", api_key="K")
    assert result.state == "ambiguous_response"
    assert result.profiles_count == 2
    assert result.cik is None


# ─────────────────────────────────────────────────────────────────────
# TEST 8 — FMP error continues batch
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fmp_error_continues_batch(monkeypatch) -> None:
    from scripts.ops import _stage_backfill_sec_metadata

    _mock_sec_ticker_cik_map(
        resolved={}, unresolved=["FOREIGN1", "FOREIGN2"],
        monkeypatch=monkeypatch,
    )

    handler = _fmp_profile_handler({
        "FOREIGN1": {"status": 500, "body": {"err": "internal"}},
        "FOREIGN2": {"body": [{"symbol": "FOREIGN2", "cik": "555555"}]},
    })
    transport = httpx.MockTransport(handler)
    import httpx as _httpx_mod
    real = _httpx_mod.AsyncClient
    monkeypatch.setattr(
        _httpx_mod, "AsyncClient",
        lambda *a, **kw: real(
            transport=transport,
            **{k: v for k, v in kw.items() if k != "transport"},
        ),
    )

    pool = _mock_pool(
        _baseline_snapshot(),
        scope_rows=[
            _ticker_row("FOREIGN1", cik=None),
            _ticker_row("FOREIGN2", cik=None),
        ],
    )
    out = await _stage_backfill_sec_metadata(
        pool,
        {
            "dry_run": False,
            "do_cik": True,
            "do_metadata": False,
            "do_fmp_fallback": True,
            "fmp_rate_limit_sleep_s": 0.0,
            "tickers": "FOREIGN1,FOREIGN2",
        },
    )
    assert out["cik_fmp_fallback"]["candidates"] == 2
    assert out["cik_fmp_fallback"]["fmp_error"] == 1
    assert out["cik_fmp_fallback"]["resolved"] == 1
    assert out["cik_fmp_fallback"]["written"] == 1


# ─────────────────────────────────────────────────────────────────────
# TEST 9 — lifetime_end IS NOT NULL skips the row at write time
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_skips_lifetime_ended(monkeypatch) -> None:
    from scripts.ops import _stage_backfill_sec_metadata

    _mock_sec_ticker_cik_map(
        resolved={}, unresolved=["DEADCO"], monkeypatch=monkeypatch,
    )

    handler = _fmp_profile_handler({
        "DEADCO": {"body": [{"symbol": "DEADCO", "cik": "777"}]},
    })
    transport = httpx.MockTransport(handler)
    import httpx as _httpx_mod
    real = _httpx_mod.AsyncClient
    monkeypatch.setattr(
        _httpx_mod, "AsyncClient",
        lambda *a, **kw: real(
            transport=transport,
            **{k: v for k, v in kw.items() if k != "transport"},
        ),
    )

    pool = _mock_pool(
        _baseline_snapshot(),
        scope_rows=[_ticker_row("DEADCO", cik=None)],
    )
    # The pool's execute returns "UPDATE 0" → no rows updated, which
    # is what the lifetime_end IS NOT NULL guard yields at SQL time.
    conn = pool.acquire.return_value.__aenter__.return_value
    conn.execute.return_value = "UPDATE 0"

    out = await _stage_backfill_sec_metadata(
        pool,
        {
            "dry_run": False,
            "do_cik": True,
            "do_metadata": False,
            "do_fmp_fallback": True,
            "fmp_rate_limit_sleep_s": 0.0,
            "tickers": "DEADCO",
        },
    )
    assert out["cik_fmp_fallback"]["resolved"] == 1   # FMP did resolve
    assert out["cik_fmp_fallback"]["written"] == 0    # but write was rejected
    assert out["cik_fmp_fallback"]["skipped_lifetime_ended"] >= 1


# ─────────────────────────────────────────────────────────────────────
# TEST 10 — dry_run persists nothing
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dry_run_persists_nothing(monkeypatch) -> None:
    from scripts.ops import _stage_backfill_sec_metadata

    _mock_sec_ticker_cik_map(
        resolved={}, unresolved=["FOREIGN1"], monkeypatch=monkeypatch,
    )

    handler = _fmp_profile_handler({
        "FOREIGN1": {"body": [{"symbol": "FOREIGN1", "cik": "987654"}]},
    })
    transport = httpx.MockTransport(handler)
    import httpx as _httpx_mod
    real = _httpx_mod.AsyncClient
    monkeypatch.setattr(
        _httpx_mod, "AsyncClient",
        lambda *a, **kw: real(
            transport=transport,
            **{k: v for k, v in kw.items() if k != "transport"},
        ),
    )

    pool = _mock_pool(
        _baseline_snapshot(),
        scope_rows=[_ticker_row("FOREIGN1", cik=None)],
    )
    out = await _stage_backfill_sec_metadata(
        pool,
        {
            "dry_run": True,                  # ← key under test
            "do_cik": True,
            "do_metadata": False,
            "do_fmp_fallback": True,
            "fmp_rate_limit_sleep_s": 0.0,
            "tickers": "FOREIGN1",
        },
    )
    # Adapter still ran (resolved counter); but written=0 and no UPDATE
    # call to ticker_classifications, no INSERT to application_log.
    assert out["dry_run"] is True
    assert out["cik_fmp_fallback"]["resolved"] == 1
    assert out["cik_fmp_fallback"]["written"] == 0
    assert out["cik_fmp_fallback"]["divergence_events_written"] == 0

    conn = pool.acquire.return_value.__aenter__.return_value
    fmp_updates = [
        c for c in conn.execute.await_args_list
        if "cik_source = 'fmp'" in c.args[0]
    ]
    assert fmp_updates == []
    appl_inserts = [
        c for c in conn.executemany.await_args_list
        if "platform.application_log" in c.args[0]
    ]
    assert appl_inserts == []


# ─────────────────────────────────────────────────────────────────────
# TEST 11 — summary counts include all 7 terminal states
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_summary_counts_include_all_terminal_states(
    monkeypatch,
) -> None:
    from scripts.ops import _stage_backfill_sec_metadata

    _mock_sec_ticker_cik_map(
        resolved={}, unresolved=[], monkeypatch=monkeypatch,
    )
    pool = _mock_pool(_baseline_snapshot(), scope_rows=[])
    out = await _stage_backfill_sec_metadata(
        pool,
        {
            "dry_run": True,
            "do_fmp_fallback": True,
            "tickers": "NOTHING_TO_DO",
        },
    )
    assert "cik_fmp_fallback" in out
    required_keys = {
        "candidates",
        "resolved",
        "no_match",
        "symbol_mismatch",
        "no_cik_in_profile",
        "fmp_error",
        "skipped_existing_cik",
        "skipped_lifetime_ended",
        "written",
        "divergence_events_written",
    }
    assert required_keys <= set(out["cik_fmp_fallback"].keys())


# ─────────────────────────────────────────────────────────────────────
# TEST 12 — no migration required (static parse of CHECK constraint)
# ─────────────────────────────────────────────────────────────────────


def test_no_migration_required_sentinel() -> None:
    """The 20260530_0200 migration's ``_VALID_CIK_SOURCES`` tuple
    must already include ``'fmp'`` so the P1b implementation can
    write ``cik_source='fmp'`` without a new migration.

    The plan calls this out explicitly; the sentinel locks it down so
    a future migration that drops 'fmp' from the CHECK constraint
    reds CI. Static text parse so we never import the migration
    module (no SLF access; no alembic init side effects).
    """
    migration_path = (
        _REPO / "platform" / "migrations" / "versions"
        / "20260530_0200_issuer_metadata_foundation.py"
    )
    assert migration_path.is_file(), f"missing {migration_path}"
    text = migration_path.read_text(encoding="utf-8")
    # Find the _VALID_CIK_SOURCES tuple literal and assert 'fmp' is
    # in its element list.
    import re as _re
    m = _re.search(
        r"_VALID_CIK_SOURCES\s*=\s*\(\s*([^)]+)\)",
        text,
        flags=_re.DOTALL,
    )
    assert m is not None, (
        "migration must define ``_VALID_CIK_SOURCES = (...)`` tuple "
        "with the CHECK constraint values"
    )
    elements_blob = m.group(1)
    assert "'fmp'" in elements_blob or '"fmp"' in elements_blob, (
        "schema CHECK constraint must permit cik_source='fmp' so "
        "P1b can persist FMP-derived CIKs without a new migration"
    )
