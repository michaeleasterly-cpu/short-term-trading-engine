"""parent_resolver — per-handler-lane dispatch + TKR-14 mint + pin-at-first-resolve."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

from tpcore.identity.parent_resolver import (
    HandlerKind,
    ResolvedClassification,
    ResolveInputs,
    _infer_ipo_venue,
    _is_sec_first,
    _ProfileResult,
    resolve,
)
from tpcore.identity.tkr14 import (
    AssetClass,
    DiscoverySource,
    IPOVenue,
    validate,
)

# ─────────────────────────────────────────────────────────────────
# Lane classification — SEC-first vs FMP-first
# ─────────────────────────────────────────────────────────────────


def test_insider_is_sec_first():
    assert _is_sec_first(HandlerKind.INSIDER)


def test_material_events_is_sec_first():
    assert _is_sec_first(HandlerKind.MATERIAL_EVENTS)


@pytest.mark.parametrize(
    "kind",
    [
        HandlerKind.PRICES,
        HandlerKind.FUNDAMENTALS,
        HandlerKind.PROFILE,
        HandlerKind.SHORT_INTEREST,
        HandlerKind.CORPORATE_ACTIONS,
        HandlerKind.EARNINGS,
        HandlerKind.LIQUIDITY,
        HandlerKind.SPREAD,
        HandlerKind.SOCIAL,
        HandlerKind.OPTIONS,
        HandlerKind.BORROW,
        HandlerKind.OTHER,
    ],
)
def test_fmp_first_lanes(kind):
    assert not _is_sec_first(kind)


# ─────────────────────────────────────────────────────────────────
# Input validation per lane
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sec_lane_requires_cik():
    """SEC-first lanes must have CIK; ticker is recovered via reverse-lookup."""
    inputs = ResolveInputs(ticker=None, cik=None, handler_kind=HandlerKind.INSIDER)
    with pytest.raises(ValueError, match="requires `cik`"):
        await resolve(
            inputs,
            sec_ticker_lookup={},
            fmp_profile_lookup=None,
            openfigi_lookup=None,
        )


@pytest.mark.asyncio
async def test_fmp_lane_requires_ticker():
    """FMP-first lanes must have ticker."""
    inputs = ResolveInputs(ticker=None, cik=None, handler_kind=HandlerKind.PRICES)
    with pytest.raises(ValueError, match="requires `ticker`"):
        await resolve(
            inputs,
            sec_ticker_lookup={},
            fmp_profile_lookup=None,
            openfigi_lookup=None,
        )


# ─────────────────────────────────────────────────────────────────
# IPO-venue inference heuristic
# ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "exchange,expected",
    [
        ("XNYS", IPOVenue.NYSE),
        ("NYSE", IPOVenue.NYSE),
        ("XNAS", IPOVenue.NASDAQ),
        ("NASDAQ", IPOVenue.NASDAQ),
        ("XASE", IPOVenue.AMEX),
        ("AMEX", IPOVenue.AMEX),
        ("BZX", IPOVenue.CBOE_BZX),
        ("CBOE", IPOVenue.CBOE_BZX),
        ("OTC", IPOVenue.OTC),
        ("OTCM", IPOVenue.OTC),
        ("XLON", IPOVenue.FOREIGN_PRIMARY),
        ("XTKS", IPOVenue.FOREIGN_PRIMARY),
        (None, IPOVenue.OTHER),
        ("", IPOVenue.OTHER),
    ],
)
def test_infer_ipo_venue(exchange, expected):
    assert _infer_ipo_venue(exchange) == expected


# ─────────────────────────────────────────────────────────────────
# Full resolve() — FMP-first lane with mocked stubs
# ─────────────────────────────────────────────────────────────────


def _make_profile(**kwargs: Any) -> dict[str, Any]:
    """Build a dict that parent_resolver will coerce into _ProfileResult."""
    default = {
        "country": "US",
        "asset_class": AssetClass.STOCK,
        "exchange": "XNAS",
        "cik": "0000320193",
        "cusip": "037833100",
        "isin": "US0378331005",
        "legal_name": "APPLE INC",
    }
    default.update(kwargs)
    return default


@dataclass
class _FakeOpenFIGIResult:
    """Lightweight stand-in for OpenFIGIResult (the real type imports httpx)."""

    composite_figi: str | None


_NOW = datetime(2026, 5, 23, tzinfo=UTC)


@pytest.mark.asyncio
async def test_resolve_fmp_first_lane_full_pipeline():
    """PRICES handler: ticker → FMP profile → OpenFIGI → TKR-14 minted."""

    async def fmp_lookup(ticker: str) -> dict[str, Any]:
        assert ticker == "AAPL"
        return _make_profile()

    async def openfigi_lookup(tickers: list[str]) -> list[_FakeOpenFIGIResult]:
        assert tickers == ["AAPL"]
        return [_FakeOpenFIGIResult(composite_figi="BBG000B9XRY4")]

    inputs = ResolveInputs(ticker="AAPL", cik=None, handler_kind=HandlerKind.PRICES)
    result = await resolve(
        inputs,
        sec_ticker_lookup={},
        fmp_profile_lookup=fmp_lookup,
        openfigi_lookup=openfigi_lookup,
        now=_NOW,
    )

    assert isinstance(result, ResolvedClassification)
    assert result.ticker == "AAPL"
    assert result.country == "US"
    assert result.asset_class == AssetClass.STOCK
    assert result.ipo_venue == IPOVenue.NASDAQ  # inferred from XNAS exchange
    assert result.discovery_source == DiscoverySource.FMP
    assert result.cik == "0000320193"
    assert result.cusip == "037833100"
    assert result.isin == "US0378331005"
    assert result.figi == "BBG000B9XRY4"
    assert result.legal_name == "APPLE INC"
    assert validate(result.tkr14_id), f"Minted ID must be a valid TKR-14: {result.tkr14_id}"
    assert result.tkr14_id.startswith("US")  # country segment


# ─────────────────────────────────────────────────────────────────
# Full resolve() — SEC-first lane with mocked stubs
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_sec_first_lane_reverse_lookup():
    """INSIDER handler: CIK → SEC reverse-lookup ticker → FMP profile → OpenFIGI."""

    # SEC ticker→CIK map; parent_resolver inverts it for the reverse direction.
    sec_map = {"AAPL": 320193, "MSFT": 789019}

    fmp_calls: list[str] = []
    async def fmp_lookup(ticker: str) -> dict[str, Any]:
        fmp_calls.append(ticker)
        return _make_profile(legal_name="APPLE INC")

    async def openfigi_lookup(tickers: list[str]) -> list[_FakeOpenFIGIResult]:
        return [_FakeOpenFIGIResult(composite_figi="BBG000B9XRY4")]

    inputs = ResolveInputs(ticker=None, cik="320193", handler_kind=HandlerKind.INSIDER)
    result = await resolve(
        inputs,
        sec_ticker_lookup=sec_map,
        fmp_profile_lookup=fmp_lookup,
        openfigi_lookup=openfigi_lookup,
        now=_NOW,
    )

    assert result.ticker == "AAPL", "Reverse-lookup must recover ticker from CIK"
    assert fmp_calls == ["AAPL"], "FMP enrichment called with the reverse-resolved ticker"
    assert result.discovery_source == DiscoverySource.SEC, (
        "Discovery source pinned to SEC for the insider lane"
    )
    assert validate(result.tkr14_id)


@pytest.mark.asyncio
async def test_resolve_sec_first_lane_zero_padded_cik():
    """CIK may arrive as a zero-padded string like '0000320193'; reverse-lookup must still find it."""
    sec_map = {"AAPL": 320193}

    async def fmp_lookup(ticker: str) -> dict[str, Any]:
        return _make_profile()

    async def openfigi_lookup(tickers: list[str]) -> list[_FakeOpenFIGIResult]:
        return [_FakeOpenFIGIResult(composite_figi="BBG000B9XRY4")]

    inputs = ResolveInputs(ticker=None, cik="0000320193", handler_kind=HandlerKind.INSIDER)
    result = await resolve(
        inputs,
        sec_ticker_lookup=sec_map,
        fmp_profile_lookup=fmp_lookup,
        openfigi_lookup=openfigi_lookup,
        now=_NOW,
    )
    assert result.ticker == "AAPL"


@pytest.mark.asyncio
async def test_resolve_sec_first_lane_cik_not_in_sec_with_fallback_ticker():
    """Foreign-issuer CIK SEC doesn't carry → caller provides fallback ticker → resolution proceeds."""
    sec_map = {"AAPL": 320193}  # no entry for our test CIK

    async def fmp_lookup(ticker: str) -> dict[str, Any]:
        return _make_profile(country="JP", exchange="XTKS")

    async def openfigi_lookup(tickers: list[str]) -> list[_FakeOpenFIGIResult]:
        return [_FakeOpenFIGIResult(composite_figi="BBG000XXXXX1")]

    inputs = ResolveInputs(
        ticker="6758",  # fallback ticker provided
        cik="9999999",  # not in SEC map
        handler_kind=HandlerKind.INSIDER,
    )
    result = await resolve(
        inputs,
        sec_ticker_lookup=sec_map,
        fmp_profile_lookup=fmp_lookup,
        openfigi_lookup=openfigi_lookup,
        now=_NOW,
    )
    assert result.ticker == "6758"
    assert result.country == "JP"
    assert result.ipo_venue == IPOVenue.FOREIGN_PRIMARY


