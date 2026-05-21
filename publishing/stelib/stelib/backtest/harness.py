"""Provider-agnostic backtest harness.

Strategies receive a ``DataProviderInterface`` — never a vendor SDK — so the
same strategy code runs in backtest, paper, and live with byte-identical
inputs (modulo the data source).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from stelib.interfaces.data import Bar, DataProviderInterface

from .cost_model import SimpleCostModel


class BacktestConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: date
    end: date
    starting_capital: Decimal = Decimal("100000")
    universe: list[str] = Field(default_factory=list, description="Empty = all active+delisted.")
    include_delisted: bool = True


class BacktestResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    config: BacktestConfig
    final_equity: Decimal
    total_return_pct: Decimal
    sharpe: Decimal | None = None
    max_drawdown_pct: Decimal | None = None
    n_trades: int = 0
    trades: list[dict] = Field(default_factory=list)


class Strategy(ABC):
    """User-supplied strategy. Pure logic — no I/O beyond ``data``."""

    @abstractmethod
    async def on_bar(self, bar: Bar, state: dict) -> list[dict]:
        """Receive next session's bar; return zero or more order intents."""
        raise NotImplementedError


class BacktestHarness:
    """Runs a ``Strategy`` against a ``DataProviderInterface`` day-by-day."""

    def __init__(
        self,
        data: DataProviderInterface,
        cost_model: SimpleCostModel | None = None,
    ) -> None:
        self._data = data
        self._cost_model = cost_model or SimpleCostModel()

    async def run(self, strategy: Strategy, config: BacktestConfig) -> BacktestResult:
        """Drive the strategy bar-by-bar and aggregate results.

        TODO: build the date axis from XNYS sessions only; for each session
        and each universe symbol, fetch the bar through ``self._data``,
        invoke ``strategy.on_bar``, simulate fills via ``self._cost_model``,
        and accumulate trades + equity curve. NEVER hardcode a vendor.
        """
        _ = (strategy, config, self._data, self._cost_model)
        raise NotImplementedError
