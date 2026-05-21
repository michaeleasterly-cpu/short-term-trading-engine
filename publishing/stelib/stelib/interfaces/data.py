"""Data-provider abstraction.

Engines and the backtest harness both consume data through this interface.
This is the seam that lets us swap Alpaca/FMP/SEC EDGAR/etc. without
touching strategy code.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class Bar(BaseModel):
    """OHLCV bar. ``ts`` is the bar **close** in UTC."""

    model_config = ConfigDict(extra="forbid")

    symbol: str
    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    adjusted_close: Decimal | None = None


class Quote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    ts: datetime
    bid: Decimal
    ask: Decimal
    bid_size: int = 0
    ask_size: int = 0


class Fundamentals(BaseModel):
    """Point-in-time fundamentals snapshot. Always tagged with ``as_of``."""

    model_config = ConfigDict(extra="allow")

    symbol: str
    as_of: date
    period: str  # e.g. "2025-Q3"
    revenue: Decimal | None = None
    net_income: Decimal | None = None
    free_cash_flow: Decimal | None = None
    total_assets: Decimal | None = None
    receivables: Decimal | None = None
    capex: Decimal | None = None
    shares_outstanding: Decimal | None = None
    cash_and_equivalents: Decimal | None = None
    total_debt: Decimal | None = None


class EarningsEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    report_date: date
    fiscal_period: str
    eps_estimate: Decimal | None = None
    eps_actual: Decimal | None = None
    revenue_estimate: Decimal | None = None
    revenue_actual: Decimal | None = None


class DataProviderInterface(ABC):
    """Abstract data provider. All datetimes UTC; all dates are NYSE session dates."""

    @abstractmethod
    async def get_daily_bars(self, symbol: str, start: date, end: date) -> list[Bar]:
        raise NotImplementedError

    @abstractmethod
    async def get_quote(self, symbol: str) -> Quote:
        raise NotImplementedError

    @abstractmethod
    async def get_fundamentals(self, symbol: str, as_of: date | None = None) -> Fundamentals | None:
        """Return point-in-time fundamentals as of ``as_of`` (defaults to today)."""
        raise NotImplementedError

    @abstractmethod
    async def get_earnings_calendar(
        self, symbol: str, start: date, end: date
    ) -> list[EarningsEvent]:
        raise NotImplementedError

    @abstractmethod
    async def list_active_symbols(self) -> list[str]:
        raise NotImplementedError

    @abstractmethod
    async def list_delisted_symbols(self) -> list[tuple[str, date]]:
        """Return ``(symbol, delisting_date)`` pairs. Required for survivorship-free backtests."""
        raise NotImplementedError