@pytest.mark.asyncio
async def test_resolve_sec_first_lane_cik_not_in_sec_no_fallback_raises():
    """SEC reverse-lookup miss + no fallback ticker → ValueError (no way to proceed)."""
    inputs = ResolveInputs(ticker=None, cik="9999999", handler_kind=HandlerKind.INSIDER)
    with pytest.raises(ValueError, match="not found in company_tickers"):
        await resolve(
            inputs,
            sec_ticker_lookup={"AAPL": 320193},
            fmp_profile_lookup=None,
            openfigi_lookup=None,
            now=_NOW,
        )


# ─────────────────────────────────────────────────────────────────
# Partial resolution — figi None when OpenFIGI unavailable
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_figi_none_when_openfigi_returns_empty():
    """When OpenFIGI returns no result, figi is None; classification still minted."""

    async def fmp_lookup(ticker: str) -> dict[str, Any]:
        return _make_profile()

    async def openfigi_lookup(tickers: list[str]) -> list[_FakeOpenFIGIResult]:
        return []  # nothing back

    inputs = ResolveInputs(ticker="AAPL", cik=None, handler_kind=HandlerKind.PRICES)
    result = await resolve(
        inputs,
        sec_ticker_lookup={},
        fmp_profile_lookup=fmp_lookup,
        openfigi_lookup=openfigi_lookup,
        now=_NOW,
    )
    assert result.figi is None
    assert result.tkr14_id  # still minted


@pytest.mark.asyncio
async def test_resolve_openfigi_none_lookup_returns_none_figi():
    """Caller passes openfigi_lookup=None when OpenFIGI is disabled; figi resolves to None."""

    async def fmp_lookup(ticker: str) -> dict[str, Any]:
        return _make_profile()

    inputs = ResolveInputs(ticker="AAPL", cik=None, handler_kind=HandlerKind.PRICES)
    result = await resolve(
        inputs,
        sec_ticker_lookup={},
        fmp_profile_lookup=fmp_lookup,
        openfigi_lookup=None,
        now=_NOW,
    )
    assert result.figi is None
    assert result.tkr14_id


# ─────────────────────────────────────────────────────────────────
# _ProfileResult stub — coercion from dict
# ─────────────────────────────────────────────────────────────────


def test_profile_result_defaults():
    """The stub has US/STOCK defaults so a None-fmp-lookup yields a sane fallback."""
    p = _ProfileResult()
    assert p.country == "US"
    assert p.asset_class == AssetClass.STOCK
    assert p.cik is None
