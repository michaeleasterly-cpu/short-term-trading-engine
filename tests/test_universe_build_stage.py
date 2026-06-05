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


def test_insert_is_idempotent_on_conflict_id() -> None:
    src = _stage_source()
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

    conn = MagicMock()
    conn.execute = AsyncMock(side_effect=_execute)
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire)
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
    result = await ops._stage_universe_build(pool, {"dry_run": True})  # noqa: SLF001
    assert result["dry_run"] is True
    assert result["rows_minted"] == 0
    # 3 securities assembled (AAPL sec + MICRO + DEAD fmp-only).
    assert result["rows_previewed"] == 3
    assert result["n_sec"] == 1
    assert result["n_fmp_only"] == 2
    assert result["n_delisted"] == 1
    # NO INSERT executed in dry-run.
    assert executed == []


@pytest.mark.asyncio
async def test_live_run_inserts_chunked(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts import ops
    _stub_sources(monkeypatch)
    pool, executed = _make_pool()
    result = await ops._stage_universe_build(  # noqa: SLF001
        pool, {"dry_run": False, "chunk_size": 2}
    )
    assert result["dry_run"] is False
    assert result["rows_minted"] == 3
    # chunk_size=2 over 3 rows → 2 INSERT calls.
    assert len(executed) == 2
    insert_sql = executed[0][0]
    assert "INSERT INTO platform.ticker_classifications" in insert_sql
    assert "ON CONFLICT (id) DO NOTHING" in insert_sql
    # lifetime_start array is the 9th positional arg ($9); every value is
    # a real date, never the forbidden sentinel.
    sentinel = date(1900, 1, 1)
    for _sql, args in executed:
        lifetime_starts = args[8]
        for ls in lifetime_starts:
            assert ls != sentinel
            assert ls is not None
