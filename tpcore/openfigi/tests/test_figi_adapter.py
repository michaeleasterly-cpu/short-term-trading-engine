"""OpenFIGI adapter — per adapter_readiness.md §5 test contract."""
from __future__ import annotations

import json
import os
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from pydantic import ValidationError

from tpcore.openfigi import OPENFIGI_FIGI_REGEX, OpenFIGIAdapter, OpenFIGIResult
from tpcore.openfigi.figi_adapter import _validate_figi
from tpcore.outage import DataProviderOutage

# ─────────────────────────────────────────────────────────────────
# Configuration / fail-fast — adapter_readiness §3 + §5
# ─────────────────────────────────────────────────────────────────


def test_missing_api_key_raises_at_construction():
    """Per adapter_readiness §3: missing required env var raises DataProviderOutage at __init__."""
    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(DataProviderOutage, match="OPEN_FIGI_API_KEY"):
            OpenFIGIAdapter()


def test_explicit_api_key_overrides_missing_env():
    """An explicit api_key kwarg satisfies the fail-fast check (no construction error)."""
    with patch.dict(os.environ, {}, clear=True):
        # Construction must not raise — that's the assertion.
        OpenFIGIAdapter(api_key="test-key", client=httpx.AsyncClient())


# ─────────────────────────────────────────────────────────────────
# Regex validator — defends against vendor anomaly
# ─────────────────────────────────────────────────────────────────


def test_validate_figi_accepts_valid_per_omg_regex():
    """Sample valid FIGIs from the OMG FIGI 1.2 ontology examples."""
    valid = [
        "BBG000B9XRY4",  # AAPL US Composite (Apple)
        "BBG000BLNNH6",  # IBM example from OpenFIGI docs
        "BBG0013HFJF7",  # generic valid pattern
    ]
    for figi in valid:
        assert _validate_figi(figi) == figi, f"{figi} should validate"


def test_validate_figi_rejects_country_prefix_collisions():
    """The OMG spec forbids BS/BM/GG/GB/VG/GH/KY as positions 1-2 (collides with CUSIP/ISIN namespace)."""
    for bad_prefix in ("BS", "BM", "GG", "GB", "VG", "GH", "KY"):
        bad_figi = bad_prefix + "G000B9XRY4"
        assert _validate_figi(bad_figi) is None, f"{bad_figi} should be rejected"


def test_validate_figi_rejects_vowel_in_consonant_positions():
    """Positions 1-2 must be consonants; vowels are forbidden."""
    for bad in ("BAG000B9XRY4", "AAG000B9XRY4", "EBG000B9XRY4"):
        assert _validate_figi(bad) is None, f"{bad} (vowel in pos 1-2) should be rejected"


def test_validate_figi_rejects_missing_g_at_position_3():
    """Position 3 must be literal 'G'."""
    assert _validate_figi("BBX000B9XRY4") is None


def test_validate_figi_rejects_wrong_length():
    assert _validate_figi("BBG000B9X") is None  # 9 chars
    assert _validate_figi("BBG000B9XRY4XX") is None  # 14 chars


def test_validate_figi_none_passthrough():
    """None in → None out (callers pass through Optional fields)."""
    assert _validate_figi(None) is None


# ─────────────────────────────────────────────────────────────────
# Happy path — MockTransport returning canonical successful response
# ─────────────────────────────────────────────────────────────────


def _mock_response_one_success(ticker: str, composite_figi: str = "BBG000B9XRY4") -> dict[str, Any]:
    """Synthesize the OpenFIGI single-job success shape."""
    return {
        "data": [
            {
                "figi": "BBG001S5N8V8",  # exchange-level (Nasdaq for AAPL)
                "compositeFIGI": composite_figi,
                "shareClassFIGI": "BBG001S5N8V8",
                "name": "APPLE INC",
                "ticker": ticker,
                "exchCode": "US",
                "securityType": "Common Stock",
                "securityType2": "Common Stock",
                "marketSector": "Equity",
            }
        ]
    }


