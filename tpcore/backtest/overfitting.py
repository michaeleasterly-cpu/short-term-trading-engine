"""Overfitting-detection diagnostic suite.

Runs as a post-processing step after any engine backtest. Computes nine
overfitting tests and emits a structured :class:`OverfittingReport` whose
contents gate live-graduation via :class:`tpcore.backtest.credibility.
BacktestCredibilityRubric`.

The nine tests:

1. **DSR** — Deflated Sharpe Ratio (Bailey & López de Prado, 2014).
2. **PSR @ 0** — probability the *true* Sharpe exceeds zero.
3. **PBO** — Probability of Backtest Overfitting via CSCV.
4. **MinBTL** — Minimum Backtest Length required to detect the observed
   Sharpe at 95% confidence given ``n_trials`` parameter combinations.
5. **Sensitivity sweep** — perturb each parameter ±10/±25%, see how
   Sharpe degrades.
6. **Monte Carlo sequence stress** — moving-block bootstrap of trades.
7. **Noise infusion** — add 0.5% Gaussian noise to bar prices, see how
   strategy degrades.
8. **Regime coverage** — flag if a single market regime contributes
   > 60% of P&L.
9. **Trades-per-parameter ratio** — at least 10 trades per parameter.

Tests that lack required inputs (``strategy_fn``, ``price_data``,
``trial_returns_matrix``) are skipped gracefully — never raise.

The PSR/DSR/MinBTL math is implemented in this module rather than reused
from :mod:`tpcore.backtest.statistical_significance` because the latter
operates on annualized inputs while overfitting diagnostics operate in
*per-trade* (event-time) space. The two are mathematically consistent;
keeping them separate avoids unit-confusion bugs at the call site.

CSCV/PBO is implemented from scratch (Bailey, Borwein, López de Prado &
Zhu 2014, "The probability of backtest overfitting").
"""
from __future__ import annotations

import math
from collections.abc import Callable
from datetime import datetime
from itertools import combinations
from typing import Any
from uuid import UUID, uuid4

import numpy as np
import pandas as pd
import structlog
from pydantic import BaseModel, ConfigDict, Field
from scipy.stats import norm

logger = structlog.get_logger(__name__)


# ─── thresholds (per spec) ─────────────────────────────────────────────────

DSR_PASS_THRESHOLD = 0.95
PSR_AT_ZERO_PASS = 0.80
PBO_PASS_THRESHOLD = 0.50
TRADES_PER_PARAM_MIN = 10
SENSITIVITY_ROBUST = 0.80
SENSITIVITY_FRAGILE = 0.50
MC_PERCENTILE_PASS = 0.90  # observed must be in top decile of null
NOISE_ROBUST_PCT = 0.20
NOISE_FRAGILE_PCT = 0.50
REGIME_OVERCONCENTRATION_PCT = 0.60

EULER_MASCHERONI = 0.5772156649015329
DEFAULT_NOISE_SIGMA = 0.005  # 0.5% of price


# ─── PSR / DSR / MinBTL — per-trade space ─────────────────────────────────


def _per_trade_sharpe(returns: np.ndarray) -> float:
    if returns.size < 2:
        return 0.0
    sd = float(returns.std(ddof=1))
    if sd <= 0:
        return 0.0
    return float(returns.mean() / sd)


def _moments(returns: np.ndarray) -> tuple[float, float]:
    """Sample skewness and *raw* (Pearson) kurtosis. Normal kurt = 3."""
    if returns.size < 4:
        return 0.0, 3.0
    centered = returns - returns.mean()
    m2 = float((centered**2).mean())
    m3 = float((centered**3).mean())
    m4 = float((centered**4).mean())
    if m2 <= 0:
        return 0.0, 3.0
    return m3 / (m2**1.5), m4 / (m2**2)


def _psr_per_trade(
    sr: float, sr_threshold: float, n: int, skew: float, kurt: float
) -> float:
    """Probability that the *true* per-trade Sharpe exceeds ``sr_threshold``."""
    if n < 2:
        return 0.0
    var_term = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * (sr**2)
    var_term = max(var_term, 1e-12)
    z = (sr - sr_threshold) * math.sqrt(n - 1) / math.sqrt(var_term)
    return float(norm.cdf(z))


MIN_TRIALS_FOR_V = 5  # H-A2-10: advisory for CALLERS — below this the
                      # cross-trial variance is too noisy to trust as a
                      # selection-bias estimate, so callers (e.g.
                      # OverfittingDiagnostic._trial_sharpe_variance /
                      # compute_dsr_for_verdict) must pass
                      # trial_sharpe_variance=None.
                      # _expected_max_sharpe_under_null itself does NOT
                      # enforce this guard; it applies only the floor.


