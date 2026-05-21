"""Dreiss Choppiness Index (CHOP).

Moved from ``sigma.plugs.setup_detection`` 2026-05-14 so that both
Sigma (regime filter) and AllocatorService (rebalance gating) can
consume the same canonical implementation. The formula is unchanged
— the prior CHOP backtest validated this exact expression and any
drift in the math would invalidate that result.

``CHOP = 100 * log10(SUM(ATR(1), n) / (MaxHi(n) - MinLo(n))) / log10(n)``

Output bounded in roughly ``[0, 100]``. Above 61.8 → sideways chop;
below 38.2 → trending; between → transitional. Returns ``NaN`` for
bars with insufficient lookback.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

CHOP_PERIOD = 14
CHOP_SIDEWAYS_STRONG = 61.8
CHOP_SIDEWAYS_WEAK = 38.2


def compute_chop(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = CHOP_PERIOD,
) -> pd.Series:
    """Choppiness Index (Dreiss). See module docstring for the formula."""
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    sum_atr = tr.rolling(period, min_periods=period).sum()
    max_high = high.rolling(period, min_periods=period).max()
    min_low = low.rolling(period, min_periods=period).min()
    denom = (max_high - min_low).replace(0, np.nan)
    ratio = sum_atr / denom
    # log10 of non-positive values would warn; mask them out.
    safe_ratio = ratio.where(ratio > 0)
    return 100.0 * np.log10(safe_ratio) / np.log10(period)


__all__ = [
    "CHOP_PERIOD",
    "CHOP_SIDEWAYS_STRONG",
    "CHOP_SIDEWAYS_WEAK",
    "compute_chop",
]
