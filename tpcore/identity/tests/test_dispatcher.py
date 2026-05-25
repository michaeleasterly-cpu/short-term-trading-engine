"""IdentityDispatcher — ticker ↔ classification_id translation + TTL/LRU cache."""

from __future__ import annotations

import asyncio
from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from tpcore.identity.dispatcher import IdentityDispatcher


def _mock_pool(fetchval_returns) -> MagicMock:
    """Build a mock asyncpg.Pool whose ``acquire()`` yields a connection
    whose ``fetchval`` returns the supplied values in sequence.

    ``fetchval_returns`` is a list of values; each ``fetchval`` call
    pops the next one. Use a single value to repeat indefinitely.
    """
    if not isinstance(fetchval_returns, list):
        fetchval_returns = [fetchval_returns]
    queue = list(fetchval_returns)

    async def _fetchval(*_args, **_kwargs):
        if len(queue) == 1:
            return queue[0]
        return queue.pop(0)

    conn = MagicMock()
    conn.fetchval = AsyncMock(side_effect=_fetchval)

    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=None)

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire_cm)
    pool.conn_for_assertions = conn
    return pool


# ─────────────────────────────────────────────────────────────────
# ticker → classification_id
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ticker_to_cid_latest_returns_value():
    pool = _mock_pool("USOZ22OFB123XX")
    d = IdentityDispatcher(pool)
    result = await d.ticker_to_classification_id("META")
    assert result == "USOZ22OFB123XX"
    sql_used = pool.conn_for_assertions.fetchval.await_args.args[0]
    assert "valid_to IS NULL" in sql_used
    assert pool.conn_for_assertions.fetchval.await_args.args[1:] == ("META",)


@pytest.mark.asyncio
async def test_ticker_to_cid_as_of_passes_date():
    """as_of param routes to the SCD-2 query with the date bound."""
    pool = _mock_pool("USOZ12OFB123XX")
    d = IdentityDispatcher(pool)
    as_of = date(2015, 1, 1)
    result = await d.ticker_to_classification_id("FB", as_of=as_of)
    assert result == "USOZ12OFB123XX"
    sql_used = pool.conn_for_assertions.fetchval.await_args.args[0]
    assert "valid_from <= $2" in sql_used
    assert pool.conn_for_assertions.fetchval.await_args.args[1:] == ("FB", as_of)


@pytest.mark.asyncio
async def test_ticker_to_cid_returns_none_when_unknown():
    """No row in ticker_history → None (not exception)."""
    pool = _mock_pool(None)
    d = IdentityDispatcher(pool)
    result = await d.ticker_to_classification_id("UNKNOWN")
    assert result is None


# ─────────────────────────────────────────────────────────────────
# classification_id → ticker
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cid_to_ticker_latest_returns_current_ticker():
    pool = _mock_pool("META")
    d = IdentityDispatcher(pool)
    result = await d.classification_id_to_ticker("USOZ22OFB123XX")
    assert result == "META"
    sql_used = pool.conn_for_assertions.fetchval.await_args.args[0]
    assert "WHERE classification_id = $1" in sql_used
    assert "valid_to IS NULL" in sql_used


@pytest.mark.asyncio
async def test_cid_to_ticker_as_of_returns_historical_ticker():
    """as_of=<pre-rename date> resolves to the historical ticker (FB, not META)."""
    pool = _mock_pool("FB")
    d = IdentityDispatcher(pool)
    result = await d.classification_id_to_ticker("USOZ12OFB123XX", as_of=date(2015, 1, 1))
    assert result == "FB"
    sql_used = pool.conn_for_assertions.fetchval.await_args.args[0]
    assert "valid_from <= $2" in sql_used


# ─────────────────────────────────────────────────────────────────
# Cache behavior
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cache_hit_skips_db_on_second_call():
    pool = _mock_pool("USOZ22OFB123XX")
    d = IdentityDispatcher(pool)
    await d.ticker_to_classification_id("META")
    await d.ticker_to_classification_id("META")
    assert pool.conn_for_assertions.fetchval.await_count == 1


