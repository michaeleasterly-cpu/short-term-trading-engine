"""Tests for ``tpcore.indicators`` — shared technical indicators.

Smoke tests for the CHOP indicator after its 2026-05-14 extraction
from ``sigma.plugs.setup_detection``. The numeric equivalence is also
covered by the existing Sigma test suite (which still imports CHOP
through ``sigma.plugs.setup_detection``'s re-export); this file
documents the expected behavior at the tpcore boundary directly.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from tpcore.indicators.chop import (
    CHOP_PERIOD,
    CHOP_SIDEWAYS_STRONG,
    CHOP_SIDEWAYS_WEAK,
    compute_chop,
)


def test_chop_constants_unchanged():
    """The Dreiss thresholds are well-known — if these drift, the
    Sigma backtest's CHOP gate would silently change behavior."""
    assert CHOP_PERIOD == 14
    assert CHOP_SIDEWAYS_STRONG == 61.8
    assert CHOP_SIDEWAYS_WEAK == 38.2


def test_chop_returns_nan_for_insufficient_lookback():
    """First ``period`` bars don't have enough history → NaN."""
    n = CHOP_PERIOD
    high = pd.Series([float(i + 2) for i in range(n)])
    low = pd.Series([float(i) for i in range(n)])
    close = pd.Series([float(i + 1) for i in range(n)])
    chop = compute_chop(high, low, close)
    assert pd.isna(chop.iloc[CHOP_PERIOD - 2])


def test_chop_low_on_clean_trend():
    """A monotonically rising series with tight intraday ranges is
    'trending' → CHOP value comfortably below the trending threshold."""
    n = 30
    base = np.linspace(100.0, 150.0, n)
    # Tight intraday ranges so ATR sum is small vs. range — pushes CHOP down.
    high = pd.Series(base + 0.3)
    low = pd.Series(base - 0.3)
    close = pd.Series(base)
    chop_now = float(compute_chop(high, low, close).iloc[-1])
    assert chop_now < CHOP_SIDEWAYS_WEAK


def test_chop_high_on_oscillating_range():
    """An oscillating-in-range series is 'choppy' → CHOP above the
    sideways-strong threshold."""
    n = 30
    # Bars oscillate inside [100, 102] — wide daily ranges, narrow
    # overall range → high CHOP.
    rng = np.tile([100.0, 102.0], n // 2)
    high = pd.Series(rng + 1.5)
    low = pd.Series(rng - 1.5)
    close = pd.Series(rng)
    chop_now = float(compute_chop(high, low, close).iloc[-1])
    assert chop_now > CHOP_SIDEWAYS_STRONG
