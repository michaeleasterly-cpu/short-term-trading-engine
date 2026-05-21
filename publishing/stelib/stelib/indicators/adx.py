"""Wilder's Average Directional Index (ADX).

Moved from ``sigma.plugs.setup_detection`` / ``reversion.plugs.setup_detection``
on 2026-05-14 alongside the engine standardization. Sigma and Reversion
both used byte-identical implementations; this is the canonical one.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

ADX_PERIOD = 14


def compute_adx(df: pd.DataFrame, period: int = ADX_PERIOD) -> pd.Series:
    """Wilder's ADX. Returns a Series indexed like df.

    Args:
        df: DataFrame with ``high``, ``low``, ``close`` columns.
        period: lookback for Wilder smoothing. Default 14 matches the
            Sigma + Reversion baselines that earlier backtests
            validated.
    """
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)

    tr = pd.concat(
        [(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)

    up = high.diff()
    dn = -low.diff()
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)

    atr = pd.Series(tr).rolling(period, min_periods=period).mean()
    plus_di = (
        100
        * pd.Series(plus_dm, index=df.index).rolling(period, min_periods=period).mean()
        / atr
    )
    minus_di = (
        100
        * pd.Series(minus_dm, index=df.index).rolling(period, min_periods=period).mean()
        / atr
    )

    denom = (plus_di + minus_di).replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / denom
    return dx.rolling(period, min_periods=period).mean()


__all__ = ["ADX_PERIOD", "compute_adx"]