def _expected_max_sharpe_under_null(
    n_trials: int,
    n_obs: int,
    *,
    trial_sharpe_variance: float | None = None,
) -> float:
    """Expected max sample Sharpe across ``n_trials`` trials under the null.

    Bailey & López de Prado (2014), SSRN 2460551, eqn for SR₀:
        SR₀ = √V · ((1−γ)·Φ⁻¹[1−1/N] + γ·Φ⁻¹[1−1/(N·e)])
    where **V = V[ŜR_n] is the cross-trial variance of the per-trial
    Sharpe estimates across the N searched trials** (selection-bias
    dispersion), NOT the single-estimator sampling variance.

    ``trial_sharpe_variance`` — pass V[ŜR_n] computed from the sweep's
    per-trial Sharpe vector (the statistically-correct path). When
    ``None`` (a count-only / single-strategy caller that has no trial
    vector), fall back to the single-estimator null approximation
    ``1/(n_obs-1)`` AND emit a structlog WARNING — this branch is a
    documented approximation, never silent (§1.3, H-A2-1).

    The H-A2-10 floor ``max(V, 1/(n_obs-1))`` makes the change provably
    tightening-or-equal for every input: a low-dispersion / degenerate
    sweep can NOT loosen the (already-too-lenient) legacy bar. See the
    sibling impl ``ops/lab/run.py::compute_dsr_for_verdict`` — both must
    stay coherent (H-A2-7); the V-term is the cross-trial dispersion,
    ``ddof=1``, distinct from the multiple-testing count ``n_trials``.
    """
    if n_trials <= 1 or n_obs < 2:
        return 0.0
    floor = 1.0 / (n_obs - 1)  # legacy single-estimator value — now a FLOOR
    if trial_sharpe_variance is not None:
        # H-A2-10: honest cross-trial dispersion is used ONLY when it makes
        # the gate the SAME OR HARDER. A low-dispersion / degenerate sweep
        # (V < 1/(n_obs-1)) must NOT loosen the bar — clamp up to the floor.
        # anti-laundering floor: NOT max(V,0.0) — V=0 collapses SR0->0,
        # DSR->~1, spuriously clearing the 0.95 gate (T-STRICTER guards this).
        sr_variance = max(float(trial_sharpe_variance), floor)
    else:
        sr_variance = floor  # KNOWN APPROXIMATION — not the paper's V
        logger.warning(
            "tpcore.overfitting.dsr.null_variance_approximation",
            reason="no per-trial Sharpe vector available; using "
                   "single-estimator 1/(n_obs-1) instead of "
                   "cross-trial V[SR_n]",
            n_trials=n_trials,
            n_obs=n_obs,
        )
    z1 = float(norm.ppf(1.0 - 1.0 / n_trials))
    z2 = float(norm.ppf(1.0 - 1.0 / (n_trials * math.e)))
    return math.sqrt(sr_variance) * (
        (1.0 - EULER_MASCHERONI) * z1 + EULER_MASCHERONI * z2
    )


def _deflated_sharpe_ratio(
    sr: float,
    n: int,
    skew: float,
    kurt: float,
    n_trials: int,
    *,
    trial_sharpe_variance: float | None = None,
) -> float:
    threshold = _expected_max_sharpe_under_null(
        n_trials, n, trial_sharpe_variance=trial_sharpe_variance
    )
    return _psr_per_trade(sr, threshold, n, skew, kurt)


def _min_btl_trades(
    sr: float, n_trials: int, skew: float, kurt: float, confidence: float = 0.95
) -> int:
    """Minimum trades to detect ``sr`` (per-trade) at ``confidence`` after ``n_trials``."""
    if sr <= 0:
        return 1_000_000
    var_term = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * (sr**2)
    var_term = max(var_term, 1e-12)
    if n_trials <= 1:
        z = float(norm.ppf(confidence))
    else:
        alpha = 1.0 - confidence
        z1 = float(norm.ppf(1.0 - alpha / n_trials))
        z2 = float(norm.ppf(1.0 - alpha / (n_trials * math.e)))
        z = (1.0 - EULER_MASCHERONI) * z1 + EULER_MASCHERONI * z2
    n_min = 1.0 + var_term * (z / sr) ** 2
    return int(math.ceil(min(n_min, 1_000_000)))


