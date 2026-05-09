"""Abstract interfaces and shared Pydantic models.

Engines depend only on these abstractions — never on a concrete vendor SDK.
"""

from .broker import (
    AccountInfo,
    BrokerExecutionInterface,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    TimeInForce,
)
from .data import (
    Bar,
    DataProviderInterface,
    EarningsEvent,
    Fundamentals,
    Quote,
)
from .engine_plug import BaseEnginePlug

__all__ = [
    "AccountInfo",
    "Bar",
    "BaseEnginePlug",
    "BrokerExecutionInterface",
    "DataProviderInterface",
    "EarningsEvent",
    "Fundamentals",
    "Order",
    "OrderSide",
    "OrderStatus",
    "OrderType",
    "Position",
    "Quote",
    "TimeInForce",
]
