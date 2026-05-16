"""Tests for the ApeWisdom adapter + handler T1/T2 filter.

Cases per task: success, empty, pagination, malformed, idempotency,
local T1+T2 filtering. httpx.MockTransport — no network.
"""
from __future__ import annotations

import httpx
import pytest

from tpcore.apewisdom import ApeWisdomAdapter
from tpcore.outage import DataProviderOutage


def _rec(rank, tk):
    return {"rank": rank, "ticker": tk, "name": tk, "mentions": 100 - rank,
            "upvotes": 200 - rank, "rank_24h_ago": rank + 1,
            "mentions_24h_ago": 90 - rank}


def _adapter(handler) -> ApeWisdomAdapter:
    return ApeWisdomAdapter(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                 base_url="https://apewisdom.io/api/v1.0"))


async def test_happy_single_page() -> None:
    a = _adapter(lambda r: httpx.Response(200, json={
        "count": 2, "pages": 1, "current_page": 1,
        "results": [_rec(1, "MU"), _rec(2, "MSFT")]}))
    recs = await a.get_all_sentiment()
    assert [x.ticker for x in recs] == ["MU", "MSFT"]
    assert recs[0].mentions == 99 and recs[0].rank_24h_ago == 2
    await a.aclose()


async def test_pagination_walks_all_pages() -> None:
    def h(req: httpx.Request) -> httpx.Response:
        pg = int(req.url.params.get("page", 1))
        return httpx.Response(200, json={
            "count": 6, "pages": 3, "current_page": pg,
            "results": [_rec(pg * 10 + 1, f"T{pg}A"), _rec(pg * 10 + 2, f"T{pg}B")]})
    a = _adapter(h)
    recs = await a.get_all_sentiment()
    assert len(recs) == 6
    assert {x.ticker for x in recs} == {"T1A", "T1B", "T2A", "T2B", "T3A", "T3B"}
    await a.aclose()


async def test_empty_results() -> None:
    a = _adapter(lambda r: httpx.Response(200, json={
        "count": 0, "pages": 1, "current_page": 1, "results": []}))
    assert await a.get_all_sentiment() == []
    await a.aclose()


async def test_malformed_payload_raises_outage() -> None:
    a = _adapter(lambda r: httpx.Response(200, json={
        "pages": 1, "results": [{"ticker": "MU"}]}))  # missing rank/mentions
    with pytest.raises(DataProviderOutage, match="malformed"):
        await a.get_all_sentiment()
    await a.aclose()


async def test_429_retries_then_succeeds() -> None:
    n = {"c": 0}

    def h(req: httpx.Request) -> httpx.Response:
        n["c"] += 1
        if n["c"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={})
        return httpx.Response(200, json={
            "count": 1, "pages": 1, "current_page": 1, "results": [_rec(1, "MU")]})
    a = _adapter(h)
    recs = await a.get_all_sentiment()
    assert len(recs) == 1 and n["c"] == 2
    await a.aclose()


# ── handler: local T1/T2 filter + idempotency ──────────────────────────

class _Conn:
    def __init__(self, sink): self._sink = sink
    async def fetch(self, sql, *a):
        return [{"ticker": "MU"}, {"ticker": "MSFT"}]  # T1/T2 universe
    async def fetchval(self, sql, *a):
        return None  # skip-guard: no prior rows
    async def executemany(self, sql, rows):
        self._sink.append((sql, list(rows)))


class _CM:
    def __init__(self, s): self._c = _Conn(s)
    async def __aenter__(self): return self._c
    async def __aexit__(self, *e): return None


class _Pool:
    def __init__(self): self.sink = []
    def acquire(self): return _CM(self.sink)


async def test_handler_filters_to_t1_t2_and_is_idempotent(monkeypatch) -> None:
    from tpcore.ingestion import handlers

    class _FakeAdapter:
        async def __aenter__(self): return self
        async def __aexit__(self, *e): return None
        async def get_all_sentiment(self):
            from tpcore.apewisdom import SocialSentimentRecord
            return [
                SocialSentimentRecord(ticker="MU", name="Micron", rank=1,
                                      mentions=10, upvotes=20,
                                      rank_24h_ago=2, mentions_24h_ago=8),
                SocialSentimentRecord(ticker="GME", name="GameStop", rank=2,
                                      mentions=5, upvotes=9,
                                      rank_24h_ago=1, mentions_24h_ago=7),
            ]
    monkeypatch.setattr(handlers, "ApeWisdomAdapter", _FakeAdapter, raising=False)
    monkeypatch.setattr("tpcore.apewisdom.ApeWisdomAdapter", _FakeAdapter)
    monkeypatch.setattr("tpcore.ingestion.csv_archive.write_archive",
                        lambda *a, **k: type("A", (), {"path": "/tmp/x", "rows_written": 0})())
    pool = _Pool()
    n = await handlers.handle_apewisdom_social_sentiment(pool, {"skip_guard_hours": 0})
    # GME not in T1/T2 universe (MU, MSFT) → filtered out; only MU upserted
    assert n == 1
    sql, rows = pool.sink[0]
    assert "ON CONFLICT (ticker, date) DO NOTHING" in sql  # idempotent
    assert [r[0] for r in rows] == ["MU"]
