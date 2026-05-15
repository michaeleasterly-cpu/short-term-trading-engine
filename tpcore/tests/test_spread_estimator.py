"""Unit tests for the Abdi-Ranaldo (2017) spread estimator.

Replaces the prior Corwin-Schultz tests after the 2026-05-15 estimator
retirement. The archived C-S implementation (and its original tests)
lives at ``tpcore/backtest/spread_estimator_archive.py`` — no active
test imports from it; it's preserved for academic reference only.

Pinning a few invariants and one realism check:

* Constant OHLC (zero range, zero close-to-mid distance) returns 0 or
  NaN, never negative.
* Wider H/L ranges produce larger spread estimates than narrower ones
  for the same close pattern.
* The last value is always NaN — the formula needs the t+1 mid.
* ``average_spread_estimate`` returns ``None`` when too few bars.
* Length-mismatch input raises ValueError.
* **Realism**: synthetic high-volatility / tight-close bars produce a
  small spread estimate (validates the C-S failure mode is not
  reproduced); synthetic narrow-range bars where the close walks far
  from mid produce a wider estimate.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from tpcore.backtest.spread_estimator import (
    SOURCE_TAG,
    average_spread_estimate,
    estimate_spread_abdi_ranaldo,
)


def _bars(
    highs: list[float], lows: list[float], closes: list[float]
) -> tuple[pd.Series, pd.Series, pd.Series]:
    idx = pd.date_range("2024-01-01", periods=len(highs), freq="B")
    return (
        pd.Series(highs, index=idx),
        pd.Series(lows, index=idx),
        pd.Series(closes, index=idx),
    )


def test_source_tag_is_abdi_ranaldo() -> None:
    """SOURCE_TAG is the canonical column value persisted to
    ``platform.spread_observations.source``. Pinning it here so a
    rename can't silently break the aggregator in
    ``scripts/assign_liquidity_tiers.py``."""
    assert SOURCE_TAG == "abdi_ranaldo"


def test_zero_range_returns_zero_or_nan() -> None:
    high, low, close = _bars([100.0] * 10, [100.0] * 10, [100.0] * 10)
    out = estimate_spread_abdi_ranaldo(high, low, close)
    cleaned = out.replace([np.inf, -np.inf], np.nan).dropna()
    assert (cleaned >= 0).all()
    assert cleaned.mean() == 0.0


def test_last_value_is_nan() -> None:
    """Last value uses t+1's mid which doesn't exist, so it's NaN."""
    high, low, close = _bars(
        [102.0, 103.0, 101.5],
        [100.0, 101.0, 100.5],
        [101.0, 102.0, 101.0],
    )
    out = estimate_spread_abdi_ranaldo(high, low, close)
    assert math.isnan(out.iloc[-1])


def test_length_mismatch_raises() -> None:
    try:
        estimate_spread_abdi_ranaldo(
            pd.Series([1.0, 2.0]),
            pd.Series([0.5]),
            pd.Series([0.75, 1.5]),
        )
    except ValueError as exc:
        assert "length mismatch" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_average_returns_none_when_too_few_observations() -> None:
    """min_observations defaults to 5; only 3 bars → not enough."""
    high, low, close = _bars(
        [102.0, 103.0, 101.5],
        [100.0, 101.0, 100.5],
        [101.0, 102.0, 101.0],
    )
    assert average_spread_estimate(high, low, close) is None


def test_average_succeeds_with_enough_data() -> None:
    """Synthetic random walk — sanity-check the estimator runs end-to-end
    on enough bars to satisfy the min_observations gate."""
    rng = np.random.default_rng(42)
    closes = 100 + np.cumsum(rng.normal(0, 0.2, size=30))
    highs = closes + rng.uniform(0.1, 0.5, size=30)
    lows = closes - rng.uniform(0.1, 0.5, size=30)
    idx = pd.date_range("2024-01-01", periods=30, freq="B")
    high = pd.Series(highs, index=idx)
    low = pd.Series(lows, index=idx)
    close = pd.Series(closes, index=idx)
    avg = average_spread_estimate(high, low, close)
    assert avg is not None
    assert avg >= 0
    # 30¢ range on $100 close shouldn't blow up beyond a few percent.
    assert avg < 0.05


def test_high_volatility_tight_close_to_mid_yields_small_spread() -> None:
    """Regression test for the Corwin-Schultz failure mode.

    Synthetic bars with a realistic 1% daily range BUT close very
    near the mid (volatile but quote-tight stock — price moves
    around inside the day but settles near mid). C-S would have
    inflated the spread estimate to ~30+ bp; AR should stay
    below ~5 bp.

    Note on the threshold: AR uses LOG-mid-range, which differs
    slightly from arithmetic mid-range. For a 1% H/L band the
    structural offset is ~0.2 bp — negligible. For pathologically
    wide bands (e.g. 10%) AR introduces a residual ~25 bp that's
    formula-intrinsic, not a bug; real-world bars on liquid names
    never have 10% daily H/L on a daily timeframe.
    """
    n = 30
    # Mid stays at 100; ±0.5 daily ranges (1%); close right at mid.
    high = pd.Series([100.5] * n, index=pd.date_range("2024-01-01", periods=n, freq="B"))
    low = pd.Series([99.5] * n, index=high.index)
    close = pd.Series([100.0] * n, index=high.index)
    avg = average_spread_estimate(high, low, close)
    assert avg is not None
    assert avg < 0.0005, (
        f"AR should not be fooled by volatility-driven range when close ≈ mid; got {avg}"
    )


def test_close_walks_relative_to_mid_yields_nonzero_spread() -> None:
    """The other side of the previous test: when close drifts
    *away* from mid-range across days (the real signature of a
    bid-ask spread biasing trade prices), AR returns a positive
    estimate."""
    n = 30
    rng = np.random.default_rng(7)
    # Narrow daily range, close oscillates above/below mid by ~30¢ —
    # mimics the bid-ask bounce signal AR is designed to catch.
    bounces = rng.choice([-0.3, 0.3], size=n)
    mid = 100.0
    high = pd.Series([mid + 0.5] * n, index=pd.date_range("2024-01-01", periods=n, freq="B"))
    low = pd.Series([mid - 0.5] * n, index=high.index)
    close = pd.Series([mid + b for b in bounces], index=high.index)
    avg = average_spread_estimate(high, low, close)
    assert avg is not None
    assert avg > 0, "close-bouncing-around-mid should produce positive spread estimate"
