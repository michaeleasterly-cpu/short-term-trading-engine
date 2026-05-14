"""Tests for ``tpcore.data.batched_fetchers``.

Covers:
- ``fetch_bars_batch`` groups multi-ticker rows correctly + handles empty input
- ``fetch_fundamentals_batch`` produces the expected latest+history shape
- Both fetchers chunk at 500 tickers (verified by inspecting executed SQL count)
- ``with_supabase_recovery`` retries once + raises ``UniverseTooLargeError``
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any
from unittest.mock import patch

import pytest

from tpcore.data.batched_fetchers import (
    fetch_bars_batch,
    fetch_fundamentals_batch,
    with_supabase_recovery,
)
from tpcore.errors import UniverseTooLargeError


class _FakeConn:
    def __init__(self, rows_supplier) -> None:
        self.rows_supplier = rows_supplier
        self.fetch_calls: list[tuple] = []

    async def fetch(self, sql: str, *args: Any) -> list:
        self.fetch_calls.append((sql, args))
        return self.rows_supplier(sql, args)


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self.conn = conn
        self.acquired = 0

    def acquire(self) -> _FakePool:
        self.acquired += 1
        return self

    async def __aenter__(self) -> _FakeConn:
        return self.conn

    async def __aexit__(self, *_: Any) -> None:
        return None


# ─── fetch_bars_batch ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_bars_batch_groups_by_ticker():
    rows = [
        {"ticker": "AAPL", "date": date(2026, 5, 13), "open": 200.0,
         "high": 205.0, "low": 199.0, "close": 204.0, "volume": 1000},
        {"ticker": "AAPL", "date": date(2026, 5, 12), "open": 198.0,
         "high": 202.0, "low": 197.0, "close": 200.0, "volume": 800},
        {"ticker": "MSFT", "date": date(2026, 5, 13), "open": 410.0,
         "high": 412.0, "low": 408.0, "close": 411.0, "volume": 500},
    ]
    pool = _FakePool(_FakeConn(lambda sql, args: rows))

    out = await fetch_bars_batch(
        pool, ["AAPL", "MSFT"], date(2026, 5, 1), date(2026, 5, 13)
    )
    assert sorted(out.keys()) == ["AAPL", "MSFT"]
    assert len(out["AAPL"]) == 2
    assert out["AAPL"][0]["close"] == 204.0
    assert out["MSFT"][0]["volume"] == 500


@pytest.mark.asyncio
async def test_fetch_bars_batch_empty_input_short_circuits():
    pool = _FakePool(_FakeConn(lambda sql, args: []))
    out = await fetch_bars_batch(pool, [], date(2026, 1, 1), date(2026, 5, 13))
    assert out == {}
    assert pool.conn.fetch_calls == []


@pytest.mark.asyncio
async def test_fetch_bars_batch_returns_empty_list_for_tickers_with_no_bars():
    # Caller passes AAPL + MSFT; DB returns rows only for AAPL.
    rows = [
        {"ticker": "AAPL", "date": date(2026, 5, 13), "open": 200.0,
         "high": 205.0, "low": 199.0, "close": 204.0, "volume": 1000},
    ]
    pool = _FakePool(_FakeConn(lambda sql, args: rows))
    out = await fetch_bars_batch(
        pool, ["AAPL", "MSFT"], date(2026, 5, 1), date(2026, 5, 13)
    )
    # MSFT key present with empty list — deterministic iteration.
    assert out["MSFT"] == []


@pytest.mark.asyncio
async def test_fetch_bars_batch_chunks_at_500_tickers():
    """1,200 tickers should produce 3 chunks: 500 + 500 + 200."""
    rows: list[dict] = []
    pool = _FakePool(_FakeConn(lambda sql, args: rows))
    universe = [f"T{i:04d}" for i in range(1200)]
    await fetch_bars_batch(pool, universe, date(2026, 5, 1), date(2026, 5, 13))
    assert len(pool.conn.fetch_calls) == 3
    # First chunk: 500 tickers in ANY()
    assert len(pool.conn.fetch_calls[0][1][0]) == 500
    assert len(pool.conn.fetch_calls[1][1][0]) == 500
    assert len(pool.conn.fetch_calls[2][1][0]) == 200


# ─── fetch_fundamentals_batch ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_fundamentals_batch_latest_plus_history():
    # Two filings for AAPL; the newest goes to "latest", the older to "history".
    rows = [
        {"ticker": "AAPL", "filing_date": date(2026, 2, 1),
         "period_end_date": date(2025, 12, 31), "period_label": "2025Q4",
         "net_income": Decimal("100"), "fcf": None,
         "operating_cash_flow": None, "capex": None,
         "revenue": Decimal("1000"), "total_assets": None,
         "total_liabilities": None, "current_assets": None,
         "current_liabilities": None, "receivables": None,
         "cash_and_equivalents": None, "shares_outstanding": Decimal("16000")},
        {"ticker": "AAPL", "filing_date": date(2025, 11, 1),
         "period_end_date": date(2025, 9, 30), "period_label": "2025Q3",
         "net_income": Decimal("90"), "fcf": None,
         "operating_cash_flow": None, "capex": None,
         "revenue": Decimal("950"), "total_assets": None,
         "total_liabilities": None, "current_assets": None,
         "current_liabilities": None, "receivables": None,
         "cash_and_equivalents": None, "shares_outstanding": Decimal("16100")},
    ]
    pool = _FakePool(_FakeConn(lambda sql, args: rows))
    out = await fetch_fundamentals_batch(pool, ["AAPL"], date(2026, 5, 14))
    aapl = out["AAPL"]
    assert aapl is not None
    assert aapl["period"] == "2025Q4"
    assert len(aapl["history"]) == 1
    assert aapl["history"][0]["period"] == "2025Q3"


@pytest.mark.asyncio
async def test_fetch_fundamentals_batch_missing_ticker_maps_to_none():
    pool = _FakePool(_FakeConn(lambda sql, args: []))
    out = await fetch_fundamentals_batch(pool, ["NOTHING"], date(2026, 5, 14))
    assert out == {"NOTHING": None}


# ─── with_supabase_recovery decorator ───────────────────────────────────


@pytest.mark.asyncio
async def test_recovery_retries_once_on_query_canceled():
    import asyncpg.exceptions

    attempts = {"n": 0}

    @with_supabase_recovery
    async def flaky(tickers: list[str]) -> str:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise asyncpg.exceptions.QueryCanceledError("statement timeout")
        return "ok"

    result = await flaky(["AAPL", "MSFT"])
    assert result == "ok"
    assert attempts["n"] == 2


@pytest.mark.asyncio
async def test_recovery_raises_universe_too_large_after_two_failures():
    import asyncpg.exceptions

    @with_supabase_recovery
    async def always_fail(tickers: list[str]) -> str:
        raise asyncpg.exceptions.QueryCanceledError("statement timeout")

    with patch("asyncio.sleep"):  # speed up the backoff
        with pytest.raises(UniverseTooLargeError) as excinfo:
            await always_fail(["A", "B", "C", "D"])
    assert excinfo.value.ticker_count == 4
    assert excinfo.value.attempt == 2


@pytest.mark.asyncio
async def test_recovery_passes_through_non_timeout_errors():
    @with_supabase_recovery
    async def boom(tickers: list[str]) -> str:
        raise ValueError("not a timeout")

    with pytest.raises(ValueError, match="not a timeout"):
        await boom(["A"])
