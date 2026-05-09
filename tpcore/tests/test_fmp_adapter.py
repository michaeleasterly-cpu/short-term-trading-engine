"""Tests for ``tpcore.fmp.FMPFundamentalsAdapter``.

Mocks ``httpx.AsyncClient`` so the adapter is exercised end-to-end —
URL construction, PIT filter, statement merge, normalization — without
hitting FMP. A separate live test (gated behind ``RUN_FMP_LIVE_TESTS``)
exercises the real endpoint with the configured API key.
"""
from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from typing import Any

import httpx
import pytest

from tpcore.fmp import FMPFundamentalsAdapter
from tpcore.outage import DataProviderOutage


def _income(date_str: str, filing_str: str, revenue: float, net_income: float) -> dict:
    return {
        "date": date_str,
        "filingDate": filing_str,
        "period": "Q1",
        "revenue": revenue,
        "netIncome": net_income,
    }


def _cash(date_str: str, filing_str: str, fcf: float, capex: float) -> dict:
    return {
        "date": date_str,
        "filingDate": filing_str,
        "period": "Q1",
        "freeCashFlow": fcf,
        "operatingCashFlow": fcf - capex,
        "capitalExpenditure": capex,
    }


def _balance(date_str: str, filing_str: str, ta: float, tl: float, recv: float) -> dict:
    return {
        "date": date_str,
        "filingDate": filing_str,
        "period": "Q1",
        "totalAssets": ta,
        "totalLiabilities": tl,
        "totalCurrentAssets": ta * 0.4,
        "totalCurrentLiabilities": tl * 0.3,
        "netReceivables": recv,
        "cashAndCashEquivalents": ta * 0.1,
    }


def _make_mock_transport(
    income: list[dict], cash: list[dict], balance: list[dict]
) -> httpx.MockTransport:
    """Map the three /stable/ endpoints to canned JSON."""
    bodies = {
        "income-statement": income,
        "cash-flow-statement": cash,
        "balance-sheet-statement": balance,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        for k, body in bodies.items():
            if k in request.url.path:
                return httpx.Response(200, json=body)
        return httpx.Response(404, json={"error": "unknown endpoint"})

    return httpx.MockTransport(handler)


# ────────────────────────────────────────────────────────────────────────────
# Adapter behavior
# ────────────────────────────────────────────────────────────────────────────


async def test_get_quarterly_fundamentals_merges_three_statements() -> None:
    transport = _make_mock_transport(
        income=[_income("2025-09-30", "2025-10-25", 1000.0, 200.0)],
        cash=[_cash("2025-09-30", "2025-10-25", 180.0, -30.0)],
        balance=[_balance("2025-09-30", "2025-10-25", 5000.0, 2000.0, 400.0)],
    )
    client = httpx.AsyncClient(transport=transport)
    adapter = FMPFundamentalsAdapter(api_key="fake", client=client)
    try:
        result = await adapter.get_quarterly_fundamentals("AAPL")
    finally:
        await adapter.aclose()
    assert result["symbol"] == "AAPL"
    assert result["revenue"] == Decimal("1000.0")
    assert result["net_income"] == Decimal("200.0")
    assert result["fcf"] == Decimal("180.0")
    assert result["total_assets"] == Decimal("5000.0")
    assert result["total_liabilities"] == Decimal("2000.0")
    assert result["receivables"] == Decimal("400.0")
    assert result["filing_date"] == date(2025, 10, 25)
    assert result["history"] == []


async def test_get_quarterly_fundamentals_pit_filter_drops_future_filings() -> None:
    transport = _make_mock_transport(
        income=[
            _income("2025-09-30", "2025-10-25", 1000.0, 200.0),
            _income("2025-06-30", "2025-07-15", 950.0, 180.0),
        ],
        cash=[
            _cash("2025-09-30", "2025-10-25", 180.0, -30.0),
            _cash("2025-06-30", "2025-07-15", 165.0, -28.0),
        ],
        balance=[
            _balance("2025-09-30", "2025-10-25", 5000.0, 2000.0, 400.0),
            _balance("2025-06-30", "2025-07-15", 4900.0, 1980.0, 380.0),
        ],
    )
    client = httpx.AsyncClient(transport=transport)
    adapter = FMPFundamentalsAdapter(api_key="fake", client=client)
    try:
        # Force a PIT cutoff that excludes the Q3 filing (filed Oct 25).
        result = await adapter.get_quarterly_fundamentals(
            "AAPL", as_of_date=date(2025, 10, 1)
        )
    finally:
        await adapter.aclose()
    assert result["filing_date"] == date(2025, 7, 15)
    assert result["revenue"] == Decimal("950.0")
    assert result["history"] == []


async def test_caches_repeated_lookups() -> None:
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if "income-statement" in request.url.path:
            return httpx.Response(
                200, json=[_income("2025-09-30", "2025-10-25", 1000.0, 200.0)]
            )
        if "cash-flow-statement" in request.url.path:
            return httpx.Response(
                200, json=[_cash("2025-09-30", "2025-10-25", 180.0, -30.0)]
            )
        return httpx.Response(
            200, json=[_balance("2025-09-30", "2025-10-25", 5000.0, 2000.0, 400.0)]
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = FMPFundamentalsAdapter(api_key="fake", client=client)
    try:
        await adapter.get_quarterly_fundamentals("AAPL")
        first_count = call_count["n"]
        await adapter.get_quarterly_fundamentals("AAPL")
        assert call_count["n"] == first_count, "second call should be served from cache"
    finally:
        await adapter.aclose()


async def test_raises_outage_on_persistent_4xx() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "forbidden"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = FMPFundamentalsAdapter(api_key="fake", client=client)
    try:
        with pytest.raises(DataProviderOutage):
            await adapter.get_quarterly_fundamentals("AAPL")
    finally:
        await adapter.aclose()


async def test_raises_outage_when_no_periods_match_pit() -> None:
    transport = _make_mock_transport(
        income=[_income("2025-09-30", "2026-01-01", 1000.0, 200.0)],
        cash=[_cash("2025-09-30", "2026-01-01", 180.0, -30.0)],
        balance=[_balance("2025-09-30", "2026-01-01", 5000.0, 2000.0, 400.0)],
    )
    client = httpx.AsyncClient(transport=transport)
    adapter = FMPFundamentalsAdapter(api_key="fake", client=client)
    try:
        with pytest.raises(DataProviderOutage):
            await adapter.get_quarterly_fundamentals(
                "AAPL", as_of_date=date(2025, 10, 1)
            )
    finally:
        await adapter.aclose()


def test_init_without_api_key_raises_outage(monkeypatch: Any) -> None:
    monkeypatch.delenv("FMP_API_KEY", raising=False)
    with pytest.raises(DataProviderOutage):
        FMPFundamentalsAdapter()


# ────────────────────────────────────────────────────────────────────────────
# Optional live test
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.skipif(
    not os.environ.get("RUN_FMP_LIVE_TESTS"),
    reason="RUN_FMP_LIVE_TESTS not set",
)
async def test_live_aapl_smoke() -> None:
    async with FMPFundamentalsAdapter() as adapter:
        result = await adapter.get_quarterly_fundamentals(
            "AAPL", as_of_date=date(2025, 12, 31)
        )
    assert result["revenue"] is not None
    assert result["net_income"] is not None
    assert result["fcf"] is not None
    assert result["filing_date"] is not None
