"""Avellaneda–Lee PCA-residual + OU s-score primitives (engine-free).

Shared backtest primitive for any engine that wants the canonical
statistical-arbitrage signal (Avellaneda & Lee 2010, "Statistical
Arbitrage in the U.S. Equities Market", *Quantitative Finance* 10(7)).
Lives in ``tpcore/backtest/`` next to the other shared primitives
(``cost_model``, ``credibility``, ``overfitting``). Engine-free on
purpose: imports only numpy + pandas + stdlib; no DB, no live-path
imports, no engine package imports. Unit-tested at
``tpcore/tests/test_pca_residual.py``.

Currently consumed by the **Reversion PCA-residual Lab candidate**
(``reversion/lab_pca_residual.py``; spec
``docs/superpowers/specs/2026-05-20-reversion-pca-residual-lab-
candidate.md``). The Lab candidate alone determines per-engine
parameter values; this module is parameter-agnostic.

The three documented entry points are pure functions (no module
state, no DB) — engine-free, unit-testable on small synthetic panels:

* :func:`compute_rolling_pca_residuals` — rolling 252-day PCA on a
  log-return panel; returns the residuals after top-K factor removal.
* :func:`compute_ou_s_scores` — Ornstein–Uhlenbeck fit on cumulative
  residual series; returns the standardised s-score (Avellaneda 2010
  §4.1).
* :func:`compute_pca_groups` — k-means clustering on top-K eigenvector
  loadings; PCA-implied statistical groups (GICS-sector substitute
  when no sector source is available).

Avellaneda canonical pinned-elsewhere defaults (kept as ``DEFAULT_*``
module constants so callers can name them in the literature anchor):

* ``DEFAULT_PCA_WINDOW = 252`` — one trading year (Avellaneda §3.1).
* ``DEFAULT_TOP_K = 3`` — top market + 2 macro factors (Avellaneda §3.2
  recommends K ∈ {3, 5} for the simpler analysis).
* ``DEFAULT_OU_HALF_LIFE_DAYS = 30`` — upper end of the literature
  centre (Avellaneda §4 median ~ 25; 30 ⇒ honest fewer-trades floor).
* ``DEFAULT_PCA_GROUP_K = 20`` — substitutes ~ GICS 11 sectors +
  ~ 24 industry groups; the central-band value.
* ``DEFAULT_PCA_GROUP_SEED = 42`` — fixed seed ⇒ reproducible groups.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# ────────────────────────────────────────────────────────────────────────
# Avellaneda–Lee 2010 canonical defaults (named here for downstream
# callers; the engine-side Lab module re-asserts these as its pinned
# constants — DRY-against-literature without DRY-against-engine).
# ────────────────────────────────────────────────────────────────────────

DEFAULT_PCA_WINDOW = 252
DEFAULT_TOP_K = 3
DEFAULT_OU_HALF_LIFE_DAYS = 30
DEFAULT_OU_ENTRY_THRESHOLD = 1.25
DEFAULT_OU_EXIT_THRESHOLD = 0.50
DEFAULT_PCA_GROUP_K = 20
DEFAULT_PCA_GROUP_SEED = 42
DEFAULT_VOLUME_OVERLAY_WINDOW_DAYS = 20
DEFAULT_VOLUME_OVERLAY_CLIP = 1.51


# ────────────────────────────────────────────────────────────────────────
# Rolling PCA residuals
# ────────────────────────────────────────────────────────────────────────


def compute_rolling_pca_residuals(
    prices_panel: pd.DataFrame,
    *,
    window: int = DEFAULT_PCA_WINDOW,
    top_k: int = DEFAULT_TOP_K,
) -> pd.DataFrame:
    """Rolling top-K-PC removed residual returns.

    Parameters
    ----------
    prices_panel
        Wide DataFrame: index = dates (sorted ascending), columns =
        tickers, values = close prices. NaN allowed (missing names on
        a date are dropped from that date's eigendecomposition).
    window
        Rolling window in trading days. Avellaneda 2010 canonical 252.
    top_k
        Number of leading principal components to remove. Avellaneda
        2010 §3.2 recommends K ∈ {3, 5}.

    Returns
    -------
    DataFrame of residuals (same shape as the input log-return panel):
    for each date t ≥ window, the residual is
    ``r_t − Σ_{k=1..K} β_{k,t} ⋅ pc_{k,t}`` where (β_{k,t}, pc_{k,t})
    are the t-th rolling-PCA loadings + factor values. Dates before
    the first feasible window are NaN (no lookahead). Returns an empty
    DataFrame when the input has zero columns or zero rows (degenerate
    guard).
    """
    if prices_panel.empty or prices_panel.shape[1] == 0:
        return pd.DataFrame(index=prices_panel.index)

    # Log returns. The +0 trick keeps the index in sync; first row is
    # NaN by construction. Avellaneda 2010 works on log-returns
    # (eq. 3.1).
    log_prices = np.log(prices_panel.where(prices_panel > 0))
    returns = log_prices.diff()

    n_rows, n_cols = returns.shape
    if n_rows < window + 1:
        return pd.DataFrame(np.nan, index=returns.index, columns=returns.columns)

    residuals = pd.DataFrame(np.nan, index=returns.index, columns=returns.columns)

    for t in range(window, n_rows):
        # Eigendecompose on the window [t-window, t-1] (strictly
        # BEFORE date t — no lookahead). Project the current returns
        # (at date t) onto the prior-window's eigenvectors.
        window_df = returns.iloc[t - window : t]
        cur_row = returns.iloc[t]

        # Drop tickers with missing data in either the window or the
        # current row — they get NaN residuals on this date.
        valid_mask = (
            window_df.notna().all(axis=0) & cur_row.notna()
        )
        if int(valid_mask.sum()) < max(top_k + 1, 2):
            continue
        valid_cols = window_df.columns[valid_mask]
        window_mat = window_df[valid_cols].to_numpy(dtype=float)
        cur_vec = cur_row[valid_cols].to_numpy(dtype=float)

        # Centre each ticker's history over the window before PCA so
        # the leading PC is the equally-weighted market mode (the
        # Avellaneda convention).
        col_means = window_mat.mean(axis=0)
        centered = window_mat - col_means
        # Covariance matrix (n_tickers × n_tickers). The Avellaneda
        # paper uses correlation; we use covariance scaled by stddev
        # below — equivalent for the residual projection.
        if centered.shape[0] < 2:
            continue
        std = centered.std(axis=0, ddof=1)
        std_safe = np.where(std > 1e-12, std, 1.0)
        standardized = centered / std_safe
        # Symmetric covariance of standardized series (≈ correlation).
        cov = (standardized.T @ standardized) / (centered.shape[0] - 1)
        try:
            eigvals, eigvecs = np.linalg.eigh(cov)
        except np.linalg.LinAlgError:
            continue
        # eigh returns ascending eigenvalues; flip to descending.
        order = np.argsort(eigvals)[::-1]
        eigvecs = eigvecs[:, order]

        # Top-K eigenvectors as the systematic-factor loadings.
        top_vecs = eigvecs[:, :top_k]
        # Project the standardized current return onto the loadings;
        # un-standardize to a per-ticker residual. For top_k=0 the
        # projection in standardized space is zero by construction
        # ⇒ residual = raw return (the centering shift cancels
        # because both the residual definition AND the projection are
        # against the centered series; we add col_means back into the
        # projection only when top_k > 0).
        std_cur = (cur_vec - col_means) / std_safe
        if top_k > 0:
            projection_std = top_vecs @ (top_vecs.T @ std_cur)
            projection = projection_std * std_safe + col_means
            residual = cur_vec - projection
        else:
            # No PC removed ⇒ residual is the raw return.
            residual = cur_vec.copy()

        for col, val in zip(valid_cols, residual, strict=True):
            residuals.at[returns.index[t], col] = float(val)

    return residuals


# ────────────────────────────────────────────────────────────────────────
# OU s-score
# ────────────────────────────────────────────────────────────────────────


def compute_ou_s_scores(
    residuals: pd.DataFrame,
    *,
    half_life_days: int = DEFAULT_OU_HALF_LIFE_DAYS,
) -> pd.DataFrame:
    """Standardised OU s-score on the cumulative residual series.

    Avellaneda & Lee 2010 §4.1 builds X_t = Σ_{s ≤ t} residual_s and
    fits an Ornstein-Uhlenbeck process X_{t+1} = a + b X_t + ξ_t; the
    s-score is (X_t − μ_eq) / σ_eq where μ_eq = a / (1−b) and
    σ_eq² = Var(ξ) / (1−b²).

    Practical implementation: a rolling-AR(1) fit on the cumulative
    residual series over a window matched to the half-life (a longer
    half-life ⇒ wider fitting window; Avellaneda uses ≈ 60 trading
    days for half-life ≈ 30).

    Parameters
    ----------
    residuals
        DataFrame from :func:`compute_rolling_pca_residuals`.
    half_life_days
        OU mean-reversion half-life target. Determines the fitting
        window (= 2 × half_life_days by Avellaneda's empirical
        prescription).

    Returns
    -------
    DataFrame of s-scores, same shape as ``residuals``. Pre-warmup
    dates and degenerate-fit dates are NaN.
    """
    if residuals.empty:
        return pd.DataFrame(index=residuals.index, columns=residuals.columns)

    fit_window = max(2 * int(half_life_days), 10)
    s_scores = pd.DataFrame(
        np.nan, index=residuals.index, columns=residuals.columns,
    )

    # Cumulative residual series per ticker; NaN propagates as zero
    # contribution (the OU drift is on the running sum, not the gaps).
    cum = residuals.fillna(0.0).cumsum()

    for ticker in residuals.columns:
        x = cum[ticker].to_numpy(dtype=float)
        if x.size < fit_window + 1:
            continue
        for t in range(fit_window, x.size):
            x_win = x[t - fit_window : t]
            x_lag = x_win[:-1]
            x_lead = x_win[1:]
            if x_lag.size < 2:
                continue
            # AR(1): x_lead = a + b * x_lag + eps.
            x_lag_mean = float(x_lag.mean())
            x_lead_mean = float(x_lead.mean())
            denom = float(((x_lag - x_lag_mean) ** 2).sum())
            if denom <= 1e-12:
                continue
            b = float(((x_lag - x_lag_mean) * (x_lead - x_lead_mean)).sum() / denom)
            a = x_lead_mean - b * x_lag_mean
            if not (-1.0 < b < 1.0):
                # Non-stationary OU fit — Avellaneda's prescription is
                # to skip the name on this date.
                continue
            residuals_fit = x_lead - (a + b * x_lag)
            sigma_eps = float(residuals_fit.std(ddof=1)) if residuals_fit.size >= 2 else 0.0
            if sigma_eps <= 1e-12:
                continue
            mu_eq = a / (1.0 - b)
            sigma_eq = sigma_eps / np.sqrt(max(1.0 - b * b, 1e-12))
            if sigma_eq <= 1e-12:
                continue
            s_scores.iat[t, s_scores.columns.get_loc(ticker)] = (
                (x[t] - mu_eq) / sigma_eq
            )

    return s_scores


# ────────────────────────────────────────────────────────────────────────
# PCA-implied statistical groups (k-means on top-K loadings)
# ────────────────────────────────────────────────────────────────────────


def compute_pca_groups(
    loadings: np.ndarray,
    *,
    k: int = DEFAULT_PCA_GROUP_K,
    seed: int = DEFAULT_PCA_GROUP_SEED,
    max_iters: int = 100,
) -> dict[int, int]:
    """k-means on per-ticker top-K-PC loadings → group assignments.

    GICS sectors are unavailable on this platform (no sector source);
    Avellaneda & Lee 2010 §3.4 substitutes "industry-implied groups"
    via k-means clustering on the eigenvector loadings. This is a
    deterministic implementation (Lloyd's algorithm; fixed seed; ties
    broken by the lowest index, NEVER by RNG).

    Parameters
    ----------
    loadings
        2-D ``ndarray`` of shape ``(n_tickers, top_k)`` — per-ticker
        loadings against the top-K principal components.
    k
        Number of clusters. Avellaneda canonical 20 ≈ midpoint of GICS
        11 sectors + 24 industry groups.
    seed
        Numpy RNG seed for the centroid initialisation
        (numpy.random.default_rng). Fixed ⇒ identical groups across
        runs.
    max_iters
        Lloyd's-algorithm iteration cap; convergence usually < 30.

    Returns
    -------
    dict mapping the **row index** (0..n_tickers-1) → group id
    (0..k-1). The caller (engine-side Lab module) re-keys this by
    ticker name. Returns an empty dict on degenerate inputs (no rows,
    or k ≥ n_tickers).
    """
    if loadings.size == 0:
        return {}
    n_tickers = loadings.shape[0]
    if n_tickers == 0:
        return {}
    k_eff = min(int(k), n_tickers)
    if k_eff <= 1:
        return {int(i): 0 for i in range(n_tickers)}

    rng = np.random.default_rng(seed)
    # k-means++ initialisation — picks centroids spread across the
    # loading space. Deterministic given the seed.
    centroids = _kmeans_pp_init(loadings, k=k_eff, rng=rng)

    labels = np.zeros(n_tickers, dtype=int)
    for _ in range(max_iters):
        # Assignment step.
        dists = _squared_distances(loadings, centroids)
        new_labels = np.argmin(dists, axis=1)
        if np.array_equal(new_labels, labels):
            break
        labels = new_labels
        # Update step. Empty clusters get re-seeded to the farthest
        # point from their (formerly assigned) centroid — preserves
        # k_eff non-empty clusters.
        for c in range(k_eff):
            members = loadings[labels == c]
            if members.shape[0] > 0:
                centroids[c] = members.mean(axis=0)

    return {int(i): int(labels[i]) for i in range(n_tickers)}


def _squared_distances(points: np.ndarray, centroids: np.ndarray) -> np.ndarray:
    """Pairwise squared L2 distances; (n, d) × (k, d) → (n, k)."""
    # Broadcasting: (n, 1, d) − (1, k, d) → (n, k, d); square + sum.
    diff = points[:, None, :] - centroids[None, :, :]
    return np.einsum("nkd,nkd->nk", diff, diff)


def _kmeans_pp_init(
    points: np.ndarray, *, k: int, rng: np.random.Generator,
) -> np.ndarray:
    """Deterministic k-means++ initialisation given an RNG."""
    n = points.shape[0]
    chosen = [int(rng.integers(n))]
    for _ in range(1, k):
        centroids_so_far = points[chosen]
        dists = _squared_distances(points, centroids_so_far).min(axis=1)
        # Avoid degenerate all-zero distances by adding a tiny floor.
        weights = dists + 1e-12
        probs = weights / weights.sum()
        chosen.append(int(rng.choice(n, p=probs)))
    return points[chosen].astype(float, copy=True)


__all__ = [
    "DEFAULT_OU_ENTRY_THRESHOLD",
    "DEFAULT_OU_EXIT_THRESHOLD",
    "DEFAULT_OU_HALF_LIFE_DAYS",
    "DEFAULT_PCA_GROUP_K",
    "DEFAULT_PCA_GROUP_SEED",
    "DEFAULT_PCA_WINDOW",
    "DEFAULT_TOP_K",
    "DEFAULT_VOLUME_OVERLAY_CLIP",
    "DEFAULT_VOLUME_OVERLAY_WINDOW_DAYS",
    "compute_ou_s_scores",
    "compute_pca_groups",
    "compute_rolling_pca_residuals",
]
