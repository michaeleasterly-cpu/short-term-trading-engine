"""Broker abstraction.

Engines must call only this interface — never a vendor SDK directly.
The Alpaca-specific implementation lives outside ``tpcore``.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"


class TimeInForce(StrEnum):
    DAY = "day"
    GTC = "gtc"
    IOC = "ioc"
    FOK = "fok"
    OPG = "opg"
    CLS = "cls"


class OrderStatus(StrEnum):
    NEW = "new"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"


class OrderClass(StrEnum):
    """Bracket vs. plain order. Mirrors Alpaca's ``order_class``.

    A BRACKET order carries linked take-profit and stop-loss legs that the
    broker submits atomically; when one fills the other is auto-cancelled.
    """

    SIMPLE = "simple"
    BRACKET = "bracket"


class Order(BaseModel):
    """Cross-broker order model. Timestamps in UTC."""

    model_config = ConfigDict(frozen=False, extra="forbid")

    client_order_id: str
    broker_order_id: str | None = None
    symbol: str
    side: OrderSide
    qty: Decimal
    order_type: OrderType
    time_in_force: TimeInForce = TimeInForce.DAY
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    order_class: OrderClass = OrderClass.SIMPLE
    take_profit_limit_price: Decimal | None = Field(
        default=None, description="Bracket TP leg limit price; required iff order_class=BRACKET."
    )
    stop_loss_stop_price: Decimal | None = Field(
        default=None, description="Bracket SL leg stop price; required iff order_class=BRACKET."
    )
    status: OrderStatus = OrderStatus.NEW
    filled_qty: Decimal = Decimal("0")
    avg_fill_price: Decimal | None = None
    submitted_at: datetime | None = None
    filled_at: datetime | None = None
    engine_id: str | None = Field(default=None, description="Originating engine, e.g. 'sigma'.")


class Position(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    qty: Decimal
    avg_entry_price: Decimal
    market_value: Decimal | None = None
    unrealized_pl: Decimal | None = None
    cost_basis: Decimal | None = None


class AccountInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: str
    cash: Decimal
    equity: Decimal
    buying_power: Decimal
    portfolio_value: Decimal
    pattern_day_trader: bool = False
    paper: bool = True


class BrokerExecutionInterface(ABC):
    """Abstract broker. Implementations must be idempotent on retries."""

    @abstractmethod
    async def get_account(self) -> AccountInfo:
        raise NotImplementedError

    @abstractmethod
    async def get_positions(self) -> list[Position]:
        raise NotImplementedError

    @abstractmethod
    async def place_order(self, order: Order) -> Order:
        """Submit ``order``. Returns the broker-acknowledged order with ids/status set."""
        raise NotImplementedError

    @abstractmethod
    async def cancel_order(self, order_id: str) -> None:
        raise NotImplementedError

    @abstractmethod
    async def get_order(self, order_id: str) -> Order:
        raise NotImplementedError

    @abstractmethod
    async def emergency_cancel_all(self) -> int:
        """Cancel all open orders. Returns the number of orders canceled."""
        raise NotImplementedError
