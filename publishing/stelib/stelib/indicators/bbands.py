"""Bollinger Bands.

Moved from ``sigma.plugs.setup_detection`` / ``reversion.plugs.setup_detection``
on 2026-05-14 alongside the engine standardization. The core math is
identical; this returns a 4-tuple including ``width_normalized`` so
Sigma's width-percentile path keeps working. Reversion only needs the
first three return values — it can unpack ``sma, upper, lower, _``.
"""
from __future__ import annotations

import pandas as pd

BB_PERIOD = 20
BB_NUM_STD = 2.0


def compute_bbands(
    df: pd.DataFrame,
    period: int = BB_PERIOD,
    num_std: float = BB_NUM_STD,
) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    """Returns ``(sma, upper, lower, width_normalized)``.

    Args:
        df: DataFrame with at least a ``close`` column.
        period: rolling window. Default 20 matches the Sigma backtest
            baseline.
        num_std: bandwidth in standard deviations. Default 2.0.
    """
    close = df["close"]
    sma = close.rolling(period, min_periods=period).mean()
    sd = close.rolling(period, min_periods=period).std(ddof=0)
    upper = sma + num_std * sd
    lower = sma - num_std * sd
    width = (upper - lower) / sma
    return sma, upper, lower, width


__all__ = ["BB_NUM_STD", "BB_PERIOD", "compute_bbands"]
