"""Canary — data models. The canary's only 'strategy' is: hold 1
share SPY. CANARY_TICKER/CANARY_QTY are the single source of truth.
CANARY_MAX_NOTIONAL_USD keeps trades microscopic (tiny fixed cap)."""
from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict

CANARY_TICKER = "SPY"
CANARY_QTY = 1
CANARY_MAX_NOTIONAL_USD = Decimal("2000")  # 1 share SPY ceiling; tiny


class CanarySignal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    ticker: str = CANARY_TICKER
    qty: int = CANARY_QTY


class CanaryDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    ticker: str
    qty: int
    notional_usd: Decimal
