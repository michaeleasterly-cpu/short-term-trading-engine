"""Tests for `tpcore.backtest.statistical_significance`."""
from __future__ import annotations

import numpy as np
import pytest

from tpcore.backtest.statistical_significance import (
    deflated_sharpe_ratio,
    minimum_backtest_length,
    probabilistic_sharpe_ratio,
)


# ─── PSR ───────────────────────────────────────────────────────────────────


def test_psr_half_when_observed_exactly_at_benchmark() -> None:
    """SR_obs == benchmark exactly → PSR == 0.5 (no signal either way)."""
    # Symmetric returns whose mean is exactly zero by construction.
    returns = [0.01, -0.01, 0.02, -0.02, 0.005, -0.005] * 10
    assert sum(returns) == 0.0
    psr = probabilistic_sharpe_ratio(returns, benchmark_sr=0.0)
    assert pytest.approx(0.5, abs=0.05) == psr


def test_psr_high_when_observed_sharpe_clearly_above_benchmark() -> None:
    """High Sharpe + many obs → PSR near 1.0."""
    rng = np.random.default_rng(2)
    # Mean 0.001/day, std 0.005/day → daily Sharpe 0.2, annualized ~ 3.17
    returns = list(rng.normal(0.001, 0.005, 1000))
    psr = probabilistic_sharpe_ratio(returns, benchmark_sr=0.0)
    assert psr > 0.99


def test_psr_increases_with_more_observations() -> None:
    """Same per-period Sharpe, more obs → tighter distribution → higher PSR."""
    rng = np.random.default_rng(3)
    short = list(rng.normal(0.001, 0.01, 100))
    long_ = list(rng.normal(0.001, 0.01, 1000))
    p_short = probabilistic_sharpe_ratio(short)
    p_long = probabilistic_sharpe_ratio(long_)
    assert p_long > p_short


def test_psr_with_too_few_returns_returns_zero() -> None:
    """Sample too small → can't make a claim; we report 0."""
    assert probabilistic_sharpe_ratio([0.01]) == 0.0
    assert probabilistic_sharpe_ratio([]) == 0.0


# ─── DSR ───────────────────────────────────────────────────────────────────


def test_dsr_equals_psr_when_n_trials_is_one() -> None:
    """No multiple-testing penalty → DSR == PSR."""
    rng = np.random.default_rng(4)
    returns = list(rng.normal(0.001, 0.005, 500))
    psr = probabilistic_sharpe_ratio(returns, benchmark_sr=0.5)
    dsr = deflated_sharpe_ratio(returns, n_trials=1, benchmark_sr=0.5)
    assert pytest.approx(psr, abs=0.001) == dsr


def test_dsr_decreases_with_more_trials() -> None:
    """More trials → higher SR threshold → lower DSR."""
    rng = np.random.default_rng(5)
    returns = list(rng.normal(0.001, 0.005, 500))
    dsr_1 = deflated_sharpe_ratio(returns, n_trials=1, benchmark_sr=1.0)
    dsr_10 = deflated_sharpe_ratio(returns, n_trials=10, benchmark_sr=1.0)
    dsr_100 = deflated_sharpe_ratio(returns, n_trials=100, benchmark_sr=1.0)
    assert dsr_1 > dsr_10 > dsr_100


def test_dsr_zero_trials_raises() -> None:
    with pytest.raises(ValueError):
        deflated_sharpe_ratio([0.01, 0.02], n_trials=0)


# ─── MinBTL ────────────────────────────────────────────────────────────────


def test_minbtl_is_positive_integer() -> None:
    n = minimum_backtest_length(sharpe=1.0, n_trials=1)
    assert isinstance(n, int)
    assert n > 0


def test_minbtl_increases_with_more_trials() -> None:
    """Multiple-testing penalty → need more obs to be confident."""
    n_1 = minimum_backtest_length(sharpe=1.0, n_trials=1)
    n_10 = minimum_backtest_length(sharpe=1.0, n_trials=10)
    n_100 = minimum_backtest_length(sharpe=1.0, n_trials=100)
    assert n_1 < n_10 < n_100


def test_minbtl_decreases_with_higher_sharpe() -> None:
    """Stronger signal → fewer obs needed."""
    n_low = minimum_backtest_length(sharpe=0.5, n_trials=1)
    n_high = minimum_backtest_length(sharpe=2.0, n_trials=1)
    assert n_high < n_low


def test_minbtl_zero_sharpe_returns_inf_like() -> None:
    """No signal → MinBTL is unbounded; we cap at a sentinel large int."""
    n = minimum_backtest_length(sharpe=0.0, n_trials=1)
    assert n >= 10_000  # effectively "never"


def test_minbtl_negative_sharpe_returns_sentinel() -> None:
    """Strategy is losing → no amount of data 'proves' it works."""
    n = minimum_backtest_length(sharpe=-0.3, n_trials=1)
    assert n >= 10_000
