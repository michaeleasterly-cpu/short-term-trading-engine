"""Tests for ``tpcore.data.postgres_data_adapter.PostgresDataAdapter``.

PR-14 (2026-05-25): the adapter now reads through PricesRepo + UniverseRepo
+ IdentityDispatcher. The fake pool routes asyncpg calls by SQL substring
because a single adapter call now triggers 2+ queries (dispatcher fetchval
+ repo fetch) instead of one.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from tpcore.data.postgres_data_adapter import PostgresDataAdapter
from tpcore.interfaces.data import Bar

# ────────────────────────────────────────────────────────────────────────────
# Fake asyncpg pool — routes by SQL substring (same idea as test_snapshot_
# assembler.py's _FakeConn).
# ────────────────────────────────────────────────────────────────────────────


class _FakeConn:
    """Records SQL calls; routes fetch/fetchval by SQL substring.

    Tests configure ``script`` — a list of (substring, result) entries
    consulted in order. The first matching substring wins. Default fallback
    is empty list / None for fetch / fetchval.
    """

    def __init__(self, pool: _FakePool) -> None:
        self._pool = pool
        self.calls: list[tuple[str, str, tuple]] = []

    async def fetch(self, sql: str, *args) -> list[dict]:
        self.calls.append(("fetch", sql, args))
        return self._pool.route("fetch", sql) or []

    async def fetchval(self, sql: str, *args) -> object:
        self.calls.append(("fetchval", sql, args))
        return self._pool.route("fetchval", sql)


class _FakeAcquireCM:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *_exc) -> None:
        return None


class _FakePool:
    """Pool that routes per-call by SQL substring.

    Each test sets entries on ``script[<method>]`` — a list of
    ``(substring, result_or_callable)`` tuples. The first substring that
    matches the SQL wins. Callable results are invoked with no args (for
    test-side bookkeeping); other values are returned as-is.
    """

    def __init__(self) -> None:
        self.conn = _FakeConn(self)
        self.script: dict[str, list[tuple[str, object]]] = {
            "fetch": [],
            "fetchval": [],
        }

    def route(self, method: str, sql: str) -> object:
        for substring, result in self.script[method]:
            if substring in sql:
                return result() if callable(result) else result
        return None if method == "fetchval" else []

    def add_fetch(self, substring: str, rows: list[dict]) -> None:
        self.script["fetch"].append((substring, rows))

    def add_fetchval(self, substring: str, value: object) -> None:
        self.script["fetchval"].append((substring, value))

    def acquire(self) -> _FakeAcquireCM:
        return _FakeAcquireCM(self.conn)


def _bar_row(d: date, *, o: str, h: str, l: str, c: str, v: int) -> dict:
    """Match PricesRepo's _BARS_SINGLE_SQL projection (no adjusted_close)."""
    return {
        "date": d,
        "open": Decimal(o),
        "high": Decimal(h),
        "low": Decimal(l),
        "close": Decimal(c),
        "volume": v,
    }


