"""Tests for the FINRA short-interest adapter + PIT release-date.

Cases: success, empty, rate-limit (429→retry), permanent (4xx→
DataProviderOutage), malformed, config error, and the PIT release-date
derivation (release_date = settlement + dissemination lag, > settle).
httpx.MockTransport — no network.
"""
from __future__ import annotations

from datetime import date

import httpx
import pytest

from tpcore.finra import FinraAdapter
from tpcore.outage import DataProviderOutage

_TOKEN = {"access_token": "tok_abc"}
_DATA = [
    {"symbolCode": "AAPL", "settlementDate": "2025-01-15",
     "currentShortPositionQuantity": "1000000", "daysToCoverQuantity": "2.5"},
    {"symbolCode": "MSFT", "settlementDate": "2025-01-15",
     "currentShortPositionQuantity": "500000", "daysToCoverQuantity": "1.8"},
]


def _adapter(handler) -> FinraAdapter:
    return FinraAdapter(
        client_id="cid", client_secret="sec",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def _route(token_resp, data_resp):
    def h(req: httpx.Request) -> httpx.Response:
        if "oauth2/access_token" in str(req.url):
            return token_resp()
        return data_resp()
    return h


async def test_happy_path() -> None:
    a = _adapter(_route(lambda: httpx.Response(200, json=_TOKEN),
                         lambda: httpx.Response(200, json=_DATA)))
    recs = await a.get_short_interest(since=date(2025, 1, 1))
    assert {r.ticker for r in recs} == {"AAPL", "MSFT"}
    aapl = next(r for r in recs if r.ticker == "AAPL")
    assert aapl.short_position_qty == 1000000
    assert str(aapl.days_to_cover) == "2.5"
    await a.aclose()


async def test_empty() -> None:
    a = _adapter(_route(lambda: httpx.Response(200, json=_TOKEN),
                         lambda: httpx.Response(200, json=[])))
    assert await a.get_short_interest() == []
    await a.aclose()


async def test_data_429_retries() -> None:
    n = {"c": 0}

    def data():
        n["c"] += 1
        if n["c"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={})
        return httpx.Response(200, json=_DATA)
    a = _adapter(_route(lambda: httpx.Response(200, json=_TOKEN), data))
    recs = await a.get_short_interest()
    assert len(recs) == 2 and n["c"] == 2
    await a.aclose()


async def test_token_permanent_failure_is_outage() -> None:
    a = _adapter(_route(lambda: httpx.Response(400, json={"error": "invalid_client"}),
                        lambda: httpx.Response(200, json=_DATA)))
    with pytest.raises(DataProviderOutage):
        await a.get_short_interest()
    await a.aclose()


async def test_malformed_payload_raises_outage() -> None:
    a = _adapter(_route(lambda: httpx.Response(200, json=_TOKEN),
                        lambda: httpx.Response(200, json=[{"symbolCode": "X",
                          "settlementDate": "not-a-date"}])))
    with pytest.raises(DataProviderOutage, match="malformed"):
        await a.get_short_interest()
    await a.aclose()


def test_missing_creds_fails_fast(monkeypatch) -> None:
    monkeypatch.delenv("FINRA_API_CLIENT_ID", raising=False)
    monkeypatch.delenv("FINRA_API_SECRET_KEY", raising=False)
    with pytest.raises(DataProviderOutage, match="FINRA_API_CLIENT_ID"):
        FinraAdapter()


async def test_handler_pit_release_date_after_settlement(monkeypatch) -> None:
    """PIT: release_date must be settlement + dissemination lag (>),
    so backtests filtering release_date ≤ sim_date never look ahead."""
    from tpcore.finra import ShortInterestRecord
    from tpcore.ingestion import handlers

    class _FA:
        async def __aenter__(self): return self
        async def __aexit__(self, *e): return None
        async def get_short_interest(self, since=None):
            return [ShortInterestRecord(ticker="AAPL",
                    settlement_date=date(2025, 1, 15),
                    short_position_qty=1_000_000, days_to_cover=None)]

    captured = {}

    class _Conn:
        async def fetchval(self, *a): return None
        async def fetch(self, sql, *a):
            return [{"ticker": "AAPL"}] if "liquidity_tiers" in sql else []
        async def executemany(self, sql, rows): captured["rows"] = list(rows)

    class _CM:
        async def __aenter__(self): return _Conn()
        async def __aexit__(self, *e): return None

    class _Pool:
        def acquire(self): return _CM()

    monkeypatch.setattr("tpcore.finra.FinraAdapter", _FA)
    monkeypatch.setattr("tpcore.ingestion.csv_archive.write_archive",
                        lambda *a, **k: type("A", (), {"path": "/tmp/x"})())
    n = await handlers.handle_finra_short_interest(_Pool(), {"skip_guard_days": 0})
    assert n == 1
    tk, settle, release, pct, dtc = captured["rows"][0]
    assert settle == date(2025, 1, 15)
    assert release > settle              # PIT lag applied
    assert (release - settle).days >= 9  # ~9 NYSE sessions
    assert pct is None                   # no fundamentals → honest NULL


async def test_offset_pagination_walks_all_pages(monkeypatch) -> None:
    """Regression: the adapter MUST page via offset until a short page.

    The original bug shipped a single unpaginated request — FINRA caps
    at 1000 rows and returns the oldest settlement period first, so only
    one stale period was ever ingested (observed 2026-05-16). Page size
    is shrunk here so 3 pages exercise the loop without 1000 rows.
    """
    import json as _json

    from tpcore.finra import adapter as _ad
    monkeypatch.setattr(_ad, "_PAGE_SIZE", 2)

    def rec(sym: str, sd: str):
        return {"symbolCode": sym, "settlementDate": sd,
                "currentShortPositionQuantity": "100", "daysToCoverQuantity": "1.0"}

    pages = {
        0: [rec("AAA", "2026-04-30"), rec("BBB", "2026-04-30")],   # full → continue
        2: [rec("CCC", "2026-04-15"), rec("DDD", "2026-04-15")],   # full → continue
        4: [rec("EEE", "2026-03-31")],                             # short → stop
    }
    seen_offsets: list[int] = []

    def h(req: httpx.Request) -> httpx.Response:
        if "oauth2/access_token" in str(req.url):
            return httpx.Response(200, json=_TOKEN)
        body = _json.loads(req.content)
        off = int(body.get("offset", 0))
        seen_offsets.append(off)
        return httpx.Response(200, json=pages.get(off, []))

    a = _adapter(h)
    recs = await a.get_short_interest(since=date(2026, 1, 1))
    # All 5 rows across 3 pages, every settlement period present.
    assert {r.ticker for r in recs} == {"AAA", "BBB", "CCC", "DDD", "EEE"}
    assert seen_offsets == [0, 2, 4]
    assert {str(r.settlement_date) for r in recs} == {
        "2026-04-30", "2026-04-15", "2026-03-31"}
    await a.aclose()
