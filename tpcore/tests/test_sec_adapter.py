"""Tests for ``tpcore.sec.edgar_adapter``.

Per the 5-stage pipeline (docs/superpowers/pipelines/data_adapter_pipeline.md),
every adapter ships with tests covering:

* happy path (200 OK → expected normalized shape)
* empty response (no filings → empty list, not crash)
* rate-limit (429 → retry via @with_retry → eventual success)
* permanent failure (4xx-not-429 → no retry, DataProviderOutage raised)
* malformed response (broken XML → skipped, not propagated)
* idempotency (parse twice → same output)
* config error (missing UA env var → fail-fast at construction)

These tests exercise the adapter against ``httpx.MockTransport`` —
no live SEC calls in CI.
"""
from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from unittest.mock import patch

import httpx
import pytest

from tpcore.outage import DataProviderOutage
from tpcore.sec.edgar_adapter import SECEdgarAdapter

# ── Fixtures ────────────────────────────────────────────────────────────


_TICKER_MAP_PAYLOAD = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 789019, "ticker": "MSFT", "title": "Microsoft Corp"},
}

_SUBMISSIONS_AAPL = {
    "cik": "320193",
    "filings": {
        "recent": {
            "form": ["4", "8-K", "10-Q", "4"],
            "filingDate": ["2026-05-10", "2026-05-09", "2026-05-01", "2026-04-15"],
            "accessionNumber": [
                "0000320193-26-000001",
                "0000320193-26-000002",
                "0000320193-26-000003",
                "0000320193-26-000004",
            ],
            "primaryDocument": [
                "form4.xml",
                "form8k.htm",
                "form10q.htm",
                "form4_v2.xml",
            ],
            "items": ["", "2.02,9.01", "", ""],
        },
    },
}

_FORM4_XML = """<?xml version="1.0"?>
<ownershipDocument>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerName>COOK TIMOTHY D</rptOwnerName>
    </reportingOwnerId>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionCoding>
        <transactionCode>S</transactionCode>
      </transactionCoding>
      <transactionAmounts>
        <transactionShares><value>10000</value></transactionShares>
        <transactionPricePerShare><value>175.25</value></transactionPricePerShare>
      </transactionAmounts>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <transactionCoding>
        <transactionCode>P</transactionCode>
      </transactionCoding>
      <transactionAmounts>
        <transactionShares><value>500</value></transactionShares>
        <transactionPricePerShare><value>175.25</value></transactionPricePerShare>
      </transactionAmounts>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <transactionCoding>
        <transactionCode>M</transactionCode>
      </transactionCoding>
      <transactionAmounts>
        <transactionShares><value>200</value></transactionShares>
        <transactionPricePerShare><value>0</value></transactionPricePerShare>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>
"""


def _ua_env():
    return {"SEC_EDGAR_USER_AGENT": "STE Tests test@example.com"}


# ── 1. Happy path ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_recent_filings_happy_path():
    def handler(req: httpx.Request) -> httpx.Response:
        if "company_tickers.json" in str(req.url):
            return httpx.Response(200, json=_TICKER_MAP_PAYLOAD)
        if "submissions/CIK" in str(req.url):
            return httpx.Response(200, json=_SUBMISSIONS_AAPL)
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with patch.dict(os.environ, _ua_env(), clear=False):
        async with SECEdgarAdapter(client=client) as sec:
            filings = await sec.get_recent_filings(
                "AAPL", forms=("4", "8-K"), since=date(2026, 5, 1),
            )

    assert len(filings) == 2  # form4 on 2026-05-10 + 8-K on 2026-05-09
    forms = {f["form"] for f in filings}
    assert forms == {"4", "8-K"}
    aapl_8k = next(f for f in filings if f["form"] == "8-K")
    assert aapl_8k["items"] == "2.02,9.01"
    assert aapl_8k["filing_date"] == date(2026, 5, 9)


