"""Tests for ``tpcore.fundamentals.cache.FundamentalsCache``.

Mocks the asyncpg pool + the FMP adapter so the cache hit/miss/upsert
flow is exercised without DB or network. An optional integration test
(gated on ``RUN_DB_INTEGRATION_TESTS``) round-trips against the real
``platform.fundamentals_quarterly`` table.
"""
from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from tpcore.fundamentals.cache import FundamentalsCache
from tpcore.outage import DataProviderOutage

# ────────────────────────────────────────────────────────────────────────────
# Fake asyncpg + fake adapter
# ────────────────────────────────────────────────────────────────────────────


class _FakeConn:
    """Records SQL calls; serves canned fetch results."""

    def __init__(self, rows_by_query: dict[str, list[dict]] | None = None) -> None:
        self.rows_by_query = rows_by_query or {}
        self.fetch_calls: list[tuple[str, tuple]] = []
        self.executemany_calls: list[tuple[str, list[tuple]]] = []
        self.execute_calls: list[tuple[str, tuple]] = []

    async def fetch(self, sql: str, *args) -> list[dict]:
        self.fetch_calls.append((sql, args))
        # Match by a substring of the SQL — good enough for our tests.
        for fragment, rows in self.rows_by_query.items():
            if fragment in sql:
                # Filter by ticker + as_of if relevant.
                if "filing_date <= $2" in sql and len(args) == 2:
                    cutoff = args[1]
                    return [r for r in rows if r["ticker"] == args[0] and r["filing_date"] <= cutoff]
                return [r for r in rows if r["ticker"] == args[0]]
        return []

    async def executemany(self, sql: str, rows: list[tuple]) -> None:
        self.executemany_calls.append((sql, rows))

    async def execute(self, sql: str, *args) -> str:
        self.execute_calls.append((sql, args))
        return "OK"


class _FakeAcquireCM:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc) -> None:
        return None


class _FakePool:
    def __init__(self, conn: _FakeConn | None = None) -> None:
        self.conn = conn or _FakeConn()

    def acquire(self) -> _FakeAcquireCM:
        return _FakeAcquireCM(self.conn)


class _FakeAdapter:
    """Minimum surface: ``async get_quarterly_fundamentals(symbol, as_of_date)``."""

    def __init__(self, payload: dict | None = None) -> None:
        self.calls: list[tuple[str, Any]] = []
        self._payload = payload

    async def get_quarterly_fundamentals(
        self, symbol: str, as_of_date: date | None = None
    ) -> dict:
        self.calls.append((symbol, as_of_date))
        if self._payload is None:
            raise DataProviderOutage(f"no payload for {symbol}")
        return self._payload

    async def aclose(self) -> None:
        return None


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────


def _row(ticker: str, filing_date: date, fcf: float, ni: float, ta: float = 1000.0) -> dict:
    return {
        "ticker": ticker,
        "filing_date": filing_date,
        "period_end_date": filing_date,
        "period_label": "Q4",
        "net_income": Decimal(str(ni)),
        "fcf": Decimal(str(fcf)),
        "operating_cash_flow": Decimal("0"),
        "capex": Decimal("-10"),
        "revenue": Decimal("500"),
        "total_assets": Decimal(str(ta)),
        "total_liabilities": Decimal("400"),
        "current_assets": Decimal("300"),
        "current_liabilities": Decimal("200"),
        "receivables": Decimal("50"),
        "cash_and_equivalents": Decimal("100"),
        "shares_outstanding": Decimal("1000000000"),
    }


def _adapter_payload(filing: date, ni: float, fcf: float) -> dict:
    return {
        "symbol": "AAPL",
        "period": "Q4",
        "period_end_date": filing,
        "filing_date": filing,
        "net_income": Decimal(str(ni)),
        "revenue": Decimal("500"),
        "shares_outstanding": Decimal("1000000000"),
        "fcf": Decimal(str(fcf)),
        "operating_cash_flow": Decimal("0"),
        "capex": Decimal("-10"),
        "total_assets": Decimal("1000"),
        "total_liabilities": Decimal("400"),
        "current_assets": Decimal("300"),
        "current_liabilities": Decimal("200"),
        "receivables": Decimal("50"),
        "cash_and_equivalents": Decimal("100"),
        "history": [],
    }


# ────────────────────────────────────────────────────────────────────────────
# Cache hit / miss / upsert
# ────────────────────────────────────────────────────────────────────────────


async def test_cache_hit_skips_adapter() -> None:
    conn = _FakeConn(
        rows_by_query={
            "FROM platform.fundamentals_quarterly": [
                _row("AAPL", date(2025, 10, 31), fcf=95, ni=100),
                _row("AAPL", date(2025, 7, 30), fcf=90, ni=92),
            ],
        }
    )
    adapter = _FakeAdapter(payload=None)  # Would raise if called.
    cache = FundamentalsCache(_FakePool(conn), adapter=adapter)
    payload = await cache.get_quarterly_fundamentals("AAPL")
    assert payload["filing_date"] == date(2025, 10, 31)
    assert payload["fcf"] == Decimal("95")
    assert len(payload["history"]) == 1
    assert adapter.calls == [], "cache hit must not call FMP"


