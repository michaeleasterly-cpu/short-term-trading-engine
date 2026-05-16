"""Tests for the greeks.pro max-pain adapter.

Required cases per adapter_readiness.md: happy path, empty, rate-limit
(429→retry), permanent (403→no retry → DataProviderOutage), malformed
payload, config error at construction. httpx.MockTransport — no network.
"""
from __future__ import annotations

import httpx
import pytest

from tpcore.greeks import GreeksProAdapter
from tpcore.outage import DataProviderOutage

_OK = {
    "symbol": "SPY",
    "spotPrice": 739.17,
    "timestamp": 1778913804,
    "results": [
        {
            "expiration": 1779062400, "dte": 1, "maxPainStrike": 743,
            "totalPainAtMax": 33917300, "spotDistance": 3.83,
            "spotDistancePct": 0.5181,
        }
    ],
}


def _adapter(handler) -> GreeksProAdapter:
    return GreeksProAdapter(
        api_key="grk_test",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                 base_url="https://greeks.pro"),
    )


async def test_happy_path() -> None:
    a = _adapter(lambda req: httpx.Response(200, json=_OK))
    snap = await a.get_max_pain("SPY")
    assert snap.symbol == "SPY"
    assert str(snap.spot_price) == "739.17"
    assert len(snap.results) == 1
    r = snap.results[0]
    assert r.dte == 1 and str(r.max_pain_strike) == "743"
    assert r.expiration_date.year == 2026
    await a.aclose()


async def test_empty_results() -> None:
    a = _adapter(lambda req: httpx.Response(200, json={**_OK, "results": []}))
    snap = await a.get_max_pain("SPY")
    assert snap.results == []
    await a.aclose()


async def test_rate_limit_retries_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={})
        return httpx.Response(200, json=_OK)

    a = _adapter(handler)
    snap = await a.get_max_pain("SPY")
    assert snap.symbol == "SPY"
    assert calls["n"] == 2  # one retry
    await a.aclose()


async def test_403_paid_tier_is_permanent_outage_no_retry() -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            403, json={"error": "requires plan 'trader' or higher"})

    a = _adapter(handler)
    with pytest.raises(DataProviderOutage, match="403"):
        await a.get_max_pain("SPY")
    assert calls["n"] == 1  # no retry on permanent 4xx
    await a.aclose()


async def test_malformed_payload_raises_outage() -> None:
    a = _adapter(lambda req: httpx.Response(200, json={"symbol": "SPY"}))
    with pytest.raises(DataProviderOutage, match="malformed"):
        await a.get_max_pain("SPY")
    await a.aclose()


def test_missing_api_key_fails_fast(monkeypatch) -> None:
    monkeypatch.delenv("GREEKS_API_KEY", raising=False)
    with pytest.raises(DataProviderOutage, match="GREEKS_API_KEY"):
        GreeksProAdapter()
