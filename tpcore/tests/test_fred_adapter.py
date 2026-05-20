"""Tests for ``tpcore.fred.FREDAdapter``.

Per the 5-stage data-adapter pipeline, every adapter ships with:
* happy path (200 OK, expected normalized shape)
* empty response (no observations → empty list, not crash)
* rate-limit (429 → retry via @with_retry → eventual success)
* permanent failure (4xx-not-429 → no retry, DataProviderOutage raised)
* missing-value handling (FRED's "." sentinel → filtered)
* idempotency (parse twice → same output)
* config error (missing FRED_API_KEY → fail-fast)

All tests run against ``httpx.MockTransport`` — no live FRED calls in CI.
"""
from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from unittest.mock import patch

import httpx
import pytest

from tpcore.fred import INDICATOR_SERIES, FREDAdapter
from tpcore.outage import DataProviderOutage


def _ua_env():
    return {"FRED_API_KEY": "test-key-1234"}


_T10Y2Y_PAYLOAD = {
    "observations": [
        {"date": "2024-01-02", "value": "-0.34"},
        {"date": "2024-01-03", "value": "-0.32"},
        {"date": "2024-01-04", "value": "."},  # FRED missing-value sentinel
        {"date": "2024-01-05", "value": "-0.30"},
    ],
}

_EMPTY_PAYLOAD = {"observations": []}


# ── 1. Happy path ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_observations_happy_path():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_T10Y2Y_PAYLOAD)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.stlouisfed.org/fred",
    )
    with patch.dict(os.environ, _ua_env(), clear=False):
        async with FREDAdapter(client=client) as fred:
            obs = await fred.get_observations("T10Y2Y", start=date(2024, 1, 1))

    # Missing-value row filtered; 3 valid observations.
    assert len(obs) == 3
    assert obs[0] == {"date": date(2024, 1, 2), "value": Decimal("-0.34")}
    assert obs[2] == {"date": date(2024, 1, 5), "value": Decimal("-0.30")}


# ── 2. Empty response ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_observations_empty_payload():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_EMPTY_PAYLOAD)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.stlouisfed.org/fred",
    )
    with patch.dict(os.environ, _ua_env(), clear=False):
        async with FREDAdapter(client=client) as fred:
            obs = await fred.get_observations("SAHMREALTIME")
    assert obs == []


# ── 3. Rate-limit retry ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_observations_retries_on_429():
    call_count = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] < 2:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json=_T10Y2Y_PAYLOAD)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.stlouisfed.org/fred",
    )
    with patch.dict(os.environ, _ua_env(), clear=False), patch("asyncio.sleep"):
        async with FREDAdapter(client=client) as fred:
            obs = await fred.get_observations("T10Y2Y")

    assert call_count["n"] == 2
    assert len(obs) == 3


# ── 4. Permanent failure ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_observations_400_raises_outage_no_retry():
    """An invalid series_id → 400 from FRED — permanent, no retry."""
    call_count = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(400, text='{"error_message":"bad series_id"}')

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.stlouisfed.org/fred",
    )
    with patch.dict(os.environ, _ua_env(), clear=False), patch("asyncio.sleep"):
        async with FREDAdapter(client=client) as fred:
            with pytest.raises(DataProviderOutage):
                await fred.get_observations("BOGUS_SERIES")

    assert call_count["n"] == 1, "400 must not retry"


# ── 5. Missing-value sentinel ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_dot_value_filtered():
    """FRED encodes missing observations as ``"."``; loader must drop them
    before the DB's NOT NULL CHECK constraint sees them."""
    payload = {
        "observations": [
            {"date": "2020-01-01", "value": "."},
            {"date": "2020-01-02", "value": "."},
            {"date": "2020-01-03", "value": "5.50"},
        ],
    }

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.stlouisfed.org/fred",
    )
    with patch.dict(os.environ, _ua_env(), clear=False):
        async with FREDAdapter(client=client) as fred:
            obs = await fred.get_observations("INDPRO")
    assert len(obs) == 1
    assert obs[0]["value"] == Decimal("5.50")


# ── 6. get_all_indicators iterates the five series ─────────────────────