async def test_cache_miss_falls_through_to_adapter_and_upserts() -> None:
    conn = _FakeConn(rows_by_query={})  # empty cache
    adapter = _FakeAdapter(payload=_adapter_payload(date(2025, 10, 31), ni=100, fcf=95))
    cache = FundamentalsCache(_FakePool(conn), adapter=adapter)

    # First call: cache miss → adapter call → upsert.
    # We need the readback to find the row, so seed the conn AFTER upsert.
    captured_rows: list[tuple] = []

    original_executemany = conn.executemany

    async def patched_executemany(sql: str, rows: list[tuple]) -> None:
        captured_rows.extend(rows)
        # Insert into the fake's rows_by_query so the readback succeeds.
        for r in rows:
            ticker, filing_date = r[0], r[1]
            existing = conn.rows_by_query.setdefault(
                "FROM platform.fundamentals_quarterly", []
            )
            existing.append(
                _row(ticker, filing_date, fcf=float(r[5]), ni=float(r[4]))
            )
        await original_executemany(sql, rows)

    conn.executemany = patched_executemany  # type: ignore[assignment]

    payload = await cache.get_quarterly_fundamentals("AAPL")
    assert adapter.calls == [("AAPL", None)]
    assert payload["filing_date"] == date(2025, 10, 31)
    assert len(captured_rows) == 1


async def test_pit_query_filters_by_filing_date() -> None:
    conn = _FakeConn(
        rows_by_query={
            "FROM platform.fundamentals_quarterly": [
                _row("AAPL", date(2025, 10, 31), fcf=95, ni=100),
                _row("AAPL", date(2025, 7, 30), fcf=90, ni=92),
                _row("AAPL", date(2025, 4, 28), fcf=85, ni=88),
            ],
        }
    )
    adapter = _FakeAdapter(payload=None)
    cache = FundamentalsCache(_FakePool(conn), adapter=adapter)
    payload = await cache.get_quarterly_fundamentals("AAPL", as_of_date=date(2025, 8, 1))
    # Should pick the July filing, not October.
    assert payload["filing_date"] == date(2025, 7, 30)
    assert payload["fcf"] == Decimal("90")
    assert adapter.calls == []


async def test_read_only_mode_raises_outage_on_miss() -> None:
    """No adapter wired → cache miss raises ``DataProviderOutage``."""
    cache = FundamentalsCache(_FakePool(_FakeConn()), adapter=None)
    with pytest.raises(DataProviderOutage):
        await cache.get_quarterly_fundamentals("AAPL")


async def test_backfill_requires_adapter() -> None:
    cache = FundamentalsCache(_FakePool(_FakeConn()), adapter=None)
    with pytest.raises(DataProviderOutage):
        await cache.backfill("AAPL")


async def test_backfill_upserts_payload() -> None:
    conn = _FakeConn()
    adapter = _FakeAdapter(payload=_adapter_payload(date(2025, 10, 31), ni=100, fcf=95))
    cache = FundamentalsCache(_FakePool(conn), adapter=adapter)
    n = await cache.backfill("AAPL")
    assert n == 1
    assert len(conn.executemany_calls) == 1


# ────────────────────────────────────────────────────────────────────────────
# Optional live integration
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.skipif(
    not os.environ.get("RUN_DB_INTEGRATION_TESTS"),
    reason="RUN_DB_INTEGRATION_TESTS not set",
)
async def test_cache_integration_roundtrip() -> None:
    """Real DB roundtrip — populates a test row, reads it back, cleans up."""
    from tpcore.db import build_asyncpg_pool

    pool = await build_asyncpg_pool(os.environ["DATABASE_URL"])
    test_ticker = "ZZZTEST"
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM platform.fundamentals_quarterly WHERE ticker=$1", test_ticker
            )
        adapter = _FakeAdapter(
            payload=_adapter_payload(date(2025, 10, 31), ni=100, fcf=95)
        )
        # Adapter payload uses symbol=AAPL by default; rebuild for our test ticker.
        adapter._payload["symbol"] = test_ticker  # type: ignore[index]
        cache = FundamentalsCache(pool, adapter=adapter)

        n = await cache.backfill(test_ticker)
        assert n == 1
        # Subsequent get() should hit the cache (no adapter call).
        before_calls = len(adapter.calls)
        payload = await cache.get_quarterly_fundamentals(test_ticker)
        assert payload["filing_date"] == date(2025, 10, 31)
        assert len(adapter.calls) == before_calls, "should be cache hit"
    finally:
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM platform.fundamentals_quarterly WHERE ticker=$1", test_ticker
            )
        await pool.close()
