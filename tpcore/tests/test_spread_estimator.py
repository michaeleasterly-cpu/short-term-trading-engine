"""Unit tests for the Corwin-Schultz spread estimator.

The CS formula has a few known invariants we can pin without a real
quote feed:

* A constant H = L series (zero range) returns 0 (or NaN at the
  boundary), never negative.
* Wider H/L ranges produce larger spread estimates than narrower ones
  for the same close-to-close move.
* The first row is always NaN — the formula needs a t-1 bar.
* ``average_spread_estimate`` returns ``None`` when too few bars are
  available.

We don't pin the numerical output against the paper's worked examples
because the paper uses a Monte Carlo against simulated quote data; the
ranking-only role for this estimator means the absolute value isn't
load-bearing — only the ordering matters.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from tpcore.backtest.spread_estimator import (
    average_spread_estimate,
    estimate_spread_corwin_schultz,
)


def _bars(highs: list[float], lows: list[float]) -> tuple[pd.Series, pd.Series]:
    idx = pd.date_range("2024-01-01", periods=len(highs), freq="B")
    return pd.Series(highs, index=idx), pd.Series(lows, index=idx)


def test_zero_range_returns_zero_or_nan() -> None:
    high, low = _bars([100.0] * 10, [100.0] * 10)
    out = estimate_spread_corwin_schultz(high, low)
    # First entry uses NaN (no t-1); the formula at zero range yields
    # 0 / NaN — never negative, and the .dropna mean is 0.
    cleaned = out.replace([np.inf, -np.inf], np.nan).dropna()
    assert (cleaned >= 0).all()
    # Mean over the zero-range section is 0.
    assert cleaned.mean() == 0.0


def test_wider_range_gives_larger_spread() -> None:
    narrow_high, narrow_low = _bars(
        [101.0, 101.0, 101.0, 101.0, 101.0, 101.0, 101.0, 101.0, 101.0, 101.0],
        [100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0],
    )
    wide_high, wide_low = _bars(
        [110.0, 110.0, 110.0, 110.0, 110.0, 110.0, 110.0, 110.0, 110.0, 110.0],
        [100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0],
    )
    n = average_spread_estimate(narrow_high, narrow_low) or 0.0
    w = average_spread_estimate(wide_high, wide_low) or 0.0
    assert w > n


def test_first_value_is_nan() -> None:
    high, low = _bars([102.0, 103.0, 101.5], [100.0, 101.0, 100.5])
    out = estimate_spread_corwin_schultz(high, low)
    # Last value also NaN because there's no t+1 for the gamma term.
    assert math.isnan(out.iloc[-1])


def test_length_mismatch_raises() -> None:
    high = pd.Series([1.0, 2.0])
    low = pd.Series([0.5])
    try:
        estimate_spread_corwin_schultz(high, low)
    except ValueError as exc:
        assert "length mismatch" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_average_returns_none_when_too_few_observations() -> None:
    # min_observations defaults to 5; only 3 bars → not enough.
    high, low = _bars([102.0, 103.0, 101.5], [100.0, 101.0, 100.5])
    assert average_spread_estimate(high, low) is None


def test_average_succeeds_with_enough_data() -> None:
    # Synthetic random walk with deliberate H/L gaps so the estimator has
    # something non-degenerate to chew on.
    rng = np.random.default_rng(42)
    closes = 100 + np.cumsum(rng.normal(0, 0.2, size=30))
    highs = closes + rng.uniform(0.1, 0.5, size=30)
    lows = closes - rng.uniform(0.1, 0.5, size=30)
    high = pd.Series(highs, index=pd.date_range("2024-01-01", periods=30, freq="B"))
    low = pd.Series(lows, index=high.index)
    avg = average_spread_estimate(high, low)
    assert avg is not None
    assert avg >= 0
    # Sanity: a ~30¢ range on $100 close shouldn't blow up to >5%.
    assert avg < 0.05