@pytest.mark.asyncio
async def test_cache_distinguishes_as_of_and_latest():
    """Same ticker, different as_of values are cached separately."""
    pool = _mock_pool(["USOZ22OFB123XX", "USOZ22OFB123XX"])
    d = IdentityDispatcher(pool)
    await d.ticker_to_classification_id("META")
    await d.ticker_to_classification_id("META", as_of=date(2020, 1, 1))
    assert pool.conn_for_assertions.fetchval.await_count == 2


@pytest.mark.asyncio
async def test_cached_none_is_not_re_queried():
    """An unknown ticker that resolved to None is cached as None — second
    call must not hit DB (cached miss is a real cached value)."""
    pool = _mock_pool(None)
    d = IdentityDispatcher(pool)
    r1 = await d.ticker_to_classification_id("UNKNOWN")
    r2 = await d.ticker_to_classification_id("UNKNOWN")
    assert r1 is None and r2 is None
    assert pool.conn_for_assertions.fetchval.await_count == 1


# ─────────────────────────────────────────────────────────────────
# Invalidation
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_invalidate_all_clears_cache():
    pool = _mock_pool(["USOZ22OFB123XX", "USOZ22OFB123XX"])
    d = IdentityDispatcher(pool)
    await d.ticker_to_classification_id("META")
    n = d.invalidate()
    assert n == 1
    await d.ticker_to_classification_id("META")
    assert pool.conn_for_assertions.fetchval.await_count == 2


@pytest.mark.asyncio
async def test_invalidate_by_ticker_drops_only_that_ticker():
    pool = _mock_pool(["A_CID", "B_CID", "A_CID"])
    d = IdentityDispatcher(pool)
    await d.ticker_to_classification_id("AAA")
    await d.ticker_to_classification_id("BBB")
    n = d.invalidate(ticker="AAA")
    assert n == 1
    # BBB still cached
    await d.ticker_to_classification_id("BBB")
    assert pool.conn_for_assertions.fetchval.await_count == 2
    # AAA evicted — re-queries
    await d.ticker_to_classification_id("AAA")
    assert pool.conn_for_assertions.fetchval.await_count == 3


@pytest.mark.asyncio
async def test_invalidate_by_classification_id_drops_only_that_cid():
    pool = _mock_pool(["META", "AAPL", "META"])
    d = IdentityDispatcher(pool)
    await d.classification_id_to_ticker("USOZ22OFB123XX")
    await d.classification_id_to_ticker("USOZ80NAAPL456")
    n = d.invalidate(classification_id="USOZ22OFB123XX")
    assert n == 1
    await d.classification_id_to_ticker("USOZ80NAAPL456")
    assert pool.conn_for_assertions.fetchval.await_count == 2
    await d.classification_id_to_ticker("USOZ22OFB123XX")
    assert pool.conn_for_assertions.fetchval.await_count == 3


# ─────────────────────────────────────────────────────────────────
# TTL + LRU eviction
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ttl_expiry_re_queries():
    """Entry expiry forces a fresh DB lookup."""
    pool = _mock_pool(["V1", "V1"])
    d = IdentityDispatcher(pool, cache_ttl_seconds=0.05)
    await d.ticker_to_classification_id("X")
    await asyncio.sleep(0.1)
    await d.ticker_to_classification_id("X")
    assert pool.conn_for_assertions.fetchval.await_count == 2


@pytest.mark.asyncio
async def test_lru_eviction_drops_oldest_when_max_size_exceeded():
    """When cache exceeds max_size, oldest entry is evicted."""
    pool = _mock_pool(["A", "B", "C", "A"])
    d = IdentityDispatcher(pool, cache_max_size=2)
    await d.ticker_to_classification_id("T1")  # cache: [T1]
    await d.ticker_to_classification_id("T2")  # cache: [T1, T2]
    await d.ticker_to_classification_id("T3")  # cache: [T2, T3] (T1 evicted)
    # T1 evicted → re-queries
    await d.ticker_to_classification_id("T1")
    assert pool.conn_for_assertions.fetchval.await_count == 4
