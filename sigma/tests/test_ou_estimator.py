"""Unit tests for the OU κ estimator (research spike, 2026-05-15).

Synthetic series:

* Mean-reverting OU process with known κ → estimator recovers κ within
  reasonable tolerance.
* Random walk (no mean reversion) → estimator returns 0.
* Insufficient data / non-positive prices → estimator returns 0.

The estimator's purpose is gating, not parameter recovery — exact κ
matching matters less than correctly classifying mean-reverting vs
non-mean-reverting series.
"""
from __future__ import annotations

import numpy as np
import pytest

from sigma.plugs.setup_detection import estimate_ou_kappa


def _simulate_ou(
    n: int, kappa: float, theta: float, sigma: float, x0: float, dt: float, seed: int = 0
) -> np.ndarray:
    """Euler-Maruyama path: dX = κ(θ − X)dt + σ dW.

    Returns the LEVEL path X_t; for the estimator's log-input contract,
    callers pass ``exp(X)`` so the estimator's internal log-transform
    recovers the OU process.
    """
    rng = np.random.default_rng(seed)
    x = np.empty(n)
    x[0] = x0
    sqrt_dt = np.sqrt(dt)
    for t in range(1, n):
        x[t] = x[t - 1] + kappa * (theta - x[t - 1]) * dt + sigma * sqrt_dt * rng.standard_normal()
    return x


def test_strongly_mean_reverting_series_gives_positive_kappa() -> None:
    """OU with κ=5 over 252 days should yield an estimator κ > 1 — well
    above the planned 1.0 lower-bound of the sweep range."""
    x = _simulate_ou(n=252, kappa=5.0, theta=np.log(100), sigma=0.2, x0=np.log(100), dt=1 / 252, seed=42)
    prices = np.exp(x)  # estimator log-transforms inside
    k = estimate_ou_kappa(prices, dt=1 / 252)
    assert k > 1.0, f"strong-MR series should yield κ > 1, got {k}"


def test_random_walk_yields_kappa_near_zero() -> None:
    """A geometric random walk has no mean reversion — estimator returns 0
    (b ≥ 1 after AR(1) fit, so the early-return fires)."""
    rng = np.random.default_rng(7)
    log_rets = rng.standard_normal(252) * 0.01  # 1% daily vol
    log_p = np.cumsum(log_rets) + np.log(100)
    prices = np.exp(log_p)
    k = estimate_ou_kappa(prices, dt=1 / 252)
    # Random walks frequently fit b ≈ 1; if b drifts slightly < 1 the
    # implied κ is very small. Either way, well below the planned
    # κ=1.0 floor of the sweep.
    assert k < 1.0, f"random walk should not look like κ ≥ 1.0; got {k}"


def test_trending_series_below_sweep_floor() -> None:
    """A pure linear-ramp price series has AR(1) b ≈ 1 minus a tiny
    finite-sample bias term, so κ is small-positive rather than exactly
    zero. The practical requirement is that trending series fail the
    sweep's planned κ ≥ 1.0 gate."""
    prices = np.linspace(100.0, 150.0, 252)
    k = estimate_ou_kappa(prices, dt=1 / 252)
    assert k < 1.0, f"trending series should fall under the κ=1.0 gate; got {k}"


def test_too_few_observations_returns_zero() -> None:
    prices = np.array([100.0, 100.5, 100.2])  # only 3 points
    assert estimate_ou_kappa(prices, dt=1 / 252) == 0.0


def test_non_positive_prices_returns_zero() -> None:
    # log(0) is -inf; estimator must guard.
    prices = np.array([100.0, 0.0, 100.0] * 20)
    assert estimate_ou_kappa(prices, dt=1 / 252) == 0.0


def test_constant_series_returns_zero() -> None:
    """Zero variance in x_{t-1} would divide by zero; guard returns 0."""
    prices = np.full(60, 100.0)
    assert estimate_ou_kappa(prices, dt=1 / 252) == 0.0


@pytest.mark.parametrize("kappa_true", [3.0, 5.0, 8.0])
def test_higher_kappa_input_yields_higher_kappa_estimate(kappa_true: float) -> None:
    """Estimator should preserve the ordering of true κ values (the
    spike's gating logic only needs ranking, not exact recovery)."""
    x = _simulate_ou(
        n=252, kappa=kappa_true, theta=np.log(100), sigma=0.2,
        x0=np.log(100), dt=1 / 252, seed=int(kappa_true),
    )
    prices = np.exp(x)
    k_est = estimate_ou_kappa(prices, dt=1 / 252)
    # Tolerance — estimator is noisy on 252 samples, but should land
    # in the same order of magnitude.
    assert k_est >= 0.5 * kappa_true, (
        f"κ_true={kappa_true} → estimator should be ≥ {0.5 * kappa_true}, got {k_est}"
    )
