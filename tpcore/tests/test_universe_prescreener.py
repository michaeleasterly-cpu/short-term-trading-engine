"""Tests for ``tpcore.universe.prescreener.prescreen_momentum``."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from tpcore.universe.prescreener import prescreen_momentum


class _FakeConn:
    def __init__(self, fetch_rows: list[dict]) -> None:
        self.fetch_rows = fetch_rows
        self.executed: list[tuple] = []
        self.in_transaction = False

    async def fetch(self, sql: str, *args):
        return self.fetch_rows

    async def executemany(self, sql: str, args_list):
        self.executed.extend(args_list)

    def transaction(self):
        conn = self

        class _Tx:
            async def __aenter__(self_inner):
                conn.in_transaction = True
                return None

            async def __aexit__(self_inner, *exc):
                conn.in_transaction = False
                return None

        return _Tx()


class _FakeAcquireCM:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc) -> None:
        return None


class _FakePool:
    def __init__(self, fetch_rows: list[dict]) -> None:
        self.conn = _FakeConn(fetch_rows)

    def acquire(self) -> _FakeAcquireCM:
        return _FakeAcquireCM(self.conn)


@pytest.mark.asyncio
async def test_prescreen_momentum_keeps_tradeable_t1_t2():
    rows = [
        {"ticker": "AAPL", "tier": 1, "last_close": Decimal("180.00")},
        {"ticker": "MSFT", "tier": 2, "last_close": Decimal("350.00")},
    ]
    pool = _FakePool(rows)
    counters = await prescreen_momentum(pool, date(2026, 5, 13))
    assert counters == {
        "considered": 2,
        "kept": 2,
        "dropped_no_close": 0,
        "dropped_untradeable": 0,
    }
    assert len(pool.conn.executed) == 2
    tickers_written = {row[2] for row in pool.conn.executed}
    assert tickers_written == {"AAPL", "MSFT"}
    # Engine column is always 'momentum' in this populator.
    assert all(row[1] == "momentum" for row in pool.conn.executed)


@pytest.mark.asyncio
async def test_prescreen_momentum_drops_sub_5_dollar_names():
    rows = [
        {"ticker": "PENNY", "tier": 1, "last_close": Decimal("3.00")},
        {"ticker": "AAPL", "tier": 1, "last_close": Decimal("180.00")},
    ]
    pool = _FakePool(rows)
    counters = await prescreen_momentum(pool, date(2026, 5, 13))
    assert counters["kept"] == 1
    assert counters["dropped_untradeable"] == 1
    assert {row[2] for row in pool.conn.executed} == {"AAPL"}


@pytest.mark.asyncio
async def test_prescreen_momentum_drops_warrants_and_special_classes():
    rows = [
        {"ticker": "XBPEW", "tier": 1, "last_close": Decimal("8.00")},  # warrant
        {"ticker": "BRK.B", "tier": 1, "last_close": Decimal("400.00")},  # class B
        {"ticker": "AAPL", "tier": 1, "last_close": Decimal("180.00")},
    ]
    pool = _FakePool(rows)
    counters = await prescreen_momentum(pool, date(2026, 5, 13))
    assert counters["kept"] == 1
    assert counters["dropped_untradeable"] == 2
    assert {row[2] for row in pool.conn.executed} == {"AAPL"}


@pytest.mark.asyncio
async def test_prescreen_momentum_drops_rows_with_no_close():
    rows = [
        {"ticker": "STALE", "tier": 1, "last_close": None},
        {"ticker": "AAPL", "tier": 1, "last_close": Decimal("180.00")},
    ]
    pool = _FakePool(rows)
    counters = await prescreen_momentum(pool, date(2026, 5, 13))
    assert counters["kept"] == 1
    assert counters["dropped_no_close"] == 1


@pytest.mark.asyncio
async def test_prescreen_momentum_empty_universe_no_writes():
    pool = _FakePool([])
    counters = await prescreen_momentum(pool, date(2026, 5, 13))
    assert counters == {
        "considered": 0,
        "kept": 0,
        "dropped_no_close": 0,
        "dropped_untradeable": 0,
    }
    assert pool.conn.executed == []
