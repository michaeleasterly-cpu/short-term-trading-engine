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

from tpcore.data.repositories import PricesRepo, UniverseRepo
from tpcore.identity.dispatcher import IdentityDispatcher
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

    PR-14 (2026-05-25): edge adapter — public methods take ticker (the
    DataProviderInterface contract); internally dispatches to
    classification_id and reads via PricesRepo + UniverseRepo. The
    legacy column ``prices_daily.adjusted_close`` is NOT exposed by
    PricesRepo (OHLCV-only); ``Bar.adjusted_close`` falls back to
    ``close`` (the vast majority of consumers treat them as equivalent
    post-split-adjustment).
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
        dispatcher = IdentityDispatcher(self._pool)
        cid = await dispatcher.ticker_to_classification_id(symbol)
        if cid is None:
            return []

        # Resolve effective upper bound: the more restrictive of end/as_of,
        # or 9999-12-31 sentinel when both are None.
        upper_bounds = [b for b in (end, as_of) if b is not None]
        upper = min(upper_bounds) if upper_bounds else date_t(9999, 12, 31)

        repo = PricesRepo(self._pool)
        bars = await repo.get_window(cid, start, upper)
        return [
            Bar(
                symbol=symbol,
                ts=datetime(b.date.year, b.date.month, b.date.day, tzinfo=UTC),
                open=b.open,
                high=b.high,
                low=b.low,
                close=b.close,
                volume=b.volume,
                adjusted_close=b.close,
            )
            for b in bars
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

    async def get_universe_by_liquidity_tier(
        self,
        max_tier: int = 2,
        *,
        asset_class: str | None = None,
        asset_class_in: frozenset[str] | None = None,
        require_fundamentals: bool = False,
    ) -> list[str]:
        """Tickers up to and including the given liquidity tier.

        Use this in any engine that pre-fetches per-ticker context
        (bars, fundamentals, catalysts) — the all-active universe
        (~7,700 tickers) makes the upfront work O(7700) and times out
        against Supabase. T1+T2 is ~1,200 tickers and parallels what
        the credibility backtests scored on.

        Optional filters:

        * ``asset_class`` (legacy 2026-05-15) / ``asset_class_in``
          (multi-class, 2026-05-30) — inner-joins
          ``platform.ticker_classifications`` and filters by
          ``asset_class``. New engine code should use
          ``asset_class_in=engine_profile.allowed_asset_classes``
          to honour the per-engine roster declaration.
        * ``require_fundamentals``: when True, inner-joins
          ``platform.fundamentals_quarterly`` (DISTINCT ticker) so only
          tickers with at least one fundamentals row are returned —
          required for the Reversion EQ gate.
        """
        repo = UniverseRepo(self._pool)
        rows = await repo.enumerate(
            max_liquidity_tier=max_tier,
            asset_class=asset_class,
            asset_class_in=asset_class_in,
        )
        if require_fundamentals:
            from tpcore.data.repositories import FundamentalsRepo

            funded = await FundamentalsRepo(self._pool).funded_subset([r.classification_id for r in rows])
            rows = [r for r in rows if r.classification_id in funded]

        # Match the legacy ORDER BY (lt.tier ASC, lt.ticker ASC).
        rows_sorted = sorted(
            rows,
            key=lambda r: (
                r.liquidity_tier if r.liquidity_tier is not None else max_tier + 1,
                r.current_ticker or "",
            ),
        )
        # Dedupe (same cid can appear via multiple ticker_history rows).
        seen: set[str] = set()
        out: list[str] = []
        for r in rows_sorted:
            t = r.current_ticker
            if t is not None and t not in seen:
                seen.add(t)
                out.append(t)
        return out

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
            "Fundamentals come from FundamentalsCache + FMPFundamentalsAdapter, not platform.prices_daily"
        )

    async def get_earnings_calendar(  # pragma: no cover
        self, symbol: str, start: date_t, end: date_t
    ) -> list[EarningsEvent]:
        raise NotImplementedError("Earnings calendar comes from the FMP earnings adapter, not prices_daily")

    async def list_delisted_symbols(self) -> list[tuple[str, date_t]]:  # pragma: no cover
        raise NotImplementedError(
            "Delisted-symbol listing not yet wired; query "
            "platform.prices_daily WHERE delisted = true directly from "
            "the backtest harness if needed"
        )


__all__ = ["PostgresDataAdapter"]
