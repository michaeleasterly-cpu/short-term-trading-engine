"""Monte Carlo sequence stress test via moving-block bootstrap.

The test answers: "is this strategy's observed Sharpe distinguishable
from random reordering of the same trades?" Implementation:

1. Take the per-trade return stream r_1..r_n.
2. Generate ``n_simulations`` resampled sequences using a moving-block
   bootstrap with ``block_size`` consecutive trades per block. Block
   length 5 preserves intra-week serial correlation in trade outcomes.
3. For each sequence, walk the equity curve and record:
   - terminal Sharpe ratio,
   - max drawdown,
   - whether equity ever dropped below ``ruin_threshold × starting``.
4. Compare the *observed* sequence's Sharpe to the simulated null
   distribution. ``observed_sharpe_percentile`` is the fraction of
   simulations with worse Sharpe than the observed sequence — a value
   ≥ 0.90 means observed is in the top decile (statistically distinct
   from a randomly-reordered null).

The fan chart is the 10th, 50th, and 90th percentile of cumulative
equity at each trade index, useful for plotting bands of plausible
outcomes given the trade-level distribution.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

import numpy as np


SHARPE_NULL_DECISION_THRESHOLD = 0.90  # observed must beat this fraction of nulls


@dataclass(frozen=True)
class MCResult:
    """Output of `monte_carlo_sequence_test`."""

    n_simulations: int
    observed_sharpe: float
    observed_sharpe_percentile: float  # fraction of nulls the observed beats
    probability_of_ruin: float  # fraction of nulls that crashed below threshold
    worst_drawdown_p95: float  # 95th-percentile worst drawdown across nulls
    fan_chart: dict[str, list[float]]  # 'p10', 'p50', 'p90' equity curves

    @property
    def observed_is_significant(self) -> bool:
        """True iff observed Sharpe sits in the top decile of nulls."""
        return self.observed_sharpe_percentile >= SHARPE_NULL_DECISION_THRESHOLD


def monte_carlo_sequence_test(
    trades: list[dict],
    *,
    n_simulations: int = 1000,
    block_size: int = 5,
    ruin_threshold: float = 0.5,
    return_key: str = "return_pct",
    seed: int | None = None,
) -> MCResult:
    """Bootstrap-resample ``trades`` and build a null distribution of Sharpes.

    Each trade dict must have ``return_key`` (default ``"return_pct"``).
    Returns are treated as *fractional* (0.02 = +2%).
    """
    if not trades:
        raise ValueError("trades must be non-empty")
    returns = np.asarray([float(t[return_key]) for t in trades], dtype=float)
    n = len(returns)
    if block_size < 1:
        raise ValueError("block_size must be >= 1")
    if n < block_size:
        raise ValueError(
            f"need at least {block_size} trades for a block-size-{block_size} bootstrap; got {n}"
        )

    rng = np.random.default_rng(seed)
    observed_sharpe = _sharpe(returns)

    null_sharpes = np.empty(n_simulations, dtype=float)
    null_drawdowns = np.empty(n_simulations, dtype=float)
    null_ruined = np.zeros(n_simulations, dtype=bool)
    # fan-chart needs every simulation's full equity curve. With n trades and
    # n_simulations sims, that's n_simulations × (n + 1) floats. For the typical
    # caller (n ~ 60, sims ~ 1000) this is small.
    equity_curves = np.empty((n_simulations, n + 1), dtype=float)

    for s in range(n_simulations):
        sequence = _block_resample(returns, block_size=block_size, rng=rng)
        equity = _equity_curve(sequence)
        equity_curves[s] = equity
        null_sharpes[s] = _sharpe(sequence)
        null_drawdowns[s] = _max_drawdown(equity)
        null_ruined[s] = bool(equity.min() < ruin_threshold)

    observed_percentile = float((null_sharpes < observed_sharpe).mean())
    probability_of_ruin = float(null_ruined.mean())
    worst_drawdown_p95 = float(np.percentile(null_drawdowns, 5))  # most negative DD in worst 5%

    fan_chart = {
        "p10": [float(x) for x in np.percentile(equity_curves, 10, axis=0)],
        "p50": [float(x) for x in np.percentile(equity_curves, 50, axis=0)],
        "p90": [float(x) for x in np.percentile(equity_curves, 90, axis=0)],
    }

    return MCResult(
        n_simulations=n_simulations,
        observed_sharpe=observed_sharpe,
        observed_sharpe_percentile=observed_percentile,
        probability_of_ruin=probability_of_ruin,
        worst_drawdown_p95=worst_drawdown_p95,
        fan_chart=fan_chart,
    )


# ─── Internals ─────────────────────────────────────────────────────────────


def _block_resample(
    returns: np.ndarray, *, block_size: int, rng: np.random.Generator
) -> np.ndarray:
    """Moving-block bootstrap: pick random blocks and concatenate to length n."""
    n = len(returns)
    n_blocks = math.ceil(n / block_size)
    # Valid starting indices for a block of size `block_size`.
    max_start = n - block_size
    starts = rng.integers(0, max_start + 1, size=n_blocks)
    out = np.empty(n_blocks * block_size, dtype=returns.dtype)
    for i, s in enumerate(starts):
        out[i * block_size : (i + 1) * block_size] = returns[s : s + block_size]
    return out[:n]


def _sharpe(returns: np.ndarray) -> float:
    if len(returns) < 2:
        return 0.0
    sd = float(returns.std(ddof=1))
    if sd <= 0:
        # All returns equal — Sharpe is ±inf in the limit; return a large finite
        # signed magnitude so percentile math still works without poisoning np ops.
        mean = float(returns.mean())
        if mean > 0:
            return 1e6
        if mean < 0:
            return -1e6
        return 0.0
    return float(returns.mean() / sd)


def _equity_curve(returns: np.ndarray) -> np.ndarray:
    """Compounded equity curve starting at 1.0, after each trade."""
    growth = 1.0 + returns
    equity = np.empty(len(returns) + 1, dtype=float)
    equity[0] = 1.0
    equity[1:] = np.cumprod(growth)
    return equity


def _max_drawdown(equity: np.ndarray) -> float:
    """Max drawdown as a negative fraction (e.g. -0.25 = 25% drawdown)."""
    peak = np.maximum.accumulate(equity)
    dd = (equity - peak) / peak
    return float(dd.min())


__all__ = [
    "MCResult",
    "SHARPE_NULL_DECISION_THRESHOLD",
    "monte_carlo_sequence_test",
]