@pytest.mark.asyncio
async def test_get_all_indicators_visits_each_series():
    """Confirms the helper hits every INDICATOR_SERIES entry; doesn't
    short-circuit on a single failure."""
    seen_series: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen_series.append(req.url.params.get("series_id") or "")
        return httpx.Response(200, json=_T10Y2Y_PAYLOAD)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.stlouisfed.org/fred",
    )
    with patch.dict(os.environ, _ua_env(), clear=False), patch("asyncio.sleep"):
        async with FREDAdapter(client=client) as fred:
            results = await fred.get_all_indicators()

    expected_ids = {sid for _, sid in INDICATOR_SERIES}
    assert set(seen_series) == expected_ids
    assert set(results.keys()) == {name for name, _ in INDICATOR_SERIES}


# ── 7. get_all_indicators tolerates per-series failure ─────────────────


@pytest.mark.asyncio
async def test_get_all_indicators_continues_on_single_series_failure():
    """If one series returns 400 (e.g., deprecated), the rest still
    return observations. Bulk run reports the failure but doesn't raise."""
    def handler(req: httpx.Request) -> httpx.Response:
        sid = req.url.params.get("series_id")
        if sid == "INDPRO":
            return httpx.Response(400, text="deprecated")
        return httpx.Response(200, json=_T10Y2Y_PAYLOAD)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.stlouisfed.org/fred",
    )
    with patch.dict(os.environ, _ua_env(), clear=False), patch("asyncio.sleep"):
        async with FREDAdapter(client=client) as fred:
            results = await fred.get_all_indicators()

    assert results["industrial_production"] == []  # INDPRO failed
    assert len(results["yield_curve"]) == 3  # T10Y2Y succeeded


# ── 8. Idempotency ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_observations_idempotent_same_input_same_output():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_T10Y2Y_PAYLOAD)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.stlouisfed.org/fred",
    )
    with patch.dict(os.environ, _ua_env(), clear=False):
        async with FREDAdapter(client=client) as fred:
            obs1 = await fred.get_observations("T10Y2Y")
            obs2 = await fred.get_observations("T10Y2Y")
    assert obs1 == obs2


# ── 9. Config error ────────────────────────────────────────────────────


def test_missing_api_key_raises_fail_fast():
    with patch.dict(os.environ, {}, clear=True), pytest.raises(DataProviderOutage):
        FREDAdapter()


# ── 10. latest_published probe (#165 facet 4 — FRED parallel of AAII) ──


_SERIES_METADATA_PAYLOAD = {
    "seriess": [
        {
            "id": "DGS10",
            "observation_start": "1962-01-02",
            "observation_end": "2026-05-18",
            "last_updated": "2026-05-19 15:16:43-05",
        }
    ],
}


@pytest.mark.asyncio
async def test_latest_published_happy_returns_observation_end():
    """The cheap probe hits ``/fred/series`` (NOT ``/series/observations``)
    and returns the parsed ``observation_end`` date."""
    seen_paths: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen_paths.append(req.url.path)
        return httpx.Response(200, json=_SERIES_METADATA_PAYLOAD)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.stlouisfed.org/fred",
    )
    with patch.dict(os.environ, _ua_env(), clear=False):
        async with FREDAdapter(client=client) as fred:
            d = await fred.latest_published("DGS10")

    assert d == date(2026, 5, 18)
    # Cheap-probe contract: hits the metadata endpoint, NOT observations.
    assert seen_paths == ["/fred/series"]


@pytest.mark.asyncio
async def test_latest_published_missing_observation_end_returns_none():
    """Malformed metadata (no observation_end) ⇒ None ⇒ caller stays
    strict (never silently-green)."""
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"seriess": [{"id": "DGS10"}]})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.stlouisfed.org/fred",
    )
    with patch.dict(os.environ, _ua_env(), clear=False):
        async with FREDAdapter(client=client) as fred:
            assert await fred.latest_published("DGS10") is None


@pytest.mark.asyncio
async def test_latest_published_empty_seriess_returns_none():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"seriess": []})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.stlouisfed.org/fred",
    )
    with patch.dict(os.environ, _ua_env(), clear=False):
        async with FREDAdapter(client=client) as fred:
            assert await fred.latest_published("UNKNOWN_SERIES") is None


@pytest.mark.asyncio
async def test_latest_published_404_returns_none_not_raise():
    """Permanent failure becomes None (probe is best-effort; strict-
    behind is the caller's fallback). Mirrors the AAII probe's
    ``except httpx.HTTPError: return None`` contract."""
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="series_id not found")

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.stlouisfed.org/fred",
    )
    with patch.dict(os.environ, _ua_env(), clear=False):
        async with FREDAdapter(client=client) as fred:
            assert await fred.latest_published("BOGUS") is None
