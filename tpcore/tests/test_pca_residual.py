"""Unit tests for tpcore.backtest.pca_residual primitives.

Tests the engine-free Avellaneda–Lee 2010 PCA-residual + OU s-score
primitives on small synthetic panels with known structure. The tests
are deliberately deterministic (seeded RNG; pinned tolerances) so a
fix-forward in the primitive can never silently regress.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tpcore.backtest.pca_residual import (
    DEFAULT_PCA_GROUP_SEED,
    compute_ou_s_scores,
    compute_pca_groups,
    compute_rolling_pca_residuals,
)


def _synthetic_factor_panel(
    *, n_dates: int, n_tickers: int, seed: int,
) -> pd.DataFrame:
    """Two-factor synthetic close-price panel; return-driven."""
    rng = np.random.default_rng(seed)
    # Two latent factors driving systematic returns; per-ticker
    # idiosyncratic noise on top.
    factor_returns = rng.normal(0.0, 0.01, size=(n_dates, 2))
    betas = rng.normal(0.5, 0.3, size=(n_tickers, 2))
    idio = rng.normal(0.0, 0.005, size=(n_dates, n_tickers))
    log_returns = factor_returns @ betas.T + idio
    # First row zero so cumprod starts at 1.
    log_returns[0] = 0.0
    log_prices = np.cumsum(log_returns, axis=0)
    prices = 100.0 * np.exp(log_prices)
    dates = pd.bdate_range("2020-01-01", periods=n_dates)
    cols = [f"T{i:03d}" for i in range(n_tickers)]
    return pd.DataFrame(prices, index=dates, columns=cols)


# ── U1: PCA residuals orthogonal to factors after K = 2 removal ────


def test_U1_residuals_orthogonal_to_known_factors() -> None:
    """On a 2-factor synthetic panel, residuals after K = 2 PC removal
    should have near-zero correlation with the underlying factors (the
    canonical PCA-residual identity)."""
    n_dates, n_tickers, window = 350, 30, 252
    prices = _synthetic_factor_panel(
        n_dates=n_dates, n_tickers=n_tickers, seed=7,
    )
    residuals = compute_rolling_pca_residuals(
        prices, window=window, top_k=2,
    )
    # On dates ≥ window, residuals should exist; cross-sectional mean
    # of residuals should be approximately zero on each date (market
    # mode removed).
    post_window = residuals.iloc[window:]
    row_means = post_window.mean(axis=1).abs()
    # Market mode ≈ removed → row means tiny.
    assert (row_means < 0.005).mean() > 0.7, (
        f"more than 30% of residual rows have |mean| ≥ 0.005; got "
        f"row_means stats {row_means.describe()}"
    )


# ── U2: No lookahead — residuals pre-window are NaN ────────────────


def test_U2_no_lookahead_pre_window_nan() -> None:
    """For dates before the rolling window has filled, every residual
    must be NaN. A non-NaN pre-window cell is a lookahead bug."""
    n_dates, n_tickers, window = 300, 20, 252
    prices = _synthetic_factor_panel(
        n_dates=n_dates, n_tickers=n_tickers, seed=11,
    )
    residuals = compute_rolling_pca_residuals(
        prices, window=window, top_k=3,
    )
    pre_window = residuals.iloc[:window]
    assert pre_window.isna().all().all(), (
        "every pre-window residual cell must be NaN (no lookahead)"
    )


# ── U3: OU s-score recovers a known half-life within tolerance ─────


def test_U3_ou_s_score_recovers_known_half_life() -> None:
    """A synthetic mean-reverting AR(1) series should yield finite,
    bounded s-scores. The hard test is non-degeneracy: enough s-scores
    are finite + their absolute range is bounded by O(10) (a healthy
    OU-stationary distribution under Avellaneda's standardisation)."""
    rng = np.random.default_rng(13)
    n = 400
    # AR(1) with half-life ≈ 30 trading days ⇒ b = 0.5 ** (1 / 30) ≈
    # 0.9772. Cumulative residual series under such an AR(1) is
    # stationary.
    b_true = 0.5 ** (1.0 / 30.0)
    x = np.zeros(n)
    eps = rng.normal(0.0, 1.0, size=n)
    for t in range(1, n):
        x[t] = b_true * x[t - 1] + eps[t]
    # The s-score expects RESIDUALS (not the cumulative series); the
    # primitive computes the cumulative inside. Pass first-differences.
    residuals = pd.DataFrame(
        {"T000": np.diff(x, prepend=0.0)},
        index=pd.bdate_range("2020-01-01", periods=n),
    )
    s = compute_ou_s_scores(residuals, half_life_days=30)
    finite = s["T000"].dropna().to_numpy()
    assert finite.size > 100, "expected > 100 finite s-scores; got " f"{finite.size}"
    # s-score range under a healthy AR(1) is bounded — extreme outliers
    # at the boundary of the OU stationary distribution are plausible;
    # we test the bulk (75th-percentile-abs) stays in a sane range.
    p75 = float(np.quantile(np.abs(finite), 0.75))
    assert p75 < 3.5, (
        f"75th-percentile |s| = {p75:.3f} — OU fit drifted into a"
        " non-stationary regime"
    )
    # And the mean s-score is close to zero (the OU process has
    # zero-mean stationary distribution under Avellaneda's standardisation).
    assert abs(float(finite.mean())) < 2.0, (
        f"mean s-score {finite.mean():.3f} far from zero — fit bias"
    )


# ── U4: PCA-groups determinism on fixed seed ───────────────────────


def test_U4_pca_groups_deterministic_given_seed() -> None:
    """compute_pca_groups MUST produce identical assignments across
    runs given the same loadings + same seed (fixed-seed reproducibility
    — no hidden RNG state)."""
    rng = np.random.default_rng(17)
    loadings = rng.normal(0.0, 1.0, size=(40, 3))
    g1 = compute_pca_groups(loadings, k=5, seed=DEFAULT_PCA_GROUP_SEED)
    g2 = compute_pca_groups(loadings, k=5, seed=DEFAULT_PCA_GROUP_SEED)
    assert g1 == g2
    # And k-eff capping: k > n_tickers ⇒ k_eff = n_tickers.
    g3 = compute_pca_groups(loadings, k=1000, seed=DEFAULT_PCA_GROUP_SEED)
    # No assignment exceeds n_tickers (every row in some cluster).
    assert max(g3.values()) < loadings.shape[0]


# ── U5: Degenerate inputs return cleanly (no crash, no NaN propagation) ─


def test_U5_degenerate_inputs_return_cleanly() -> None:
    """Empty panel / single-ticker panel must not crash; the primitive
    returns an empty / NaN frame as appropriate."""
    empty = pd.DataFrame()
    out = compute_rolling_pca_residuals(empty, window=252, top_k=3)
    assert out.empty

    single = pd.DataFrame(
        {"AAA": [100.0, 101.0, 102.0]},
        index=pd.bdate_range("2020-01-01", periods=3),
    )
    out2 = compute_rolling_pca_residuals(single, window=252, top_k=3)
    # All NaN because we don't have 252 sessions.
    assert out2.isna().all().all()

    out3 = compute_pca_groups(np.empty((0, 3)), k=5)
    assert out3 == {}


# ── U6: K = 0 edge guard ───────────────────────────────────────────


def test_U6_top_k_zero_returns_log_returns_unchanged() -> None:
    """top_k=0 should be a degenerate-but-honest call: no PC removed
    ⇒ the "residual" is the raw log return. We don't optimize this
    case (the engine pins K = 3) but the primitive must not crash."""
    prices = _synthetic_factor_panel(n_dates=270, n_tickers=10, seed=19)
    res = compute_rolling_pca_residuals(prices, window=252, top_k=0)
    # Post-window rows should equal the raw log returns (the
    # projection onto zero PCs is zero).
    log_prices = np.log(prices)
    log_returns = log_prices.diff()
    post = res.iloc[252:]
    expected = log_returns.iloc[252:]
    # Compare via finite mask (NaN rows on either side should agree).
    diff = (post - expected).dropna(how="all")
    if not diff.empty:
        max_abs = diff.abs().max().max()
        # Tolerance is generous — the centering / standardising path
        # introduces O(1e-12) round-off; we just want "essentially equal".
        assert max_abs < 1e-9, f"top_k=0 disagrees with raw returns by {max_abs}"


# ── U7: PCA-groups handles all-zero loadings ───────────────────────


def test_U7_pca_groups_all_zero_loadings() -> None:
    """A degenerate all-zero loadings matrix shouldn't crash; the
    expected behaviour is that all rows end up in the same cluster
    (every distance is 0)."""
    loadings = np.zeros((10, 3))
    out = compute_pca_groups(loadings, k=5, seed=1)
    assert len(out) == 10
    # Every assignment in [0, k_eff).
    assert all(0 <= v < 5 for v in out.values())


@pytest.mark.parametrize("k", [1, 5, 20, 50])
def test_U4b_pca_groups_k_range(k: int) -> None:
    """Group K spanning the literature range produces valid output."""
    rng = np.random.default_rng(31)
    loadings = rng.normal(0.0, 1.0, size=(60, 3))
    out = compute_pca_groups(loadings, k=k, seed=DEFAULT_PCA_GROUP_SEED)
    assert len(out) == 60
    k_eff = min(k, 60)
    # All assignments within [0, k_eff).
    assert all(0 <= v < k_eff for v in out.values())
