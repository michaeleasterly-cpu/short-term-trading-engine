"""Postgres-backed implementation of ``DataProviderInterface``.

Reads daily bars from ``platform.prices_daily`` — the survivorship-free
canonical store maintained by the corporate-actions pipeline. There is no
fallback to a live API: backtest, paper, and live trading must all read
through the same source for parity. If the database is unreachable, the
caller halts.

Only ``get_daily_bars`` and ``list_active_symbols`` are implemented today.
The remaining ``DataProviderInterface`` methods raise ``NotImplementedError``;
they belong to fundamentals / quote / earnings adapters that are wired
separately.
"""
from __future__ import annotations

from datetime import UTC, datetime
from datetime import date as date_t
from typing import TYPE_CHECKING

import structlog

from tpcore.interfaces.data import (
    Bar,
    DataProviderInterface,
    EarningsEvent,
    Fundamentals,
    Quote,
)

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)


class PostgresDataAdapter(DataProviderInterface):
    """Daily bars from ``platform.prices_daily`` — sole source of truth.

    The pool is required at construction. A ``None`` pool is rejected
    immediately rather than deferred — there is no Alpaca fallback path,
    and a silently-degraded adapter would break the parity guarantee that
    backtest/paper/live trading all read the same data.
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        if pool is None:
            raise ValueError(
                "PostgresDataAdapter requires a connection pool — "
                "platform.prices_daily is the sole bar source, no live-API fallback"
            )
        self._pool = pool

    async def get_daily_bars(
        self,
        symbol: str,
        start: date_t,
        end: date_t | None = None,
        as_of: date_t | None = None,
    ) -> list[Bar]:
        """Fetch daily OHLCV bars for ``symbol`` from ``platform.prices_daily``.

        Args:
            symbol: ticker.
            start: inclusive lower bound on the bar date.
            end: inclusive upper bound on the bar date; ``None`` = no upper bound.
            as_of: point-in-time clamp — exclude bars after this date even if
                ``end`` is later. Use this in backtests / replay to forbid
                look-ahead. Both ``end`` and ``as_of`` are applied when given.

        Returns bars in ascending date order; empty list when the symbol has
        no data in the window. Bar ``ts`` is the session date at midnight UTC
        (the column is a ``DATE`` — no intra-day resolution to preserve).
        """
        sql = """
            SELECT date, open, high, low, close, volume, adjusted_close
            FROM platform.prices_daily
            WHERE ticker = $1
              AND date >= $2
              AND ($3::date IS NULL OR date <= $3)
              AND ($4::date IS NULL OR date <= $4)
            ORDER BY date
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql, symbol, start, end, as_of)
        return [
            Bar(
                symbol=symbol,
                ts=datetime(r["date"].year, r["date"].month, r["date"].day, tzinfo=UTC),
                open=r["open"],
                high=r["high"],
                low=r["low"],
                close=r["close"],
                volume=int(r["volume"]),
                adjusted_close=r["adjusted_close"],
            )
            for r in rows
        ]

    async def get_universe_symbols(self) -> list[str]:
        """Distinct tickers with at least one bar in the last 90 days and
        not flagged delisted. Sorted ascending.

        This is the live-tradable universe — if a symbol hasn't priced in
        90 days, it's effectively delisted whether or not the flag is set,
        so the freshness filter doubles as a soft survivorship gate.
        """
        sql = """
            SELECT DISTINCT ticker
            FROM platform.prices_daily
            WHERE date >= CURRENT_DATE - INTERVAL '90 days'
              AND delisted = false
            ORDER BY ticker
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql)
        return [r["ticker"] for r in rows]

    async def get_universe_by_liquidity_tier(self, max_tier: int = 2) -> list[str]:
        """Tickers up to and including the given liquidity tier.

        Use this in any engine that pre-fetches per-ticker context
        (bars, fundamentals, catalysts) — the all-active universe
        (~7,700 tickers) makes the upfront work O(7700) and times out
        against Supabase. T1+T2 is ~1,200 tickers and parallels what
        the credibility backtests scored on.
        """
        sql = """
            SELECT ticker
            FROM platform.liquidity_tiers
            WHERE tier <= $1
            ORDER BY tier ASC, ticker ASC
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql, max_tier)
        return [r["ticker"] for r in rows]

    async def list_active_symbols(self) -> list[str]:
        """``DataProviderInterface`` alias for ``get_universe_symbols``."""
        return await self.get_universe_symbols()

    async def get_quote(self, symbol: str) -> Quote:  # pragma: no cover
        raise NotImplementedError(
            "PostgresDataAdapter does not serve real-time quotes — wire a "
            "broker / market-data adapter for live quotes"
        )

    async def get_fundamentals(  # pragma: no cover
        self, symbol: str, as_of: date_t | None = None
    ) -> Fundamentals | None:
        raise NotImplementedError(
            "Fundamentals come from FundamentalsCache + FMPFundamentalsAdapter, "
            "not platform.prices_daily"
        )

    async def get_earnings_calendar(  # pragma: no cover
        self, symbol: str, start: date_t, end: date_t
    ) -> list[EarningsEvent]:
        raise NotImplementedError(
            "Earnings calendar comes from the FMP earnings adapter, not prices_daily"
        )

    async def list_delisted_symbols(self) -> list[tuple[str, date_t]]:  # pragma: no cover
        raise NotImplementedError(
            "Delisted-symbol listing not yet wired; query "
            "platform.prices_daily WHERE delisted = true directly from "
            "the backtest harness if needed"
        )


__all__ = ["PostgresDataAdapter"]