# ── 2. Empty response ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_recent_filings_unknown_ticker_returns_empty():
    def handler(req: httpx.Request) -> httpx.Response:
        if "company_tickers.json" in str(req.url):
            return httpx.Response(200, json=_TICKER_MAP_PAYLOAD)
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with patch.dict(os.environ, _ua_env(), clear=False):
        async with SECEdgarAdapter(client=client) as sec:
            filings = await sec.get_recent_filings("ZZZZ", forms=("4",))
    assert filings == []  # unknown ticker — empty, not raised


# ── 3. Rate-limit retry ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_recent_filings_retries_on_429():
    call_count = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if "company_tickers.json" in str(req.url):
            return httpx.Response(200, json=_TICKER_MAP_PAYLOAD)
        if "submissions/CIK" in str(req.url):
            call_count["n"] += 1
            if call_count["n"] < 2:
                return httpx.Response(429, headers={"Retry-After": "0"})
            return httpx.Response(200, json=_SUBMISSIONS_AAPL)
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with patch.dict(os.environ, _ua_env(), clear=False), patch("asyncio.sleep"):
        async with SECEdgarAdapter(client=client) as sec:
            filings = await sec.get_recent_filings(
                "AAPL", forms=("4",), since=date(2026, 5, 1),
            )

    assert call_count["n"] == 2
    assert len(filings) == 1


# ── 4. Permanent failure ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_recent_filings_403_raises_outage_no_retry():
    call_count = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if "company_tickers.json" in str(req.url):
            return httpx.Response(200, json=_TICKER_MAP_PAYLOAD)
        if "submissions/CIK" in str(req.url):
            call_count["n"] += 1
            return httpx.Response(403, text="forbidden")
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with patch.dict(os.environ, _ua_env(), clear=False), patch("asyncio.sleep"):
        async with SECEdgarAdapter(client=client) as sec:
            with pytest.raises(DataProviderOutage):
                await sec.get_recent_filings("AAPL", forms=("4",))

    assert call_count["n"] == 1, "permanent 403 must not retry"


# ── 5. Malformed response ──────────────────────────────────────────────


def test_parse_form4_handles_malformed_xml_gracefully():
    rows, skipped = SECEdgarAdapter.parse_form4_transactions(
        "<not><valid>xml", "AAPL", date(2026, 5, 10),
    )
    assert rows == []
    assert skipped == 1


def test_parse_form4_extracts_buy_sell_skips_exotic_codes():
    rows, skipped = SECEdgarAdapter.parse_form4_transactions(
        _FORM4_XML, "AAPL", date(2026, 5, 10),
    )
    # Two valid rows (S=SELL + P=BUY), one skipped (M=Exempt conversion).
    assert len(rows) == 2
    assert skipped == 1
    sell = next(r for r in rows if r["transaction_type"] == "SELL")
    buy = next(r for r in rows if r["transaction_type"] == "BUY")
    assert sell["shares"] == 10000
    assert sell["price"] == Decimal("175.25")
    assert sell["value"] == Decimal("1752500.00")
    assert sell["insider_name"] == "COOK TIMOTHY D"
    assert buy["shares"] == 500


# ── 6. Idempotency ─────────────────────────────────────────────────────


def test_parse_form4_idempotent_same_input_same_output():
    r1, s1 = SECEdgarAdapter.parse_form4_transactions(
        _FORM4_XML, "AAPL", date(2026, 5, 10),
    )
    r2, s2 = SECEdgarAdapter.parse_form4_transactions(
        _FORM4_XML, "AAPL", date(2026, 5, 10),
    )
    assert r1 == r2
    assert s1 == s2


# ── 7. 8-K item parsing ────────────────────────────────────────────────


def test_parse_8k_items_handles_csv_and_blank():
    assert SECEdgarAdapter.parse_8k_items("2.02,9.01") == ["2.02", "9.01"]
    assert SECEdgarAdapter.parse_8k_items("Item 5.02") == ["5.02"]
    assert SECEdgarAdapter.parse_8k_items("") == ["OTHER"]
    assert SECEdgarAdapter.parse_8k_items("  ") == ["OTHER"]


# ── 8. Config error ────────────────────────────────────────────────────


def test_missing_user_agent_env_raises_fail_fast():
    with patch.dict(os.environ, {}, clear=True), pytest.raises(DataProviderOutage):
        SECEdgarAdapter()