def _universe_row(
    *,
    cid: str,
    ticker: str,
    tier: int | None = 2,
    asset_class: str = "stock",
) -> dict:
    """Match the v_universe SELECT projection that UniverseRepo reads."""
    return {
        "classification_id": cid,
        "ticker_at_date": ticker,
        "current_ticker": ticker,
        "asset_class": asset_class,
        "country": "US",
        "status": "active",
        "liquidity_tier": tier,
        "valid_from": date(2020, 1, 1),
        "valid_to": None,
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
    pool.add_fetchval("ticker_history", "AAPL_CID")  # dispatcher: ticker → cid
    pool.add_fetch(
        "platform.prices_daily",
        [
            _bar_row(date(2026, 1, 5), o="100.00", h="101.50", l="99.50", c="100.75", v=1_500_000),
            _bar_row(date(2026, 1, 6), o="100.80", h="102.00", l="100.10", c="101.40", v=1_650_000),
        ],
    )
    adapter = PostgresDataAdapter(pool)  # type: ignore[arg-type]

    bars = await adapter.get_daily_bars("AAPL", date(2026, 1, 1), date(2026, 1, 31))

    assert len(bars) == 2
    assert all(isinstance(b, Bar) for b in bars)
    assert bars[0].symbol == "AAPL"
    assert bars[0].ts == datetime(2026, 1, 5, tzinfo=UTC)
    assert bars[0].close == Decimal("100.75")
    assert bars[0].volume == 1_500_000
    assert bars[1].ts == datetime(2026, 1, 6, tzinfo=UTC)
    # adjusted_close falls back to close in PR-14 (PricesRepo is OHLCV-only).
    assert bars[0].adjusted_close == Decimal("100.75")


@pytest.mark.asyncio
async def test_get_daily_bars_empty_when_ticker_not_in_history() -> None:
    """No cid resolution → return [] without hitting prices_daily."""
    pool = _FakePool()
    # No fetchval entry → router returns None → dispatcher resolves to None.
    adapter = PostgresDataAdapter(pool)  # type: ignore[arg-type]

    bars = await adapter.get_daily_bars("NEVERLISTED", date(2026, 1, 1), date(2026, 1, 31))

    assert bars == []
    # No prices_daily fetch should have happened.
    assert not any("platform.prices_daily" in c[1] for c in pool.conn.calls)


@pytest.mark.asyncio
async def test_get_daily_bars_empty_for_cid_with_no_bars() -> None:
    """cid resolves but window empty → empty bar list."""
    pool = _FakePool()
    pool.add_fetchval("ticker_history", "MSFT_CID")
    pool.add_fetch("platform.prices_daily", [])
    adapter = PostgresDataAdapter(pool)  # type: ignore[arg-type]

    bars = await adapter.get_daily_bars("MSFT", date(2026, 1, 1), date(2026, 1, 31))

    assert bars == []


@pytest.mark.asyncio
async def test_get_daily_bars_uses_most_restrictive_upper_bound() -> None:
    """end + as_of both set → repo upper bound is min(end, as_of)."""
    pool = _FakePool()
    pool.add_fetchval("ticker_history", "MSFT_CID")
    pool.add_fetch("platform.prices_daily", [])
    adapter = PostgresDataAdapter(pool)  # type: ignore[arg-type]

    await adapter.get_daily_bars(
        "MSFT",
        start=date(2025, 1, 1),
        end=date(2026, 12, 31),
        as_of=date(2025, 6, 30),
    )

    # PricesRepo.get_window binds (cid, start, end). Last fetch is the bars
    # query; the third positional arg is the upper bound — must be the more
    # restrictive of (end, as_of) = as_of.
    bars_call = next(c for c in pool.conn.calls if "platform.prices_daily" in c[1])
    args = bars_call[2]
    assert args == ("MSFT_CID", date(2025, 1, 1), date(2025, 6, 30))


@pytest.mark.asyncio
async def test_get_daily_bars_end_none_and_as_of_none_uses_sentinel() -> None:
    """Both upper bounds None → repo gets 9999-12-31 sentinel (no upper)."""
    pool = _FakePool()
    pool.add_fetchval("ticker_history", "SPY_CID")
    pool.add_fetch("platform.prices_daily", [])
    adapter = PostgresDataAdapter(pool)  # type: ignore[arg-type]

    await adapter.get_daily_bars("SPY", date(2024, 1, 1))

    bars_call = next(c for c in pool.conn.calls if "platform.prices_daily" in c[1])
    args = bars_call[2]
    assert args == ("SPY_CID", date(2024, 1, 1), date(9999, 12, 31))


# ────────────────────────────────────────────────────────────────────────────
# get_universe_symbols / list_active_symbols
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_universe_symbols_returns_distinct_tickers() -> None:
    """Direct query preserved — freshness + delisted filter is PR-14
    out of scope; UniverseRepo doesn't have an analog."""
    pool = _FakePool()
    pool.add_fetch(
        "FROM platform.prices_daily",
        [{"ticker": "AAPL"}, {"ticker": "GOOGL"}, {"ticker": "MSFT"}],
    )
    adapter = PostgresDataAdapter(pool)  # type: ignore[arg-type]

    symbols = await adapter.get_universe_symbols()

    assert symbols == ["AAPL", "GOOGL", "MSFT"]
    sql = next(c[1] for c in pool.conn.calls if "DISTINCT ticker" in c[1])
    assert "delisted = false" in sql
    assert "INTERVAL '90 days'" in sql


# ────────────────────────────────────────────────────────────────────────────
# get_universe_by_liquidity_tier — now goes through UniverseRepo
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_universe_by_liquidity_tier_basic() -> None:
    pool = _FakePool()
    pool.add_fetch(
        "platform.v_universe",
        [
            _universe_row(cid="AAPL_CID", ticker="AAPL", tier=1),
            _universe_row(cid="MSFT_CID", ticker="MSFT", tier=2),
        ],
    )
    adapter = PostgresDataAdapter(pool)  # type: ignore[arg-type]

    universe = await adapter.get_universe_by_liquidity_tier(max_tier=2)

    assert universe == ["AAPL", "MSFT"]
    sql, args = next((c[1], c[2]) for c in pool.conn.calls if "platform.v_universe" in c[1])
    assert "liquidity_tier <= $1" in sql
    assert args == (2,)


@pytest.mark.asyncio
async def test_get_universe_by_liquidity_tier_with_asset_class_filter() -> None:
    """asset_class kwarg routes through UniverseRepo's asset_class filter."""
    pool = _FakePool()
    pool.add_fetch(
        "platform.v_universe",
        [
            _universe_row(cid="AAPL_CID", ticker="AAPL", tier=1, asset_class="stock"),
            _universe_row(cid="BAC_CID", ticker="BAC", tier=2, asset_class="stock"),
        ],
    )
    adapter = PostgresDataAdapter(pool)  # type: ignore[arg-type]

    universe = await adapter.get_universe_by_liquidity_tier(
        max_tier=3,
        asset_class="stock",
    )

    assert universe == ["AAPL", "BAC"]
    sql, args = next((c[1], c[2]) for c in pool.conn.calls if "platform.v_universe" in c[1])
    assert "asset_class = $2" in sql
    assert args == (3, "stock")


@pytest.mark.asyncio
async def test_get_universe_by_liquidity_tier_with_fundamentals_filter() -> None:
    """require_fundamentals intersects with FundamentalsRepo.funded_subset."""
    pool = _FakePool()
    pool.add_fetch(
        "platform.v_universe",
        [
            _universe_row(cid="AAPL_CID", ticker="AAPL", tier=1),
            _universe_row(cid="NOFUND_CID", ticker="NOFUND", tier=1),
        ],
    )
    # Only AAPL has fundamentals in this fixture.
    pool.add_fetch(
        "platform.fundamentals_quarterly",
        [{"classification_id": "AAPL_CID"}],
    )
    adapter = PostgresDataAdapter(pool)  # type: ignore[arg-type]

    universe = await adapter.get_universe_by_liquidity_tier(
        max_tier=2,
        require_fundamentals=True,
    )

    assert universe == ["AAPL"]


@pytest.mark.asyncio
async def test_get_universe_by_liquidity_tier_dedupes_ticker_history_duplicates() -> None:
    """Same cid can appear via multiple ticker_history rows — dedupe in output."""
    pool = _FakePool()
    pool.add_fetch(
        "platform.v_universe",
        [
            _universe_row(cid="AAPL_CID", ticker="AAPL", tier=1),
            _universe_row(cid="AAPL_CID", ticker="AAPL", tier=1),  # dupe
            _universe_row(cid="MSFT_CID", ticker="MSFT", tier=1),
        ],
    )
    adapter = PostgresDataAdapter(pool)  # type: ignore[arg-type]

    universe = await adapter.get_universe_by_liquidity_tier(max_tier=2)

    assert universe == ["AAPL", "MSFT"]


@pytest.mark.asyncio
async def test_list_active_symbols_aliases_universe() -> None:
    """list_active_symbols delegates to get_universe_symbols."""
    pool = _FakePool()
    pool.add_fetch(
        "FROM platform.prices_daily",
        [{"ticker": "AAPL"}, {"ticker": "MSFT"}],
    )
    adapter = PostgresDataAdapter(pool)  # type: ignore[arg-type]

    universe = await adapter.get_universe_symbols()
    pool.conn.calls.clear()

    pool.script["fetch"].clear()
    pool.add_fetch(
        "FROM platform.prices_daily",
        [{"ticker": "AAPL"}, {"ticker": "MSFT"}],
    )
    active = await adapter.list_active_symbols()

    assert active == universe == ["AAPL", "MSFT"]


# ────────────────────────────────────────────────────────────────────────────
# NotImplementedError methods (unchanged)
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_quote_not_implemented() -> None:
    pool = _FakePool()
    adapter = PostgresDataAdapter(pool)  # type: ignore[arg-type]
    with pytest.raises(NotImplementedError, match="real-time quotes"):
        await adapter.get_quote("AAPL")


@pytest.mark.asyncio
async def test_get_fundamentals_not_implemented() -> None:
    pool = _FakePool()
    adapter = PostgresDataAdapter(pool)  # type: ignore[arg-type]
    with pytest.raises(NotImplementedError, match="FundamentalsCache"):
        await adapter.get_fundamentals("AAPL")


@pytest.mark.asyncio
async def test_get_earnings_calendar_not_implemented() -> None:
    pool = _FakePool()
    adapter = PostgresDataAdapter(pool)  # type: ignore[arg-type]
    with pytest.raises(NotImplementedError, match="FMP earnings adapter"):
        await adapter.get_earnings_calendar("AAPL", date(2026, 1, 1), date(2026, 12, 31))


@pytest.mark.asyncio
async def test_list_delisted_symbols_not_implemented() -> None:
    pool = _FakePool()
    adapter = PostgresDataAdapter(pool)  # type: ignore[arg-type]
    with pytest.raises(NotImplementedError, match="not yet wired"):
        await adapter.list_delisted_symbols()
