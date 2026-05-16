"""Tests for the Finnhub insider-sentiment adapter.

Required cases per adapter_readiness.md: happy, empty, 429→retry,
403→no-retry→DataProviderOutage, malformed, config error. MockTransport.
"""
from __future__ import annotations

from datetime import date

import httpx
import pytest

from tpcore.finnhub import FinnhubAdapter
from tpcore.outage import DataProviderOutage

_OK = {
    "symbol": "AAPL",
    "data": [
        {"symbol": "AAPL", "year": 2024, "month": 2,
         "change": -89388, "mspr": -63.737488},
        {"symbol": "AAPL", "year": 2024, "month": 4,
         "change": -652766, "mspr": -33.164017},
    ],
}
_FROM, _TO = date(2024, 1, 1), date(2024, 12, 31)


def _adapter(handler) -> FinnhubAdapter:
    return FinnhubAdapter(
        api_key="fh_test",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                 base_url="https://finnhub.io/api/v1"),
    )


async def test_happy_path() -> None:
    a = _adapter(lambda req: httpx.Response(200, json=_OK))
    res = await a.get_insider_sentiment("AAPL", _FROM, _TO)
    assert res.symbol == "AAPL"
    assert len(res.records) == 2
    r0 = res.records[0]
    assert r0.year == 2024 and r0.month == 2
    assert str(r0.mspr) == "-63.737488" and str(r0.net_change) == "-89388"
    await a.aclose()


async def test_empty_data() -> None:
    a = _adapter(lambda req: httpx.Response(200, json={"symbol": "AAPL", "data": []}))
    res = await a.get_insider_sentiment("AAPL", _FROM, _TO)
    assert res.records == []
    await a.aclose()


async def test_rate_limit_retries_then_succeeds() -> None:
    n = {"c": 0}

    def h(req: httpx.Request) -> httpx.Response:
        n["c"] += 1
        if n["c"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={})
        return httpx.Response(200, json=_OK)

    a = _adapter(h)
    res = await a.get_insider_sentiment("AAPL", _FROM, _TO)
    assert len(res.records) == 2 and n["c"] == 2
    await a.aclose()


async def test_403_premium_is_permanent_outage_no_retry() -> None:
    n = {"c": 0}

    def h(req: httpx.Request) -> httpx.Response:
        n["c"] += 1
        return httpx.Response(403, json={"error": "You don't have access"})

    a = _adapter(h)
    with pytest.raises(DataProviderOutage, match="403"):
        await a.get_insider_sentiment("AAPL", _FROM, _TO)
    assert n["c"] == 1
    await a.aclose()


async def test_malformed_payload_raises_outage() -> None:
    a = _adapter(lambda req: httpx.Response(
        200, json={"symbol": "AAPL", "data": [{"year": 2024}]}))
    with pytest.raises(DataProviderOutage, match="malformed"):
        await a.get_insider_sentiment("AAPL", _FROM, _TO)
    await a.aclose()


def test_missing_api_key_fails_fast(monkeypatch) -> None:
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    with pytest.raises(DataProviderOutage, match="FINNHUB_API_KEY"):
        FinnhubAdapter()