@pytest.mark.asyncio
async def test_map_tickers_happy_path():
    """Single-ticker mapping returns a populated OpenFIGIResult."""
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body == [{"idType": "TICKER", "idValue": "AAPL", "exchCode": "US"}]
        assert request.headers["X-OPENFIGI-APIKEY"] == "test-key"
        return httpx.Response(200, json=[_mock_response_one_success("AAPL")])

    transport = httpx.MockTransport(handler)
    async with OpenFIGIAdapter(
        api_key="test-key",
        client=httpx.AsyncClient(transport=transport),
    ) as adapter:
        results = await adapter.map_tickers(["AAPL"])

    assert len(results) == 1
    r = results[0]
    assert r.ticker == "AAPL"
    assert r.exch_code == "US"
    assert r.composite_figi == "BBG000B9XRY4"
    assert r.figi_not_found is False
    assert r.name == "APPLE INC"
    assert r.security_type == "Common Stock"


@pytest.mark.asyncio
async def test_map_tickers_batches_when_over_100():
    """101 tickers → 2 batches (100 + 1); both POSTed; results concatenated in order."""
    call_count = 0
    batch_sizes: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        body = json.loads(request.content)
        batch_sizes.append(len(body))
        return httpx.Response(
            200, json=[_mock_response_one_success(j["idValue"]) for j in body]
        )

    transport = httpx.MockTransport(handler)
    async with OpenFIGIAdapter(
        api_key="test-key",
        client=httpx.AsyncClient(transport=transport),
    ) as adapter:
        tickers = [f"T{i:03d}" for i in range(101)]
        results = await adapter.map_tickers(tickers)

    assert call_count == 2, f"expected 2 batch POSTs, got {call_count}"
    assert batch_sizes == [100, 1]
    assert len(results) == 101
    assert [r.ticker for r in results] == tickers


@pytest.mark.asyncio
async def test_map_tickers_empty_input_returns_empty():
    """Edge case: empty ticker list → empty result list, no HTTP call."""
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json=[])

    transport = httpx.MockTransport(handler)
    async with OpenFIGIAdapter(
        api_key="test-key",
        client=httpx.AsyncClient(transport=transport),
    ) as adapter:
        results = await adapter.map_tickers([])

    assert results == []
    assert call_count == 0


# ─────────────────────────────────────────────────────────────────
# No-match warning — adapter_readiness §5 (clean no-match, not error)
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_identifier_found_returns_clean_no_match():
    """OpenFIGI returns {warning: 'No identifier found.'} at HTTP 200 — NOT a 404, NOT raised."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"warning": "No identifier found."}])

    transport = httpx.MockTransport(handler)
    async with OpenFIGIAdapter(
        api_key="test-key",
        client=httpx.AsyncClient(transport=transport),
    ) as adapter:
        results = await adapter.map_tickers(["NOTREAL"])

    assert len(results) == 1
    r = results[0]
    assert r.ticker == "NOTREAL"
    assert r.figi_not_found is True
    assert r.composite_figi is None
    assert r.share_class_figi is None
    assert r.exchange_figi is None


# ─────────────────────────────────────────────────────────────────
# No-retry-on-permanent-4xx — adapter_readiness §5
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_403_raises_immediately_no_retry():
    """Per adapter_readiness §1: permanent 4xx fails fast; no retry."""
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(403, text="Forbidden: invalid API key")

    transport = httpx.MockTransport(handler)
    async with OpenFIGIAdapter(
        api_key="bad-key",
        client=httpx.AsyncClient(transport=transport),
    ) as adapter:
        with pytest.raises(DataProviderOutage, match="403"):
            await adapter.map_tickers(["AAPL"])

    assert call_count == 1, f"403 must not retry; got {call_count} calls"


@pytest.mark.asyncio
async def test_400_raises_immediately_no_retry():
    """A bad payload also fails immediately."""
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(400, text="Bad request")

    transport = httpx.MockTransport(handler)
    async with OpenFIGIAdapter(
        api_key="test-key",
        client=httpx.AsyncClient(transport=transport),
    ) as adapter:
        with pytest.raises(DataProviderOutage, match="400"):
            await adapter.map_tickers(["AAPL"])

    assert call_count == 1


# ─────────────────────────────────────────────────────────────────
# Outage mapping for persistent 5xx — adapter_readiness §5
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_persistent_500_maps_to_data_provider_outage():
    """A persistent 5xx (after retry exhaustion) raises DataProviderOutage, not raw HTTPError."""
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(500, text="Internal Server Error")

    transport = httpx.MockTransport(handler)
    # Patch sleep so the test doesn't actually wait through exponential backoff
    with patch("asyncio.sleep") as mock_sleep:
        mock_sleep.return_value = None
        async with OpenFIGIAdapter(
            api_key="test-key",
            client=httpx.AsyncClient(transport=transport),
        ) as adapter:
            with pytest.raises(DataProviderOutage):
                await adapter.map_tickers(["AAPL"])

    # @with_retry default max_attempts=3 → 3 total calls (1 + 2 retries)
    assert call_count >= 2, f"expected retry on 5xx; got {call_count} calls"


# ─────────────────────────────────────────────────────────────────
# Result-count mismatch — vendor-anomaly defense
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_result_count_mismatch_raises_outage():
    """If OpenFIGI returns N results for M jobs, raise rather than silently misalign."""
    def handler(request: httpx.Request) -> httpx.Response:
        # Return only 1 result for 2 jobs — a vendor anomaly
        return httpx.Response(200, json=[_mock_response_one_success("AAPL")])

    transport = httpx.MockTransport(handler)
    async with OpenFIGIAdapter(
        api_key="test-key",
        client=httpx.AsyncClient(transport=transport),
    ) as adapter:
        with pytest.raises(DataProviderOutage, match="result-count mismatch"):
            await adapter.map_tickers(["AAPL", "MSFT"])


# ─────────────────────────────────────────────────────────────────
# Per-job error path — vendor returns {"error": "..."}
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_per_job_error_raises_outage():
    """If OpenFIGI returns {error: ...} for a job, raise DataProviderOutage with the ticker context."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"error": "Server is overloaded"}])

    transport = httpx.MockTransport(handler)
    async with OpenFIGIAdapter(
        api_key="test-key",
        client=httpx.AsyncClient(transport=transport),
    ) as adapter:
        with pytest.raises(DataProviderOutage, match="per-job error"):
            await adapter.map_tickers(["AAPL"])


