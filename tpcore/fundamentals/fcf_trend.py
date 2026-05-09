"""Free-cash-flow trend analysis."""
from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class FCFTrendResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    n_periods: int
    cagr: Decimal | None = None
    slope: Decimal | None = None
    is_growing: bool = False
    is_volatile: bool = False


def analyze_fcf_trend(fcf_history: list[Decimal]) -> FCFTrendResult:
    """Slope + CAGR + volatility classification of an FCF series.

    TODO: implement with numpy least-squares; volatility = stddev / |mean|.
    """
    _ = fcf_history
    raise NotImplementedError
