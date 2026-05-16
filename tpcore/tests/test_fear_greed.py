"""Tests for the pure Fear & Greed indicator.

Covers component calculations, composite weighting, label mapping,
direction logic + dead-band, 5d-ago shift, and clip behaviour.

Fixtures use HY OAS *with variance* (a perfectly-flat series has
rolling std=0 → z-score legitimately undefined → credit NaN; real HY
data always varies). Assertions that depend on asymptotic clip or
rolling z use tolerances; exact-math assertions stay exact.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from tpcore.indicators.fear_greed import _label, compute_fear_greed


def _series(vals, start="2018-01-01"):
    idx = pd.date_range(start, periods=len(vals), freq="D")
    return pd.Series(vals, index=idx, dtype="float64")


def _hy(n: int, *, last: float | None = None) -> pd.Series:
    """HY OAS as a smooth linear ramp 3.0→5.0 — std>0 (so the rolling
    z-score is defined) and the per-window z is ~constant (no day-to-day
    credit noise), which is what the composite/direction tests need.
    Optionally pin the final value (z-direction tests)."""
    base = np.linspace(3.0, 5.0, n)
    if last is not None:
        base[-1] = last
    return _series(list(base))


def test_label_boundaries() -> None:
    assert _label(24.9) == "Extreme Fear"
    assert _label(25.0) == "Fear"
    assert _label(44.9) == "Fear"
    assert _label(45.0) == "Neutral"
    assert _label(54.9) == "Neutral"
    assert _label(55.0) == "Greed"
    assert _label(74.9) == "Greed"
    assert _label(75.0) == "Extreme Greed"


def test_composite_weighting_is_exact() -> None:
    """score == round(0.30·vol + 0.30·credit + 0.25·mom + 0.15·safe, 1)
    — verify the actual weighting against the returned components,
    independent of any contrived value."""
    n = 800
    last = compute_fear_greed(
        _series([20.0] * n), _hy(n), _series([400.0] * n),
        _series([1.0] * n),
    ).iloc[-1]
    expected = round(
        0.30 * last["volatility_component"]
        + 0.30 * last["credit_component"]
        + 0.25 * last["momentum_component"]
        + 0.15 * last["safe_haven_component"],
        1,
    )
    assert abs(last["score"] - expected) < 0.05
    # vol/mom/safe are exact at these flat inputs
    assert abs(last["volatility_component"] - 50.0) < 1e-6
    assert abs(last["momentum_component"] - 50.0) < 1e-6
    assert abs(last["safe_haven_component"] - 50.0) < 1e-6
    assert last["label"] == _label(float(last["score"]))


def test_safe_haven_clip_boundaries_exact() -> None:
    n = 800
    vix, hy, sp = _series([20.0] * n), _hy(n), _series([400.0] * n)
    assert compute_fear_greed(vix, hy, sp, _series([-2.0] * n)
                              ).iloc[-1]["safe_haven_component"] == 0.0
    assert compute_fear_greed(vix, hy, sp, _series([5.0] * n)
                              ).iloc[-1]["safe_haven_component"] == 100.0


def test_volatility_is_high_when_vix_collapses() -> None:
    n = 800
    vix = _series([40.0] * (n - 1) + [1.0])  # far below its 50dma
    out = compute_fear_greed(vix, _hy(n), _series([400.0] * n),
                             _series([1.0] * n))
    v = out.iloc[-1]["volatility_component"]
    assert 95.0 < v <= 100.0   # asymptotic clip — never exactly 100


def test_5d_shift_and_direction() -> None:
    n = 800
    vix, hy, t = _series([20.0] * n), _hy(n), _series([1.0] * n)
    sp_vals = [400.0] * (n - 6) + [400, 408, 416, 424, 432, 440]
    out = compute_fear_greed(vix, hy, _series(sp_vals), t)
    last = out.iloc[-1]
    assert last["score_5d_ago"] == out["score"].iloc[-6]   # exact shift
    assert last["direction"] == "rising"                   # momentum jump
    # row 780: full 756d history present AND still in the flat pre-ramp
    # region (ramp is the last 6 rows) → score constant → flat
    assert out.iloc[780]["direction"] == "flat"


def test_insufficient_history_is_nan_not_crash() -> None:
    out = compute_fear_greed(
        _series([20.0] * 10), _hy(10), _series([400.0] * 10),
        _series([1.0] * 10),
    )
    assert out["credit_component"].isna().all()
    assert out["score"].isna().all()


def test_credit_widening_drives_fear() -> None:
    n = 800
    out = compute_fear_greed(
        _series([20.0] * n), _hy(n, last=25.0),   # HY spike → z≫0
        _series([400.0] * n), _series([1.0] * n),
    )
    assert out.iloc[-1]["credit_component"] == 0.0
    assert out.iloc[-1]["score"] < 45             # composite → Fear


def test_returns_expected_columns() -> None:
    out = compute_fear_greed(
        _series([20.0] * 800), _hy(800),
        _series([400.0] * 800), _series([1.0] * 800),
    )
    assert list(out.columns) == [
        "score", "label", "direction", "score_5d_ago",
        "volatility_component", "credit_component",
        "momentum_component", "safe_haven_component",
    ]
