"""P0-001 — SEC ticker→CIK map adapter (offline tests).

Tests:
  * TEST-001 sec_ticker_to_cik_exact_match
  * TEST-002 unresolved_is_reported
  * TEST-003 existing_cik_not_overwritten_unsafely

These tests stub the SEC HTTP fetch with an injected map so they are
hermetic (no network, no SEC_EDGAR_USER_AGENT required).
"""
from __future__ import annotations

import pytest

from tpcore.sec.ticker_cik_map import (
    CIKResolveResult,
    SECTickerCIKMap,
    TickerCIKEntry,
)


def _fake_map() -> dict[str, TickerCIKEntry]:
    return {
        "AAPL": TickerCIKEntry(
            ticker="AAPL", cik="0000320193", company_name="Apple Inc.",
        ),
        "AZO": TickerCIKEntry(
            ticker="AZO", cik="0000866787", company_name="AutoZone, Inc.",
        ),
        "MSFT": TickerCIKEntry(
            ticker="MSFT", cik="0000789019",
            company_name="Microsoft Corporation",
        ),
    }


@pytest.mark.asyncio
async def test_001_sec_ticker_to_cik_exact_match() -> None:
    sec = SECTickerCIKMap()
    sec._map = _fake_map()
    result = await sec.resolve_missing_ciks(
        tickers=["AAPL", "AZO"],
        existing_ciks={"AAPL": None, "AZO": None},
    )
    assert isinstance(result, CIKResolveResult)
    assert set(result.resolved.keys()) == {"AAPL", "AZO"}
    assert result.resolved["AAPL"].cik == "0000320193"
    assert result.resolved["AZO"].cik == "0000866787"
    assert result.unresolved == []
    assert result.skipped_already_set == []


@pytest.mark.asyncio
async def test_002_unresolved_is_reported() -> None:
    sec = SECTickerCIKMap()
    sec._map = _fake_map()
    # NEVERHEARDOF doesn't exist in the SEC map.
    result = await sec.resolve_missing_ciks(
        tickers=["AAPL", "NEVERHEARDOF"],
        existing_ciks={"AAPL": None, "NEVERHEARDOF": None},
    )
    assert "AAPL" in result.resolved
    assert "NEVERHEARDOF" in result.unresolved
    assert "NEVERHEARDOF" not in result.resolved


@pytest.mark.asyncio
async def test_003_existing_cik_not_overwritten_unsafely() -> None:
    """If a ticker ALREADY has a CIK populated (e.g. from FMP), the
    resolver MUST report it in ``skipped_already_set`` and must NOT
    place it in ``resolved`` — this preserves operator-provenance
    semantics."""
    sec = SECTickerCIKMap()
    sec._map = _fake_map()
    # AAPL has an operator-set CIK that does NOT match SEC's value.
    # We MUST NOT overwrite it.
    operator_cik = "9999999999"
    result = await sec.resolve_missing_ciks(
        tickers=["AAPL", "AZO"],
        existing_ciks={"AAPL": operator_cik, "AZO": None},
    )
    assert result.skipped_already_set == ["AAPL"]
    assert "AAPL" not in result.resolved
    # AZO had no CIK → resolved.
    assert "AZO" in result.resolved
    assert result.resolved["AZO"].cik == "0000866787"


@pytest.mark.asyncio
async def test_004_empty_inputs_handled() -> None:
    sec = SECTickerCIKMap()
    sec._map = _fake_map()
    result = await sec.resolve_missing_ciks(
        tickers=[], existing_ciks={},
    )
    assert result.resolved == {}
    assert result.unresolved == []
    assert result.skipped_already_set == []