# ─────────────────────────────────────────────────────────────────
# Malformed FIGI in response — vendor anomaly defense
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_malformed_figi_rejected_silently():
    """If OpenFIGI ever returns a malformed FIGI, the result's composite_figi is None (logged WARN, not raised)."""
    def handler(request: httpx.Request) -> httpx.Response:
        bad = {
            "data": [
                {
                    "figi": "BSG000B9XRY4",  # BS prefix is forbidden per OMG
                    "compositeFIGI": "BSG000B9XRY4",
                    "shareClassFIGI": "BSG000B9XRY4",
                    "name": "BAD",
                    "ticker": "AAPL",
                }
            ]
        }
        return httpx.Response(200, json=[bad])

    transport = httpx.MockTransport(handler)
    async with OpenFIGIAdapter(
        api_key="test-key",
        client=httpx.AsyncClient(transport=transport),
    ) as adapter:
        results = await adapter.map_tickers(["AAPL"])

    assert results[0].composite_figi is None
    assert results[0].share_class_figi is None
    assert results[0].exchange_figi is None
    assert results[0].name == "BAD"  # non-FIGI fields still populate


# ─────────────────────────────────────────────────────────────────
# Result schema invariants (Pydantic v2 frozen / forbid extras)
# ─────────────────────────────────────────────────────────────────


def test_openfigi_result_rejects_extra_fields():
    """Pydantic v2 model_config extra='forbid' guards against silent vendor-shape drift."""
    with pytest.raises(ValidationError):  # ValidationError — kept generic to avoid pydantic import dep
        OpenFIGIResult(
            ticker="AAPL", exch_code="US",
            composite_figi="BBG000B9XRY4",
            unknown_field="surprise",  # type: ignore[call-arg]
        )


def test_openfigi_result_frozen():
    """Frozen = immutable post-construction; protects against accidental mid-pipeline mutation."""
    r = OpenFIGIResult(ticker="AAPL", exch_code="US", composite_figi="BBG000B9XRY4")
    with pytest.raises(ValidationError):
        r.composite_figi = "MUTATED"  # type: ignore[misc]


# ─────────────────────────────────────────────────────────────────
# Regex export — caller can use OPENFIGI_FIGI_REGEX directly
# ─────────────────────────────────────────────────────────────────


def test_openfigi_figi_regex_is_compiled_pattern():
    """OPENFIGI_FIGI_REGEX is exported as a compiled re.Pattern for caller use."""
    import re as _re
    assert isinstance(OPENFIGI_FIGI_REGEX, _re.Pattern)
    assert OPENFIGI_FIGI_REGEX.fullmatch("BBG000B9XRY4") is not None
    assert OPENFIGI_FIGI_REGEX.fullmatch("ZZG000B9XRY4") is not None  # Z is consonant; non-forbidden prefix