# ─── CSCV / PBO ────────────────────────────────────────────────────────────


def cscv_pbo(returns_matrix: np.ndarray | pd.DataFrame, n_splits: int = 16) -> float:
    """Probability of Backtest Overfitting via Combinatorially Symmetric CV.

    Bailey, Borwein, López de Prado & Zhu (2014).

    Args:
        returns_matrix: T × N array. T observations, N candidate strategies.
        n_splits: S, must be even and ≤ T.

    Returns:
        PBO ∈ [0, 1]. Below ~0.05: unlikely overfit. Above 0.50: more
        likely overfit than not.
    """
    arr = returns_matrix.values if isinstance(returns_matrix, pd.DataFrame) else np.asarray(returns_matrix)
    if arr.ndim != 2:
        raise ValueError("returns_matrix must be 2D (T × N)")
    t, n_strats = arr.shape
    if n_strats < 2:
        raise ValueError("need at least 2 strategies for CSCV")
    if n_splits < 2 or n_splits % 2 != 0:
        raise ValueError("n_splits must be an even integer ≥ 2")
    if t < n_splits:
        raise ValueError(f"T={t} too small for n_splits={n_splits}")

    block_size = t // n_splits
    blocks = [arr[i * block_size : (i + 1) * block_size] for i in range(n_splits)]
    half = n_splits // 2

    overfit = 0
    total = 0
    for is_idx in combinations(range(n_splits), half):
        is_set = set(is_idx)
        oos_idx = [i for i in range(n_splits) if i not in is_set]
        is_data = np.vstack([blocks[i] for i in is_idx])
        oos_data = np.vstack([blocks[i] for i in oos_idx])

        is_sharpes = _column_sharpes(is_data)
        oos_sharpes = _column_sharpes(oos_data)

        n_star = int(np.argmax(is_sharpes))
        # average rank to handle ties; result in [1, N]
        oos_ranks = pd.Series(oos_sharpes).rank(method="average")
        rank_n_star = float(oos_ranks.iloc[n_star])
        # relative rank w in (0, 1]
        w = rank_n_star / (n_strats + 1)  # avoid w=1 exactly at top rank
        if 0.0 < w < 1.0:
            lam = math.log(w / (1.0 - w))
        elif w <= 0.0:
            lam = -math.inf
        else:
            lam = math.inf
        if lam < 0:
            overfit += 1
        total += 1
    return overfit / total if total else 0.0


def _column_sharpes(arr: np.ndarray) -> np.ndarray:
    means = arr.mean(axis=0)
    sd = arr.std(axis=0, ddof=1)
    sd = np.where(sd <= 0, 1e-12, sd)
    return means / sd


# ─── Monte Carlo block bootstrap ───────────────────────────────────────────


def _block_bootstrap(returns: np.ndarray, *, block_size: int, rng: np.random.Generator) -> np.ndarray:
    n = len(returns)
    n_blocks = math.ceil(n / block_size)
    max_start = max(0, n - block_size)
    starts = rng.integers(0, max_start + 1, size=n_blocks)
    out = np.empty(n_blocks * block_size, dtype=returns.dtype)
    for i, s in enumerate(starts):
        out[i * block_size : (i + 1) * block_size] = returns[s : s + block_size]
    return out[:n]


def _equity_curve(returns: np.ndarray, start: float = 1.0) -> np.ndarray:
    eq = np.empty(len(returns) + 1, dtype=float)
    eq[0] = start
    eq[1:] = start * np.cumprod(1.0 + returns)
    return eq


# ─── ADX helper for regime classification ─────────────────────────────────


