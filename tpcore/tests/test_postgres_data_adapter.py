"""Tests for ``tpcore.data.postgres_data_adapter.PostgresDataAdapter``.

Exercised against a fake asyncpg pool — same minimum-surface fake used by
``test_persistent_store.py``. No live database required.
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from tpcore.data.postgres_data_adapter import PostgresDataAdapter
from tpcore.interfaces.data import Bar

# ────────────────────────────────────────────────────────────────────────────
# Fake asyncpg pool — minimum surface the adapter touches
# ────────────────────────────────────────────────────────────────────────────


class _FakeConn:
    def __init__(self) -> None:
        self.fetch_result: list[dict] = []
        self.calls: list[tuple[str, tuple]] = []

    async def fetch(self, sql: str, *args) -> list[dict]:
        self.calls.append((sql, args))
        return list(self.fetch_result)


class _FakeAcquireCM:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc) -> None:
        return None


class _FakePool:
    def __init__(self) -> None:
        self.conn = _FakeConn()

    def acquire(self) -> _FakeAcquireCM:
        return _FakeAcquireCM(self.conn)


def _row(d: date, *, o: str, h: str, l: str, c: str, v: int, adj: str | None = None) -> dict:
    """Build a fake asyncpg row matching the prices_daily SELECT projection."""
    return {
        "date": d,
        "open": Decimal(o),
        "high": Decimal(h),
        "low": Decimal(l),
        "close": Decimal(c),
        "volume": v,
        "adjusted_close": Decimal(adj) if adj is not None else None,
    }


# ────────────────────────────────────────────────────────────────────────────
# Construction
# ────────────────────────────────────────────────────────────────────────────


def test_init_rejects_none_pool() -> None:
    """No pool → no fallback. Constructor must refuse, not defer."""
    with pytest.raises(ValueError, match="requires a connection pool"):
        PostgresDataAdapter(pool=None)  # type: ignore[arg-type]


# ────────────────────────────────────────────────────────────────────────────
# get_daily_bars
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_daily_bars_returns_bars_in_order() -> None:
    pool = _FakePool()
    pool.conn.fetch_result = [
        _row(date(2026, 1, 5), o="100.00", h="101.50", l="99.50", c="100.75", v=1_500_000),
        _row(date(2026, 1, 6), o="100.80", h="102.00", l="100.10", c="101.40", v=1_650_000),
    ]
    adapter = PostgresDataAdapter(pool)  # type: ignore[arg-type]

    bars = await adapter.get_daily_bars("AAPL", date(2026, 1, 1), date(2026, 1, 31))

    assert len(bars) == 2
    assert all(isinstance(b, Bar) for b in bars)
    assert bars[0].symbol == "AAPL"
    assert bars[0].ts == datetime(2026, 1, 5, tzinfo=UTC)
    assert bars[0].close == Decimal("100.75")
    assert bars[0].volume == 1_500_000
    assert bars[1].ts == datetime(2026, 1, 6, tzinfo=UTC)

    # SQL was called with (symbol, start, end, as_of=None).
    sql, args = pool.conn.calls[-1]
    assert "platform.prices_daily" in sql
    assert args == ("AAPL", date(2026, 1, 1), date(2026, 1, 31), None)


@pytest.mark.asyncio
async def test_get_daily_bars_empty_for_missing_symbol() -> None:
    pool = _FakePool()
    pool.conn.fetch_result = []
    adapter = PostgresDataAdapter(pool)  # type: ignore[arg-type]

    bars = await adapter.get_daily_bars("NEVERLISTED", date(2026, 1, 1), date(2026, 1, 31))

    assert bars == []


@pytest.mark.asyncio
async def test_get_daily_bars_passes_as_of_to_query() -> None:
    """When as_of is provided, it must reach the query as $4 for the
    ``date <= $4`` point-in-time clamp."""
    pool = _FakePool()
    pool.conn.fetch_result = []
    adapter = PostgresDataAdapter(pool)  # type: ignore[arg-type]

    await adapter.get_daily_bars(
        "MSFT",
        start=date(2025, 1, 1),
        end=date(2026, 12, 31),
        as_of=date(2025, 6, 30),
    )

    _, args = pool.conn.calls[-1]
    assert args == ("MSFT", date(2025, 1, 1), date(2026, 12, 31), date(2025, 6, 30))


@pytest.mark.asyncio
async def test_get_daily_bars_end_optional() -> None:
    """end=None must be passed through — the query's ``$3::date IS NULL`` branch
    treats that as no upper bound."""
    pool = _FakePool()
    pool.conn.fetch_result = []
    adapter = PostgresDataAdapter(pool)  # type: ignore[arg-type]

    await adapter.get_daily_bars("SPY", date(2024, 1, 1))

    _, args = pool.conn.calls[-1]
    assert args == ("SPY", date(2024, 1, 1), None, None)


# ────────────────────────────────────────────────────────────────────────────
# get_universe_symbols / list_active_symbols
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_universe_symbols_returns_distinct_tickers() -> None:
    pool = _FakePool()
    pool.conn.fetch_result = [
        {"ticker": "AAPL"},
        {"ticker": "GOOGL"},
        {"ticker": "MSFT"},
    ]
    adapter = PostgresDataAdapter(pool)  # type: ignore[arg-type]

    symbols = await adapter.get_universe_symbols()

    assert symbols == ["AAPL", "GOOGL", "MSFT"]
    sql, _ = pool.conn.calls[-1]
    assert "DISTINCT ticker" in sql
    assert "delisted = false" in sql
    assert "INTERVAL '90 days'" in sql


@pytest.mark.asyncio
async def test_list_active_symbols_aliases_universe() -> None:
    pool = _FakePool()
    pool.conn.fetch_result = [{"ticker": "AAPL"}, {"ticker": "MSFT"}]
    adapter = PostgresDataAdapter(pool)  # type: ignore[arg-type]

    universe = await adapter.get_universe_symbols()
    pool.conn.calls.clear()

    pool.conn.fetch_result = [{"ticker": "AAPL"}, {"ticker": "MSFT"}]
    active = await adapter.list_active_symbols()

    assert active == universe == ["AAPL", "MSFT"]


# ────────────────────────────────────────────────────────────────────────────
# Stubbed interface methods raise NotImplementedError
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_quote_not_implemented() -> None:
    adapter = PostgresDataAdapter(_FakePool())  # type: ignore[arg-type]
    with pytest.raises(NotImplementedError):
        await adapter.get_quote("AAPL")


@pytest.mark.asyncio
async def test_get_fundamentals_not_implemented() -> None:
    adapter = PostgresDataAdapter(_FakePool())  # type: ignore[arg-type]
    with pytest.raises(NotImplementedError):
        await adapter.get_fundamentals("AAPL")


@pytest.mark.asyncio
async def test_get_earnings_calendar_not_implemented() -> None:
    adapter = PostgresDataAdapter(_FakePool())  # type: ignore[arg-type]
    with pytest.raises(NotImplementedError):
        await adapter.get_earnings_calendar("AAPL", date(2026, 1, 1), date(2026, 12, 31))


@pytest.mark.asyncio
async def test_list_delisted_symbols_not_implemented() -> None:
    adapter = PostgresDataAdapter(_FakePool())  # type: ignore[arg-type]
    with pytest.raises(NotImplementedError):
        await adapter.list_delisted_symbols()
