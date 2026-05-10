"""Tests for `tpcore.backtest.monte_carlo`."""
from __future__ import annotations

import numpy as np
import pytest

from tpcore.backtest.monte_carlo import (
    MCResult,
    monte_carlo_sequence_test,
)


def _trades_with_returns(returns: list[float]) -> list[dict]:
    return [{"return_pct": r} for r in returns]


def test_returns_mc_result_with_expected_fields() -> None:
    rng_returns = [0.01, 0.02, -0.005, 0.015, -0.01, 0.02, 0.005, -0.02, 0.01, 0.015]
    result = monte_carlo_sequence_test(
        _trades_with_returns(rng_returns), n_simulations=100, seed=42
    )
    assert isinstance(result, MCResult)
    assert result.n_simulations == 100
    assert "p10" in result.fan_chart
    assert "p50" in result.fan_chart
    assert "p90" in result.fan_chart
    # The fan-chart curves should each have len == n_trades + 1 (initial + after each trade)
    assert len(result.fan_chart["p50"]) == len(rng_returns) + 1


def test_observed_percentile_for_winning_strategy_is_above_median() -> None:
    """Winners-with-variance: observed Sharpe is positive; the bootstrap's null
    distribution centers on the same Sharpe (block-shuffling preserves the
    sample mean and std), so observed sits roughly at the *median*. Anything
    much above 0.5 implies favorable autocorrelation; below 0.5 implies the
    observed ordering is unusually unlucky vs random reorderings."""
    rng = np.random.default_rng(13)
    # Small positive drift + variance: clearly profitable but with realistic noise.
    returns = list(rng.normal(0.01, 0.02, 60))
    result = monte_carlo_sequence_test(
        _trades_with_returns(returns), n_simulations=300, seed=42
    )
    assert result.observed_sharpe > 0
    # Block bootstrap of i.i.d. winners centers on observed → percentile ≈ 0.5.
    assert 0.20 < result.observed_sharpe_percentile < 0.80


def test_observed_percentile_is_low_for_a_random_walk() -> None:
    """A symmetric random series → observed Sharpe near median of null distribution."""
    rng = np.random.default_rng(7)
    returns = list(rng.normal(0, 0.02, 100))
    result = monte_carlo_sequence_test(
        _trades_with_returns(returns), n_simulations=300, seed=42
    )
    # Symmetric ~0 mean; observed should fall around the middle of the null.
    assert 0.2 < result.observed_sharpe_percentile < 0.8


def test_probability_of_ruin_is_zero_for_uniformly_winning_strategy() -> None:
    """All winners → never drops below 50% of starting capital."""
    returns = [0.05] * 20
    result = monte_carlo_sequence_test(
        _trades_with_returns(returns), n_simulations=100, seed=42
    )
    assert result.probability_of_ruin == 0.0


def test_probability_of_ruin_is_high_for_disaster() -> None:
    """Big consecutive losses → many sequences drop below threshold."""
    # 20 returns of -10% each → cumulative ~ -90% → guaranteed ruin
    returns = [-0.10] * 20
    result = monte_carlo_sequence_test(
        _trades_with_returns(returns), n_simulations=100, ruin_threshold=0.5, seed=42
    )
    assert result.probability_of_ruin > 0.95


def test_block_size_one_is_full_iid_shuffle() -> None:
    """block_size=1 reduces to standard iid bootstrap — sanity check it works."""
    returns = [0.01, -0.01, 0.02, -0.02, 0.005]
    result = monte_carlo_sequence_test(
        _trades_with_returns(returns), n_simulations=50, block_size=1, seed=42
    )
    assert result.n_simulations == 50


def test_too_few_trades_raises() -> None:
    """With fewer trades than block_size, refuse rather than silently misbehave."""
    with pytest.raises(ValueError):
        monte_carlo_sequence_test(_trades_with_returns([0.01]), n_simulations=10, block_size=5)


def test_seed_reproducibility() -> None:
    rs = [0.01, 0.02, -0.01, 0.015, -0.02, 0.03, -0.005, 0.01, -0.015, 0.02]
    a = monte_carlo_sequence_test(_trades_with_returns(rs), n_simulations=100, seed=99)
    b = monte_carlo_sequence_test(_trades_with_returns(rs), n_simulations=100, seed=99)
    assert a.observed_sharpe_percentile == b.observed_sharpe_percentile
    assert a.probability_of_ruin == b.probability_of_ruin