def _compute_adx(spy: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder's ADX on a SPY OHLC DataFrame indexed by date.

    Falls back to NaN where the period isn't yet covered. If only ``close``
    is available, returns all NaN — caller treats that as "trend regime
    unavailable".
    """
    if not {"high", "low", "close"}.issubset(spy.columns):
        return pd.Series(np.nan, index=spy.index)
    high = spy["high"].astype(float)
    low = spy["low"].astype(float)
    close = spy["close"].astype(float)
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    up = high.diff()
    dn = -low.diff()
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    atr = tr.ewm(alpha=1.0 / period, adjust=False).mean()
    plus_di = 100.0 * pd.Series(plus_dm, index=spy.index).ewm(alpha=1.0 / period, adjust=False).mean() / atr.replace(0.0, np.nan)
    minus_di = 100.0 * pd.Series(minus_dm, index=spy.index).ewm(alpha=1.0 / period, adjust=False).mean() / atr.replace(0.0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)
    return dx.ewm(alpha=1.0 / period, adjust=False).mean()


# ─── report model ──────────────────────────────────────────────────────────


class OverfittingReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: UUID = Field(default_factory=uuid4)
    engine: str = "unknown"

    dsr_value: float
    dsr_passes: bool
    psr_at_zero: float
    psr_passes: bool

    pbo_value: float | None = None
    pbo_passes: bool | None = None
    pbo_skipped_reason: str | None = None

    min_btl_days: int
    actual_days: int
    min_btl_passes: bool

    sensitivity: dict[str, Any] | None = None
    sensitivity_passes: bool | None = None
    sensitivity_skipped_reason: str | None = None

    mc_sharpe_percentile: float
    mc_p_value: float
    mc_passes: bool
    mc_ruin_probability: float
    mc_equity_percentiles: dict[str, float]

    noise_degradation_pct: float | None = None
    noise_characterization: str | None = None
    noise_passes: bool | None = None
    noise_skipped_reason: str | None = None

    regime_overconcentrated: bool
    regime_gaps: list[str] = Field(default_factory=list)
    regime_details: dict[str, Any] = Field(default_factory=dict)
    regime_skipped_reason: str | None = None

    trades_per_param_ratio: float
    trades_per_param_passes: bool
    trades_per_param_risk: str  # "ok" / "marginal" / "high"

    overall_passed: bool
    summary: str


# ─── the diagnostic ────────────────────────────────────────────────────────


class OverfittingDiagnostic:
    """Run all nine overfitting tests on a backtest's trade-level output."""

    def __init__(
        self,
        trades: list[dict],
        parameters: dict[str, Any],
        sr_observed: float,
        n_trials: int,
        price_data: pd.DataFrame | None = None,
        benchmark_sr: float = 0.0,
        *,
        engine: str = "unknown",
        strategy_fn: Callable[[dict[str, Any]], list[dict]] | None = None,
        trial_returns_matrix: np.ndarray | pd.DataFrame | None = None,
        mc_simulations: int = 1000,
        noise_realizations: int = 100,
        noise_sigma: float = DEFAULT_NOISE_SIGMA,
        seed: int | None = 42,
    ) -> None:
        self._trades = trades
        self._parameters = parameters
        self._sr_observed = float(sr_observed)
        self._n_trials = max(1, int(n_trials))
        self._price_data = price_data
        self._benchmark_sr = float(benchmark_sr)
        self._engine = engine
        self._strategy_fn = strategy_fn
        self._trial_matrix = trial_returns_matrix
        self._mc_sims = int(mc_simulations)
        self._noise_realizations = int(noise_realizations)
        self._noise_sigma = float(noise_sigma)
        self._seed = seed

    # ─────────────────────────────────────────────────────────────────────
    # public entrypoint
    # ─────────────────────────────────────────────────────────────────────

    def run(self) -> OverfittingReport:
        pnls = self._pnl_array()
        n = pnls.size
        skew, kurt = _moments(pnls)
        sr_internal = _per_trade_sharpe(pnls) if n >= 2 else 0.0

        # 1–2. PSR / DSR
        psr_at_zero = _psr_per_trade(sr_internal, 0.0, n, skew, kurt) if n >= 2 else 0.0
        trial_sharpe_var = self._trial_sharpe_variance()  # None ⇒ documented fallback
        if trial_sharpe_var is not None:
            # H-A2-4: the V-source trial count and the multiple-testing
            # count are deliberately distinct estimands — log side-by-side
            # so any divergence is visible, never silently reconciled.
            # Single source of truth for the matrix→2-D conversion: the same
            # ``_trial_matrix_2d`` ``_trial_sharpe_variance`` used (V already
            # non-None here ⇒ the helper's guards passed ⇒ never None).
            _v_arr = self._trial_matrix_2d()
            assert _v_arr is not None  # noqa: S101 — V non-None ⇒ guards passed
            logger.info(
                "tpcore.overfitting.dsr.v_n_trial_population",
                v_trial_count=int(_v_arr.shape[1]),
                n_trials=self._n_trials,
            )
        dsr = (
            _deflated_sharpe_ratio(
                sr_internal, n, skew, kurt, self._n_trials,
                trial_sharpe_variance=trial_sharpe_var,
            )
            if n >= 2
            else 0.0
        )

        # 3. PBO via CSCV (only with a returns matrix)
        pbo_value, pbo_passes, pbo_reason = self._run_pbo()

        # 4. MinBTL
        min_btl = _min_btl_trades(sr_internal, self._n_trials, skew, kurt)
        actual = n  # observation count = trade count
        min_btl_passes = actual >= min_btl

        # 5. Sensitivity sweep
        sens_result, sens_passes, sens_reason = self._run_sensitivity(sr_internal)

        # 6. Monte Carlo sequence stress
        mc = self._run_monte_carlo(pnls)

        # 7. Noise infusion
        noise_pct, noise_char, noise_passes, noise_reason = self._run_noise(sr_internal)

        # 8. Regime coverage
        regime_over, regime_gaps, regime_details, regime_reason = self._run_regime()

        # 9. Trades-per-parameter
        n_params = max(1, len(self._parameters))
        ratio = n / n_params if n_params else 0.0
        ratio_passes = ratio >= TRADES_PER_PARAM_MIN
        if ratio >= TRADES_PER_PARAM_MIN:
            ratio_risk = "ok"
        elif ratio >= 5:
            ratio_risk = "marginal"
        else:
            ratio_risk = "high"

        # ─── overall pass logic ──────────────────────────────────────────
        non_skipped: list[bool] = [
            dsr >= DSR_PASS_THRESHOLD,
            psr_at_zero >= PSR_AT_ZERO_PASS,
            min_btl_passes,
            mc["passes"],
            ratio_passes,
        ]
        if pbo_passes is not None:
            non_skipped.append(pbo_passes)
        if sens_passes is not None:
            non_skipped.append(sens_passes)
        if noise_passes is not None:
            non_skipped.append(noise_passes)

        # Fatal-failure short-circuits per spec
        fatal = (
            (pbo_value is not None and pbo_value > PBO_PASS_THRESHOLD)
            or (dsr < DSR_PASS_THRESHOLD and n >= 2)
        )
        overall_passed = all(non_skipped) and not fatal

        summary = self._build_summary(
            sr_internal=sr_internal,
            psr_at_zero=psr_at_zero,
            dsr=dsr,
            pbo=pbo_value,
            min_btl=min_btl,
            actual=actual,
            mc=mc,
            ratio=ratio,
            overall_passed=overall_passed,
        )

        return OverfittingReport(
            engine=self._engine,
            dsr_value=float(dsr),
            dsr_passes=dsr >= DSR_PASS_THRESHOLD,
            psr_at_zero=float(psr_at_zero),
            psr_passes=psr_at_zero >= PSR_AT_ZERO_PASS,
            pbo_value=pbo_value,
            pbo_passes=pbo_passes,
            pbo_skipped_reason=pbo_reason,
            min_btl_days=int(min_btl),
            actual_days=int(actual),
            min_btl_passes=min_btl_passes,
            sensitivity=sens_result,
            sensitivity_passes=sens_passes,
            sensitivity_skipped_reason=sens_reason,
            mc_sharpe_percentile=float(mc["percentile"]),
            mc_p_value=float(mc["p_value"]),
            mc_passes=bool(mc["passes"]),
            mc_ruin_probability=float(mc["ruin_probability"]),
            mc_equity_percentiles=mc["equity_percentiles"],
            noise_degradation_pct=noise_pct,
            noise_characterization=noise_char,
            noise_passes=noise_passes,
            noise_skipped_reason=noise_reason,
            regime_overconcentrated=regime_over,
            regime_gaps=regime_gaps,
            regime_details=regime_details,
            regime_skipped_reason=regime_reason,
            trades_per_param_ratio=float(ratio),
            trades_per_param_passes=ratio_passes,
            trades_per_param_risk=ratio_risk,
            overall_passed=overall_passed,
            summary=summary,
        )

    # ─────────────────────────────────────────────────────────────────────
    # individual tests
    # ─────────────────────────────────────────────────────────────────────

    def _pnl_array(self) -> np.ndarray:
        if not self._trades:
            return np.zeros(0, dtype=float)
        return np.asarray([float(t["pnl_pct"]) for t in self._trades], dtype=float)

    def _trial_matrix_2d(self) -> np.ndarray | None:
        """The trial matrix as a 2-D ndarray, or ``None`` when it is absent /
        not 2-D / has < MIN_TRIALS_FOR_V columns. Single source of truth for
        the matrix→ndarray conversion shared by ``_trial_sharpe_variance``
        (V) and ``run()``'s H-A2-4 v_trial_count log — so the two can never
        diverge. Never raises (module contract: sub-tests never raise)."""
        if self._trial_matrix is None:
            return None
        arr = (
            self._trial_matrix.values
            if isinstance(self._trial_matrix, pd.DataFrame)
            else np.asarray(self._trial_matrix)
        )
        if arr.ndim != 2 or arr.shape[1] < MIN_TRIALS_FOR_V:
            return None
        return arr

    def _trial_sharpe_variance(self) -> float | None:
        """V[ŜR_n] across the N searched trials, from the SAME per-column
        Sharpe vector PBO already uses (``_column_sharpes``). One canonical
        Sharpe-vector definition; no second estimator. ``None`` when no
        matrix / not 2-D / < MIN_TRIALS_FOR_V columns (delegated to the
        shared ``_trial_matrix_2d``). On ``None`` the value is passed
        through ``_deflated_sharpe_ratio`` to ``_expected_max_sharpe_under_null``,
        which then takes its logged ``1/(n_obs-1)`` single-estimator fallback
        — the §3.1 / H-A2-10 floor keeps the gate safe (tightening-or-equal).
        Never raises (module contract: sub-tests never raise)."""
        arr = self._trial_matrix_2d()
        if arr is None:
            return None
        col_sharpes = _column_sharpes(arr)
        return float(np.var(col_sharpes, ddof=1))

    def _run_pbo(self) -> tuple[float | None, bool | None, str | None]:
        if self._trial_matrix is None:
            return (
                None,
                None,
                "PBO requires trial_returns_matrix (T × N) from the parameter sweep; "
                "single-strategy trades are insufficient for CSCV.",
            )
        try:
            arr = (
                self._trial_matrix.values
                if isinstance(self._trial_matrix, pd.DataFrame)
                else np.asarray(self._trial_matrix)
            )
            t = arr.shape[0]
            n_splits = 16 if t >= 16 else (8 if t >= 8 else 4 if t >= 4 else 2)
            pbo = cscv_pbo(arr, n_splits=n_splits)
            return pbo, pbo < PBO_PASS_THRESHOLD, None
        except Exception as exc:  # noqa: BLE001 — never let a sub-test crash the rollup
            logger.warning("tpcore.overfitting.pbo.error", error=str(exc))
            return None, None, f"CSCV failed: {exc}"

    def _run_sensitivity(self, sr_baseline: float) -> tuple[dict[str, Any] | None, bool | None, str | None]:
        if self._strategy_fn is None:
            return None, None, "strategy_fn not provided"
        if not self._parameters:
            return None, None, "no parameters supplied"
        per_param: dict[str, Any] = {}
        all_scores: list[float] = []
        for name, opt in self._parameters.items():
            if not isinstance(opt, (int, float)):
                # categorical — skip with note
                per_param[name] = {"score": None, "characterization": "categorical (skipped)", "perturbations": []}
                continue
            perturbations: list[dict[str, float]] = []
            target = max(1, abs(sr_baseline) * 0.5)
            n_pass = 0
            n_total = 0
            for rel in (-0.25, -0.10, 0.10, 0.25):
                trial_params = dict(self._parameters)
                trial_params[name] = float(opt) * (1.0 + rel)
                try:
                    trial_trades = self._strategy_fn(trial_params)
                    trial_pnl = np.asarray(
                        [float(t["pnl_pct"]) for t in trial_trades], dtype=float
                    )
                    sr_perturbed = _per_trade_sharpe(trial_pnl) if trial_pnl.size >= 2 else 0.0
                except Exception as exc:  # noqa: BLE001
                    logger.warning("tpcore.overfitting.sensitivity.trial_failed", param=name, rel=rel, error=str(exc))
                    sr_perturbed = 0.0
                perturbations.append({"rel_change": rel, "sharpe": float(sr_perturbed)})
                if abs(sr_perturbed) >= target:
                    n_pass += 1
                n_total += 1
            score = n_pass / n_total if n_total else 0.0
            if score >= SENSITIVITY_ROBUST:
                characterization = "robust"
            elif score >= SENSITIVITY_FRAGILE:
                characterization = "moderate"
            else:
                characterization = "fragile"
            per_param[name] = {
                "score": score,
                "characterization": characterization,
                "perturbations": perturbations,
            }
            all_scores.append(score)
        if not all_scores:
            return per_param, None, "no numeric parameters to sweep"
        passes = min(all_scores) >= SENSITIVITY_FRAGILE
        return per_param, passes, None

    def _run_monte_carlo(self, pnls: np.ndarray) -> dict[str, Any]:
        n = pnls.size
        if n < 2:
            return {
                "percentile": 0.0,
                "p_value": 1.0,
                "passes": False,
                "ruin_probability": 0.0,
                "equity_percentiles": {"p5": 1.0, "p25": 1.0, "p50": 1.0, "p75": 1.0, "p95": 1.0},
            }
        block_size = max(5, n // 20)
        block_size = min(block_size, n)
        rng = np.random.default_rng(self._seed)
        observed_sr = _per_trade_sharpe(pnls)

        null_sharpes = np.empty(self._mc_sims, dtype=float)
        terminals = np.empty(self._mc_sims, dtype=float)
        ruined = 0
        for s in range(self._mc_sims):
            seq = _block_bootstrap(pnls, block_size=block_size, rng=rng)
            equity = _equity_curve(seq)
            null_sharpes[s] = _per_trade_sharpe(seq)
            terminals[s] = float(equity[-1])
            if equity.min() < 0.5:
                ruined += 1

        # Percentile rank of the observed Sharpe in the null distribution
        percentile = float((null_sharpes < observed_sr).mean())
        # Two-sided p-value if you read percentile = position; use upper-tail.
        p_value = float(1.0 - percentile)
        equity_percentiles = {
            "p5": float(np.percentile(terminals, 5)),
            "p25": float(np.percentile(terminals, 25)),
            "p50": float(np.percentile(terminals, 50)),
            "p75": float(np.percentile(terminals, 75)),
            "p95": float(np.percentile(terminals, 95)),
        }
        return {
            "percentile": percentile,
            "p_value": p_value,
            "passes": percentile >= MC_PERCENTILE_PASS,
            "ruin_probability": ruined / self._mc_sims,
            "equity_percentiles": equity_percentiles,
        }

    def _run_noise(self, sr_baseline: float) -> tuple[float | None, str | None, bool | None, str | None]:
        if self._price_data is None:
            return None, None, None, "price_data not provided"
        pnls = self._pnl_array()
        if pnls.size < 2 or sr_baseline == 0:
            return None, None, None, "not enough trades or zero baseline Sharpe"
        rng = np.random.default_rng(self._seed)
        # The simplified path: each trade's P&L receives independent
        # entry/exit perturbations with σ = ``noise_sigma`` (fraction of price).
        # Net perturbation is ε_out − ε_in ~ N(0, σ·√2).
        sigma = self._noise_sigma * math.sqrt(2.0)
        sharpes = np.empty(self._noise_realizations, dtype=float)
        for r in range(self._noise_realizations):
            noise = rng.normal(0.0, sigma, size=pnls.size)
            sharpes[r] = _per_trade_sharpe(pnls + noise)
        mean_noisy = float(np.nan_to_num(sharpes, nan=0.0).mean())
        if sr_baseline == 0:
            return None, None, None, "baseline Sharpe is zero"
        degradation = (sr_baseline - mean_noisy) / abs(sr_baseline)
        if degradation < NOISE_ROBUST_PCT:
            characterization = "robust"
            passes = True
        elif degradation < NOISE_FRAGILE_PCT:
            characterization = "moderate"
            passes = True
        else:
            characterization = "fragile"
            passes = False
        return float(degradation), characterization, passes, None

    def _run_regime(self) -> tuple[bool, list[str], dict[str, Any], str | None]:
        if self._price_data is None:
            return False, [], {}, "price_data not provided"
        if not self._trades:
            return False, [], {}, "no trades"
        pdata = self._price_data.copy()
        if "ticker" not in pdata.columns or "date" not in pdata.columns or "close" not in pdata.columns:
            return False, [], {}, "price_data missing ticker/date/close columns"
        spy = pdata[pdata["ticker"] == "SPY"].copy()
        if spy.empty:
            return False, [], {}, "no SPY in price_data"

        spy["date"] = pd.to_datetime(spy["date"]).dt.date
        spy = spy.sort_values("date").set_index("date")
        spy["close"] = spy["close"].astype(float)
        ret = spy["close"].pct_change()
        spy["rv20_pct"] = ret.rolling(20).std() * math.sqrt(252) * 100.0
        spy["ret20_pct"] = spy["close"].pct_change(20) * 100.0
        spy["adx14"] = _compute_adx(spy)

        buckets: dict[str, dict[str, float]] = {}
        unclassified = 0
        total_pnl = 0.0
        for trade in self._trades:
            entry = trade.get("entry_date")
            entry_d = entry.date() if isinstance(entry, datetime) else entry
            if entry_d is None or entry_d not in spy.index:
                # snap to last available SPY date <= entry_d
                if entry_d is None:
                    unclassified += 1
                    continue
                prior = spy.index[spy.index <= entry_d]
                if len(prior) == 0:
                    unclassified += 1
                    continue
                row = spy.loc[prior[-1]]
            else:
                row = spy.loc[entry_d]

            rv = row["rv20_pct"]
            adx = row["adx14"]
            r20 = row["ret20_pct"]

            vix_bucket = "unknown"
            if pd.notna(rv):
                vix_bucket = "low" if rv < 15 else "moderate" if rv <= 25 else "high"
            trend_bucket = "unknown"
            if pd.notna(adx):
                trend_bucket = "trending" if adx > 25 else ("transitional" if adx >= 20 else "range")
            dir_bucket = "unknown"
            if pd.notna(r20):
                dir_bucket = "bull" if r20 > 3 else ("bear" if r20 < -3 else "sideways")

            for category, sub in (("vix", vix_bucket), ("trend", trend_bucket), ("direction", dir_bucket)):
                key = f"{category}:{sub}"
                b = buckets.setdefault(key, {"trades": 0, "wins": 0, "gross_profit": 0.0, "gross_loss": 0.0, "pnl": 0.0})
                b["trades"] += 1
                pnl = float(trade["pnl_pct"])
                b["pnl"] += pnl
                if pnl > 0:
                    b["wins"] += 1
                    b["gross_profit"] += pnl
                else:
                    b["gross_loss"] += abs(pnl)
            total_pnl += float(trade["pnl_pct"])

        gaps: list[str] = []
        # Check each category's known sub-buckets
        for category, subs in (
            ("vix", ("low", "moderate", "high")),
            ("trend", ("range", "transitional", "trending")),
            ("direction", ("bear", "sideways", "bull")),
        ):
            for sub in subs:
                if f"{category}:{sub}" not in buckets:
                    gaps.append(f"{category}:{sub}")

        overconcentrated = False
        per_bucket: dict[str, Any] = {}
        for k, b in buckets.items():
            n_t = b["trades"]
            wr = b["wins"] / n_t if n_t else 0.0
            pf = (b["gross_profit"] / b["gross_loss"]) if b["gross_loss"] > 0 else float("inf")
            share = (b["pnl"] / total_pnl) if total_pnl != 0 else 0.0
            per_bucket[k] = {
                "trades": n_t,
                "win_rate": wr,
                "profit_factor": pf,
                "pnl_share": share,
            }
            if abs(share) > REGIME_OVERCONCENTRATION_PCT and n_t > 0:
                overconcentrated = True

        details = {
            "buckets": per_bucket,
            "unclassified": unclassified,
            "total_pnl": total_pnl,
        }
        return overconcentrated, gaps, details, None

    # ─────────────────────────────────────────────────────────────────────
    # summary text
    # ─────────────────────────────────────────────────────────────────────

    @staticmethod
    def _build_summary(
        *,
        sr_internal: float,
        psr_at_zero: float,
        dsr: float,
        pbo: float | None,
        min_btl: int,
        actual: int,
        mc: dict[str, Any],
        ratio: float,
        overall_passed: bool,
    ) -> str:
        verdict = "PASSED" if overall_passed else "FAILED"
        bits: list[str] = [
            f"Overall: {verdict}.",
            f"Per-trade Sharpe={sr_internal:.2f}, PSR(>0)={psr_at_zero:.3f}, DSR={dsr:.3f}.",
        ]
        if pbo is not None:
            bits.append(f"PBO={pbo:.3f}")
        bits.append(f"MinBTL={min_btl} trades vs actual={actual}.")
        bits.append(
            f"MC percentile={mc['percentile']:.2f}, ruin probability={mc['ruin_probability']:.2f}."
        )
        bits.append(f"Trades-per-parameter ratio={ratio:.1f}.")
        return " ".join(bits)


__all__ = [
    "DSR_PASS_THRESHOLD",
    "MC_PERCENTILE_PASS",
    "NOISE_FRAGILE_PCT",
    "NOISE_ROBUST_PCT",
    "OverfittingDiagnostic",
    "OverfittingReport",
    "PBO_PASS_THRESHOLD",
    "PSR_AT_ZERO_PASS",
    "REGIME_OVERCONCENTRATION_PCT",
    "TRADES_PER_PARAM_MIN",
    "cscv_pbo",
]
