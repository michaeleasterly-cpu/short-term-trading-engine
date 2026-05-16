"""Tests for the IBorrowDesk adapter + 3-fail-skip-not-crash handler.

Cases: success, empty/unknown (404→None), block (403/429→retry),
malformed, idempotency, and the handler's critical contract: 3
consecutive failures → skip (never crash the pipeline).
"""
from __future__ import annotations

import httpx
import pytest

from tpcore.iborrowdesk import IBorrowDeskAdapter
from tpcore.outage import DataProviderOutage

_OK = {"daily": [
    {"date": "2026-05-14", "fee": 0.25, "available": 1000},
    {"date": "2026-05-15", "fee": 0.30, "available": 900},
]}


def _adapter(handler) -> IBorrowDeskAdapter:
    return IBorrowDeskAdapter(
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://www.iborrowdesk.com", follow_redirects=True))


async def test_happy_latest() -> None:
    a = _adapter(lambda r: httpx.Response(200, json=_OK))
    rec = await a.get_latest_borrow_rate("AAPL")
    assert rec.ticker == "AAPL" and str(rec.date) == "2026-05-15"
    assert str(rec.borrow_rate_pct) == "0.3"   # most-recent by date
    await a.aclose()


async def test_unknown_ticker_404_returns_none() -> None:
    a = _adapter(lambda r: httpx.Response(404, text="not found"))
    assert await a.get_latest_borrow_rate("ZZZZ") is None
    await a.aclose()


async def test_empty_daily_returns_none() -> None:
    a = _adapter(lambda r: httpx.Response(200, json={"daily": []}))
    assert await a.get_latest_borrow_rate("AAPL") is None
    await a.aclose()


async def test_429_ratelimit_retries_then_succeeds() -> None:
    """429 is the genuinely-retryable transient per the canonical
    ``with_retry`` (_is_retryable_status): retry, then succeed."""
    n = {"c": 0}

    def h(req):
        n["c"] += 1
        if n["c"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, text="slow down")
        return httpx.Response(200, json=_OK)
    a = _adapter(h)
    rec = await a.get_latest_borrow_rate("AAPL")
    assert rec is not None and n["c"] == 2
    await a.aclose()


async def test_403_block_is_permanent_outage_no_retry() -> None:
    """403 scrape-block is permanent per the canonical retry contract:
    surfaces as DataProviderOutage WITHOUT retrying (handler counts it
    toward the 3-consecutive-skip)."""
    n = {"c": 0}

    def h(req):
        n["c"] += 1
        return httpx.Response(403, text="blocked")
    a = _adapter(h)
    with pytest.raises(DataProviderOutage):
        await a.get_latest_borrow_rate("AAPL")
    assert n["c"] == 1  # no retry on permanent 403
    await a.aclose()


async def test_malformed_raises_outage() -> None:
    a = _adapter(lambda r: httpx.Response(200, json={"daily": [{"fee": 1}]}))
    with pytest.raises(DataProviderOutage, match="malformed"):
        await a.get_latest_borrow_rate("AAPL")
    await a.aclose()


async def test_handler_three_consecutive_fails_skips_not_crashes(monkeypatch) -> None:
    """Critical contract: 3 consecutive failures → CRITICAL log + skip,
    NEVER raise out of the handler (pipeline must not crash)."""
    from tpcore.ingestion import handlers

    class _FailAdapter:
        async def __aenter__(self): return self
        async def __aexit__(self, *e): return None
        async def get_latest_borrow_rate(self, tk):
            raise DataProviderOutage("iborrowdesk blocked")

    class _Conn:
        async def fetchval(self, *a): return None
        async def fetch(self, sql, *a):
            return [{"ticker": t} for t in ("AAA", "BBB", "CCC", "DDD", "EEE")]
        async def executemany(self, sql, rows): raise AssertionError("no rows expected")

    class _CM:
        async def __aenter__(self): return _Conn()
        async def __aexit__(self, *e): return None

    class _Pool:
        def acquire(self): return _CM()

    monkeypatch.setattr("tpcore.iborrowdesk.IBorrowDeskAdapter", _FailAdapter)
    # must NOT raise — returns 0, pipeline survives
    n = await handlers.handle_iborrowdesk_borrow_rates(_Pool(), {"skip_guard_hours": 0})
    assert n == 0
