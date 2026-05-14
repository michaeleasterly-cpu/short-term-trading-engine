"""Alpaca-py historical data adapter behind ``DataProviderInterface``.

Only the methods Sigma actually uses are implemented today —
``get_daily_bars`` for the universe scan. The remaining
``DataProviderInterface`` methods raise ``NotImplementedError`` so future
engines can opt into Alpaca for those without changing the call sites that
already work.

The synchronous ``StockHistoricalDataClient`` is wrapped in
``asyncio.to_thread`` so the event loop stays responsive — same pattern as
``AlpacaPaperBrokerAdapter``.
"""
from __future__ import annotations

import asyncio
import os
from datetime import date as date_t
from datetime import datetime
from decimal import Decimal
from typing import Any

import structlog

from tpcore.interfaces.data import (
    Bar,
    DataProviderInterface,
    EarningsEvent,
    Fundamentals,
    Quote,
)

logger = structlog.get_logger(__name__)


class AlpacaDataAdapter(DataProviderInterface):
    """Provides daily bars via the Alpaca SIP feed (paid tier).

    The default was previously ``"iex"``; switched to ``"sip"`` on
    2026-05-13 after discovering IEX silently misses tickers that trade
    primarily off-IEX (e.g. ALOV/LPCV/PAAC/XBPEW). The account is
    subscribed to SIP; the bigger feed produces ~60% more bars per pull
    with zero physical-integrity violations.

    Args:
        api_key: Alpaca API key (defaults to ``ALPACA_KEY`` env).
        api_secret: Alpaca secret (defaults to ``ALPACA_SECRET`` env).
        feed: Alpaca data feed; ``"sip"`` for complete data,
            ``"iex"`` only when explicitly testing the free-tier subset.
        _client: test-only injection point for the SDK client.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_secret: str | None = None,
        feed: str = "sip",
        _client: Any | None = None,
    ) -> None:
        self._api_key = api_key or os.getenv("ALPACA_KEY")
        self._api_secret = api_secret or os.getenv("ALPACA_SECRET")
        self._feed = feed
        self._client = _client if _client is not None else self._build_client()

    def _build_client(self) -> Any:
        if not self._api_key or not self._api_secret:
            raise RuntimeError("ALPACA_KEY / ALPACA_SECRET not set in environment")
        from alpaca.data.historical.stock import StockHistoricalDataClient

        return StockHistoricalDataClient(api_key=self._api_key, secret_key=self._api_secret)

    async def get_daily_bars(self, symbol: str, start: date_t, end: date_t) -> list[Bar]:
        """Fetch daily OHLCV bars for ``symbol`` between ``start`` and ``end`` (inclusive).

        Returns bars in ascending session-date order. Empty list when the
        symbol has no data in the window (delisted before ``start``, or new
        listing after ``end``).
        """
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        request = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Day,
            start=datetime(start.year, start.month, start.day),
            end=datetime(end.year, end.month, end.day),
            feed=self._feed,
            adjustment="raw",
        )
        try:
            raw = await asyncio.to_thread(self._client.get_stock_bars, request)
        except Exception as exc:
            logger.warning("tpcore.alpaca.bars_failed", symbol=symbol, error=str(exc))
            return []
        rows = (raw.data.get(symbol) if hasattr(raw, "data") else raw.get(symbol)) or []
        return [
            Bar(
                symbol=symbol,
                ts=r.timestamp,
                open=Decimal(str(r.open)),
                high=Decimal(str(r.high)),
                low=Decimal(str(r.low)),
                close=Decimal(str(r.close)),
                volume=int(r.volume),
            )
            for r in rows
        ]

    async def get_quote(self, symbol: str) -> Quote:
        """Fetch the latest NBBO quote for ``symbol`` via Alpaca's SIP feed.

        Returns the most recent bid/ask the SIP has seen — works during
        regular session, extended hours, and overnight (Alpaca always
        returns the last-known quote, even when the market is closed).

        Used by the daily ``pipeline_smoke_test`` to anchor TP/SL to
        live price rather than yesterday's close (which drifts intraday
        and breaks the bracket's ``take_profit.limit_price >= base_price``
        invariant). The single SIP request is metered against the same
        quota as ``get_daily_bars``; cost is negligible compared to a
        bars pull.

        Raises ``RuntimeError`` if Alpaca returns no row for ``symbol``
        (delisted, halted, invalid). Callers should treat that as a hard
        failure rather than a soft empty.
        """
        from alpaca.data.requests import StockLatestQuoteRequest

        request = StockLatestQuoteRequest(symbol_or_symbols=symbol, feed=self._feed)
        try:
            raw = await asyncio.to_thread(self._client.get_stock_latest_quote, request)
        except Exception as exc:
            logger.warning("tpcore.alpaca.quote_failed", symbol=symbol, error=str(exc))
            raise RuntimeError(f"AlpacaDataAdapter.get_quote({symbol}) failed: {exc}") from exc
        # alpaca-py returns a dict keyed by symbol; the value is a Quote
        # object with bid_price / ask_price / bid_size / ask_size / timestamp.
        q = raw.get(symbol) if hasattr(raw, "get") else None
        if q is None:
            raise RuntimeError(f"AlpacaDataAdapter.get_quote({symbol}): no quote returned")
        return Quote(
            symbol=symbol,
            ts=q.timestamp,
            bid=Decimal(str(q.bid_price)),
            ask=Decimal(str(q.ask_price)),
            bid_size=int(q.bid_size or 0),
            ask_size=int(q.ask_size or 0),
        )

    async def get_fundamentals(  # pragma: no cover - Sigma doesn't use fundamentals
        self, symbol: str, as_of: date_t | None = None
    ) -> Fundamentals | None:
        raise NotImplementedError("Use the FMP/SEC adapter for fundamentals")

    async def get_earnings_calendar(  # pragma: no cover - Sigma doesn't use earnings
        self, symbol: str, start: date_t, end: date_t
    ) -> list[EarningsEvent]:
        raise NotImplementedError("Use the FMP adapter for earnings")

    async def list_active_symbols(self) -> list[str]:
        """Return all currently-active US-equity tickers Alpaca can trade."""
        from alpaca.trading.client import TradingClient
        from alpaca.trading.enums import AssetClass, AssetStatus
        from alpaca.trading.requests import GetAssetsRequest

        # The data client doesn't list assets — assets live on the trading client.
        # Build a lightweight one inline; reuse same creds.
        if not self._api_key or not self._api_secret:
            raise RuntimeError("ALPACA_KEY / ALPACA_SECRET not set in environment")
        trading = TradingClient(api_key=self._api_key, secret_key=self._api_secret, paper=True)
        request = GetAssetsRequest(asset_class=AssetClass.US_EQUITY, status=AssetStatus.ACTIVE)
        assets = await asyncio.to_thread(trading.get_all_assets, request)
        return [a.symbol for a in assets if getattr(a, "tradable", True)]

    async def list_delisted_symbols(self) -> list[tuple[str, date_t]]:  # pragma: no cover
        raise NotImplementedError(
            "Survivorship-free backtests use platform.prices_daily, not the live data adapter"
        )


__all__ = ["AlpacaDataAdapter"]
