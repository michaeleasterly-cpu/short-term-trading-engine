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
    """Provides daily bars via the Alpaca free-tier IEX feed.

    Args:
        api_key: Alpaca API key (defaults to ``ALPACA_KEY`` env).
        api_secret: Alpaca secret (defaults to ``ALPACA_SECRET`` env).
        feed: Alpaca data feed; the free tier is ``"iex"``.
        _client: test-only injection point for the SDK client.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_secret: str | None = None,
        feed: str = "iex",
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

    async def get_quote(self, symbol: str) -> Quote:  # pragma: no cover - unused by Sigma
        raise NotImplementedError("AlpacaDataAdapter.get_quote not implemented")

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
