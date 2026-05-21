"""PSR / DSR / MinBTL — López de Prado-style statistical significance tests.

Three pieces:

* **PSR** (Probabilistic Sharpe Ratio): probability that the *true* Sharpe
  exceeds a benchmark, given the observed sample's Sharpe and its full
  fourth-moment distribution. PSR > 0.95 → high confidence.

* **DSR** (Deflated Sharpe Ratio): PSR with a higher benchmark that
  accounts for multiple-testing across N candidate strategies. We use the
  spec's simplified deflation:

      SR* = benchmark_sr × √(1 - ρ + N × ρ × (1 - 1/N))

  with ρ ≈ 0.02 (typical correlation among trials in a quant workflow).
  DSR > 0.90 → strategy is real after accounting for the trials run.

* **MinBTL** (Minimum Backtest Length): minimum observations needed for
  the observed Sharpe to be statistically distinguishable from zero at
  ``confidence``, given ``n_trials`` candidates were tested. Inverse of
  PSR with the deflated threshold.

PSR formula (Mertens 2002 / López de Prado 2012):

    PSR(SR*) = Φ((SR̂ - SR*) × √(T - 1) / √(1 - γ₃·SR̂ + (γ₄ - 1)/4 × SR̂²))

where SR̂ and SR* are *per-period* Sharpe ratios, T is the number of
observations, γ₃ is sample skewness, γ₄ is sample raw (Pearson) kurtosis,
and Φ is the standard normal CDF. We accept annualized inputs and
internally convert.
"""
from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np
from scipy.stats import norm

# Typical correlation among trials in a multi-parameter sweep — used by DSR
# to deflate the threshold. Close to López de Prado's published estimate
# (~0.0–0.05 for diversified workflows).
DEFAULT_TRIAL_CORRELATION = 0.02

# Sentinel returned by MinBTL when the observed Sharpe makes the inverse
# undefined (zero or negative). Effectively "never enough data".
_MINBTL_INFINITE = 1_000_000


def probabilistic_sharpe_ratio(
    returns: Sequence[float],
    benchmark_sr: float = 0.0,
    periods_per_year: int = 252,
) -> float:
    """Probability that the *true* Sharpe exceeds ``benchmark_sr``.

    ``returns`` is the per-period return series. ``benchmark_sr`` is
    expressed as an annualized Sharpe (default 0.0 = "any positive
    Sharpe is interesting").
    """
    arr = np.asarray(returns, dtype=float)
    n = len(arr)
    if n < 2:
        return 0.0

    # Per-period Sharpe of the sample.
    mean = float(arr.mean())
    sd = float(arr.std(ddof=1))
    if sd <= 0:
        # Degenerate: no risk. PSR is undefined in the limit; report 0.5
        # if mean is exactly the benchmark, otherwise 0 or 1 by sign.
        bench_per_period = benchmark_sr / math.sqrt(periods_per_year)
        if mean > bench_per_period:
            return 1.0
        if mean < bench_per_period:
            return 0.0
        return 0.5
    sr_hat = mean / sd

    # Convert benchmark to per-period.
    sr_star = benchmark_sr / math.sqrt(periods_per_year)

    # Skewness and raw (Pearson) kurtosis of the sample.
    centered = arr - mean
    m2 = float((centered**2).mean())
    m3 = float((centered**3).mean())
    m4 = float((centered**4).mean())
    if m2 <= 0:
        return 0.5
    skew = m3 / (m2**1.5)
    kurt = m4 / (m2**2)  # raw kurtosis; normal = 3.

    var_term = 1.0 - skew * sr_hat + (kurt - 1.0) / 4.0 * (sr_hat**2)
    var_term = max(var_term, 1e-12)  # guard against pathological samples
    z = (sr_hat - sr_star) * math.sqrt(n - 1) / math.sqrt(var_term)
    return float(norm.cdf(z))


def deflated_sharpe_ratio(
    returns: Sequence[float],
    n_trials: int,
    benchmark_sr: float = 0.0,
    periods_per_year: int = 252,
    *,
    trial_correlation: float = DEFAULT_TRIAL_CORRELATION,
) -> float:
    """PSR with a higher benchmark that accounts for ``n_trials`` candidates.

    With the spec's simplified deflation, the threshold is

        SR* = benchmark_sr × √(1 − ρ + N × ρ × (1 − 1/N))

    so increasing ``n_trials`` raises the threshold and lowers DSR.
    """
    if n_trials <= 0:
        raise ValueError("n_trials must be >= 1")
    if n_trials == 1:
        return probabilistic_sharpe_ratio(returns, benchmark_sr, periods_per_year)
    rho = trial_correlation
    inflation = math.sqrt(1.0 - rho + n_trials * rho * (1.0 - 1.0 / n_trials))
    deflated_threshold = benchmark_sr * inflation
    return probabilistic_sharpe_ratio(returns, deflated_threshold, periods_per_year)


def minimum_backtest_length(
    sharpe: float,
    n_trials: int,
    confidence: float = 0.95,
    periods_per_year: int = 252,
    *,
    trial_correlation: float = DEFAULT_TRIAL_CORRELATION,
) -> int:
    """Minimum number of observations to be ``confidence``-sure the Sharpe is real.

    ``sharpe`` is the *annualized* Sharpe of the candidate strategy (or the
    minimum we want to be able to detect). Output is in *periods*
    (typically trading days; 252 ≈ 1 year). Returns ``_MINBTL_INFINITE``
    when ``sharpe ≤ 0`` (no signal is detectable).
    """
    if sharpe <= 0:
        return _MINBTL_INFINITE
    if not (0.0 < confidence < 1.0):
        raise ValueError("confidence must be in (0, 1)")
    if n_trials < 1:
        raise ValueError("n_trials must be >= 1")

    sr_per_period = sharpe / math.sqrt(periods_per_year)

    # Multiple-testing inflation of the confidence threshold (Bonferroni-like).
    # Same shape used in DSR — keeps the two coherent.
    rho = trial_correlation
    if n_trials == 1:
        adjusted_conf = confidence
    else:
        # Effective per-trial confidence: (1 - α) becomes (1 - α/N_eff).
        # N_eff = N × (1 - ρ × (1 - 1/N)) — fewer effective trials when
        # trials are correlated.
        n_eff = max(1.0, n_trials * (1.0 - rho * (1.0 - 1.0 / n_trials)))
        alpha = (1.0 - confidence) / n_eff
        adjusted_conf = 1.0 - alpha
    z = float(norm.ppf(adjusted_conf))

    # Variance term assuming returns are normal (γ_3 = 0, γ_4 = 3 → (γ_4-1)/4 = 0.5).
    var_term = 1.0 + 0.5 * sr_per_period**2
    if sr_per_period <= 0:
        return _MINBTL_INFINITE
    t_required = 1.0 + (z**2) * var_term / (sr_per_period**2)
    return int(math.ceil(min(t_required, _MINBTL_INFINITE)))


__all__ = [
    "DEFAULT_TRIAL_CORRELATION",
    "deflated_sharpe_ratio",
    "minimum_backtest_length",
    "probabilistic_sharpe_ratio",
]
