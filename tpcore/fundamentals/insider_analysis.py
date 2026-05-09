"""Insider-transaction clustering analysis."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class InsiderTransactionType(str, Enum):
    BUY = "buy"
    SELL = "sell"
    OPTION_EXERCISE = "option_exercise"


class InsiderTransaction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    insider_name: str
    role: str
    txn_date: date
    txn_type: InsiderTransactionType
    shares: Decimal
    price: Decimal


class InsiderSignal(str, Enum):
    BULLISH_CLUSTER = "bullish_cluster"
    BEARISH_CLUSTER = "bearish_cluster"
    NEUTRAL = "neutral"


class InsiderClusterResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lookback_days: int
    distinct_buyers: int = 0
    distinct_sellers: int = 0
    total_buy_value: Decimal = Decimal("0")
    total_sell_value: Decimal = Decimal("0")
    signal: InsiderSignal = InsiderSignal.NEUTRAL
    notes: list[str] = Field(default_factory=list)


def analyze_insider_transactions(
    transactions: list[InsiderTransaction],
    lookback_days: int = 90,
) -> InsiderClusterResult:
    """Detect cluster buying/selling among >=3 distinct insiders inside ``lookback_days``.

    TODO: filter to lookback window, group by insider, count distinct
    buyers/sellers, classify cluster vs. neutral.
    """
    _ = (transactions, lookback_days)
    raise NotImplementedError
