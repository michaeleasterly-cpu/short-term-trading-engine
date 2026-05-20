"""Tests for ``AlpacaDataAdapter.latest_published`` — the
publication-availability probe (#165 facet 4 — Alpaca parallel of the
AAII + FRED precedents).

Stubs the alpaca-py ``StockHistoricalDataClient.get_stock_latest_bar``
via the ``_client`` injection seam — no network. Pinned cases:

* Happy path → returns the bar's ``timestamp.date()``
* Empty/missing symbol in response → ``None``
* SDK raises (e.g. 403/auth) → ``None`` (probe must never raise; caller
  stays strict)
* Probe uses the IEX feed (the SIP feed 403s the latest-bar endpoint on
  the Algo Trader Plus tier — historical SIP queries work fine, but
  this specific endpoint requires IEX)
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import MagicMock

import pytest

from tpcore.alpaca.data_adapter import AlpacaDataAdapter


class _FakeBar:
    """Stand-in for an alpaca-py Bar — only `.timestamp` is read by the
    probe."""

    def __init__(self, timestamp: datetime) -> None:
        self.timestamp = timestamp


def _make_adapter(stub_client: object) -> AlpacaDataAdapter:
    """Inject a stub client through the ``_client`` seam — bypasses
    the env-var key check by passing dummies."""
    return AlpacaDataAdapter(
        api_key="test-key", api_secret="test-secret",
        _client=stub_client,
    )


@pytest.mark.asyncio
async def test_latest_published_happy_returns_session_date():
    """A valid SPY response → the probe returns the bar's session
    date (the ``date()`` of the SDK's tz-aware ``timestamp``)."""
    bar = _FakeBar(datetime(2026, 5, 19, 20, 10, tzinfo=UTC))
    stub = MagicMock()
    stub.get_stock_latest_bar.return_value = {"SPY": bar}

    adapter = _make_adapter(stub)
    d = await adapter.latest_published("SPY")

    assert d == date(2026, 5, 19)
    # Cheap-probe contract: the latest-bar endpoint was used.
    assert stub.get_stock_latest_bar.called


@pytest.mark.asyncio
async def test_latest_published_default_symbol_is_spy():
    """Calling the probe with no symbol arg uses SPY — the canonical
    liquid anchor declared in the adapter signature default."""
    bar = _FakeBar(datetime(2026, 5, 19, 20, 10, tzinfo=UTC))
    stub = MagicMock()
    stub.get_stock_latest_bar.return_value = {"SPY": bar}

    adapter = _make_adapter(stub)
    assert await adapter.latest_published() == date(2026, 5, 19)

    # Inspect the request the SDK was called with — must be SPY + IEX.
    req = stub.get_stock_latest_bar.call_args.args[0]
    assert req.symbol_or_symbols == "SPY"
    assert req.feed == "iex", (
        "Probe MUST use the IEX feed — the Algo Trader Plus tier 403s "
        "the latest-bar endpoint on SIP ('subscription does not permit "
        "querying recent SIP data'). Using SIP here would silently "
        "leave the probe returning None in production despite working "
        "credentials."
    )


@pytest.mark.asyncio
async def test_latest_published_missing_symbol_returns_none():
    """SDK returns an empty/missing-symbol map → None ⇒ caller stays
    strict (never silently-green on an undeterminable signal)."""
    stub = MagicMock()
    stub.get_stock_latest_bar.return_value = {}

    adapter = _make_adapter(stub)
    assert await adapter.latest_published("SPY") is None


@pytest.mark.asyncio
async def test_latest_published_missing_timestamp_returns_none():
    """Bar present but ``timestamp`` is None (malformed SDK response) →
    None."""
    bar = _FakeBar(None)  # type: ignore[arg-type]
    stub = MagicMock()
    stub.get_stock_latest_bar.return_value = {"SPY": bar}

    adapter = _make_adapter(stub)
    assert await adapter.latest_published("SPY") is None


@pytest.mark.asyncio
async def test_latest_published_sdk_raises_returns_none_not_raise():
    """The SDK raising (auth error, 403, transient network) MUST NOT
    bubble — the probe is best-effort; strict-behind is the caller's
    fallback. Mirrors the AAII + FRED probes' ``except ... return
    None`` contract."""
    stub = MagicMock()
    stub.get_stock_latest_bar.side_effect = RuntimeError("403 Forbidden")

    adapter = _make_adapter(stub)
    assert await adapter.latest_published("SPY") is None


@pytest.mark.asyncio
async def test_latest_published_alternate_symbol_passes_through():
    """The probe accepts a non-default symbol (for tests / future
    multi-anchor setups). The symbol arg flows to the request."""
    bar = _FakeBar(datetime(2026, 5, 19, 20, 10, tzinfo=UTC))
    stub = MagicMock()
    stub.get_stock_latest_bar.return_value = {"AAPL": bar}

    adapter = _make_adapter(stub)
    d = await adapter.latest_published("AAPL")
    assert d == date(2026, 5, 19)
    req = stub.get_stock_latest_bar.call_args.args[0]
    assert req.symbol_or_symbols == "AAPL"
