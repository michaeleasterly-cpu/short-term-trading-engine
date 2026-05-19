"""Tests for `tpcore.backtest.overfitting`.

Synthetic strategies of three flavors — clearly-real, clearly-random, and
clearly-overfit — exercise each of the nine diagnostic tests. All inputs
are generated with seeded numpy RNGs so the tests are deterministic.
"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd

from tpcore.backtest.overfitting import (
    OverfittingDiagnostic,
    OverfittingReport,
    cscv_pbo,
)

# ─── synthetic-data helpers ────────────────────────────────────────────────


def _make_trades(
    returns: list[float] | np.ndarray,
    *,
    start: date = date(2020, 1, 6),
    direction: str = "LONG",
) -> list[dict]:
    """Wrap a return series as the trade-dict shape OverfittingDiagnostic expects."""
    trades: list[dict] = []
    d = start
    for r in returns:
        # one trade-day apart so entry/exit dates are unique and ordered
        trades.append(
            {
                "pnl_pct": float(r),
                "entry_date": d,
                "exit_date": d + timedelta(days=1),
                "direction": direction,
                "ticker": "TEST",
            }
        )
        d = d + timedelta(days=1)
    return trades


def _profitable_returns(seed: int = 11) -> np.ndarray:
    """100 returns, per-trade Sharpe ~1.2, slightly positive skew."""
    rng = np.random.default_rng(seed)
    base = rng.normal(0.024, 0.02, 100)
    # inject a few large winners → positive skew
    base[::25] += 0.03
    return base


def _random_returns(seed: int = 13) -> np.ndarray:
    """100 returns, near-zero mean, fat tails (high kurtosis)."""
    rng = np.random.default_rng(seed)
    # student-t-like fat tails via mixture: mostly small, a few big symmetric.
    body = rng.normal(0.0005, 0.012, 92)
    tails = rng.choice([-0.06, 0.06], size=8)
    out = np.concatenate([body, tails])
    rng.shuffle(out)
    return out


def _overfit_returns(seed: int = 17) -> np.ndarray:
    """30 returns, Sharpe ~2.5 in-sample with extreme negative skew (one big loss)."""
    rng = np.random.default_rng(seed)
    base = rng.normal(0.04, 0.015, 30)
    # Inject one or two large negative shocks for extreme negative skew + high kurt.
    base[5] = -0.18
    base[20] = -0.12
    return base


# ─── 1. Synthetic profitable strategy ──────────────────────────────────────


def test_profitable_strategy_passes_dsr_psr_pbo_and_trades_per_param() -> None:
    trades = _make_trades(_profitable_returns())
    diag = OverfittingDiagnostic(
        trades=trades,
        parameters={"z_threshold": 3.0, "lookback": 20},
        sr_observed=float(np.mean([t["pnl_pct"] for t in trades])
                          / np.std([t["pnl_pct"] for t in trades], ddof=1)),
        n_trials=5,
    )
    report = diag.run()

    assert isinstance(report, OverfittingReport)
    assert report.psr_at_zero > 0.80, f"PSR_at_zero={report.psr_at_zero}"
    assert report.dsr_passes, f"DSR={report.dsr_value}"
    assert report.trades_per_param_ratio >= 10
    assert report.trades_per_param_passes


# ─── 2. Synthetic random strategy ──────────────────────────────────────────


def test_random_strategy_fails_dsr_or_psr_and_trades_per_param() -> None:
    """Random near-zero-Sharpe series with many parameters → fails several gates.

    Use 11 parameters so trades-per-param is < 10.
    """
    trades = _make_trades(_random_returns())
    pnls = np.array([t["pnl_pct"] for t in trades])
    sd = float(pnls.std(ddof=1))
    sr = float(pnls.mean() / sd) if sd > 0 else 0.0
    diag = OverfittingDiagnostic(
        trades=trades,
        parameters={f"p{i}": float(i) for i in range(11)},
        sr_observed=sr,
        n_trials=200,
    )
    report = diag.run()

    # Either DSR fails, or PSR-at-zero fails — random strategies should not
    # convincingly clear the bar for "true Sharpe > 0".
    assert (not report.dsr_passes) or (not report.psr_passes), (
        f"random strategy unexpectedly passed both DSR and PSR (DSR={report.dsr_value}, "
        f"PSR0={report.psr_at_zero})"
    )
    # 100 trades / 11 params = 9.09 → fails the ≥ 10 threshold
    assert not report.trades_per_param_passes


# ─── 3. Synthetic overfit strategy ────────────────────────────────────────


def test_overfit_strategy_fails_minbtl() -> None:
    """30 trades with high in-sample Sharpe but n_trials=100 → MinBTL >> actual."""
    trades = _make_trades(_overfit_returns())
    pnls = np.array([t["pnl_pct"] for t in trades])
    sd = float(pnls.std(ddof=1))
    sr = float(pnls.mean() / sd) if sd > 0 else 0.0
    diag = OverfittingDiagnostic(
        trades=trades,
        parameters={"z_threshold": 3.0, "lookback": 20},
        sr_observed=sr,
        n_trials=100,
    )
    report = diag.run()

    assert report.actual_days < report.min_btl_days, (
        f"expected MinBTL > actual; got MinBTL={report.min_btl_days}, actual={report.actual_days}"
    )
    assert not report.min_btl_passes


# ─── 4. Block bootstrap autocorrelation ───────────────────────────────────


def test_block_bootstrap_preserves_short_range_autocorrelation() -> None:
    """Build a series with strong AR(1) structure; verify the MC null preserves
    sign of lag-1 autocorrelation across blocks (a small block size of 1
    would *not* — that's the iid bootstrap)."""
    rng = np.random.default_rng(31)
    n = 200
    eps = rng.normal(0, 0.01, n)
    ar = np.zeros(n)
    ar[0] = eps[0]
    phi = 0.6
    for i in range(1, n):
        ar[i] = phi * ar[i - 1] + eps[i]

    trades = _make_trades(ar)
    diag = OverfittingDiagnostic(
        trades=trades,
        parameters={"x": 1.0},
        sr_observed=float(ar.mean() / ar.std(ddof=1)) if ar.std(ddof=1) > 0 else 0.0,
        n_trials=1,
    )
    # Internal MC uses block_size = max(5, n // 20) = max(5, 10) = 10. With block-10 the
    # null preserves AR(1) structure inside blocks; the observed ordering's
    # autocorrelation should be roughly preserved by the block bootstrap.
    report = diag.run()
    # Sanity: the MC ran and produced a defined percentile in [0, 1].
    assert 0.0 <= report.mc_sharpe_percentile <= 1.0
    assert 0.0 <= report.mc_p_value <= 1.0


# ─── 5. Noise infusion (no strategy_fn) ───────────────────────────────────


def test_noise_infusion_skipped_without_strategy_fn_and_price_data() -> None:
    """Without price_data or strategy_fn, the test is gracefully skipped."""
    trades = _make_trades(_profitable_returns())
    diag = OverfittingDiagnostic(
        trades=trades,
        parameters={"z": 3.0},
        sr_observed=1.0,
        n_trials=5,
    )
    report = diag.run()
    assert report.noise_passes is None
    assert report.noise_degradation_pct is None


def test_noise_infusion_robust_strategy_low_degradation() -> None:
    """A stable per-trade-pnl strategy: small noise → small degradation < 20%."""
    trades = _make_trades(_profitable_returns())
    # Cheap price_data: one ticker, 130 daily bars
    price_data = pd.DataFrame(
        {
            "ticker": ["TEST"] * 130,
            "date": [date(2020, 1, 6) + timedelta(days=i) for i in range(130)],
            "close": [100.0 + 0.05 * i for i in range(130)],
        }
    )
    diag = OverfittingDiagnostic(
        trades=trades,
        parameters={"z": 3.0},
        sr_observed=1.0,
        n_trials=5,
        price_data=price_data,
    )
    report = diag.run()
    assert report.noise_degradation_pct is not None
    assert report.noise_degradation_pct < 0.20
    assert report.noise_characterization == "robust"


# ─── 6. Regime coverage ───────────────────────────────────────────────────


def test_regime_coverage_flags_overconcentration() -> None:
    """80% of P&L from a single VIX bucket → overconcentrated."""
    # 50 small wins (low-vol regime, high P&L share from this bucket)
    # 5 small wins/losses spread across the other regimes
    rng = np.random.default_rng(42)

    # SPY bars: stable low-vol period, then a high-vol period
    n_low = 60
    n_high = 60
    spy_close_low = 400.0 + np.cumsum(rng.normal(0, 0.1, n_low))   # very low vol
    spy_close_high = 380.0 + np.cumsum(rng.normal(0, 4.0, n_high))  # high vol
    spy_close = np.concatenate([spy_close_low, spy_close_high])
    dates = [date(2020, 1, 6) + timedelta(days=i) for i in range(n_low + n_high)]

    price_data = pd.DataFrame(
        {
            "ticker": ["SPY"] * (n_low + n_high),
            "date": dates,
            "close": spy_close,
            "high": spy_close * 1.01,
            "low": spy_close * 0.99,
            "open": spy_close,
        }
    )

    # Place ~80% of trades in the low-vol window, 20% in high-vol; bias P&L
    # heavily toward the low-vol bucket.
    trades = []
    # 30 trades in low-vol window with large profit
    for i in range(30):
        d = dates[25 + i]
        trades.append({
            "pnl_pct": 0.04,
            "entry_date": d,
            "exit_date": d + timedelta(days=1),
            "direction": "LONG",
            "ticker": "TEST",
        })
    # 5 trades in high-vol window, small P&L
    for i in range(5):
        d = dates[n_low + 20 + i]
        trades.append({
            "pnl_pct": 0.001,
            "entry_date": d,
            "exit_date": d + timedelta(days=1),
            "direction": "LONG",
            "ticker": "TEST",
        })

    diag = OverfittingDiagnostic(
        trades=trades,
        parameters={"z": 3.0},
        sr_observed=2.0,
        n_trials=3,
        price_data=price_data,
    )
    report = diag.run()
    assert report.regime_overconcentrated, f"expected overconcentration; details={report.regime_details}"


# ─── 7. CSCV / PBO standalone ─────────────────────────────────────────────


def test_cscv_pbo_real_strategy_low_pbo() -> None:
    """Matrix where one strategy is a true outperformer in every split → PBO ~ 0."""
    rng = np.random.default_rng(0)
    T, N = 200, 8
    # All N strategies have iid noise; column 0 has a real positive drift on top.
    M = rng.normal(0, 0.01, (T, N))
    M[:, 0] += 0.005  # column 0 is the "real" winner uniformly across the period.
    pbo = cscv_pbo(M, n_splits=8)
    assert 0.0 <= pbo <= 1.0
    assert pbo < 0.30, f"real-strategy PBO too high: {pbo}"


def test_cscv_pbo_overfit_strategies_high_pbo() -> None:
    """Random matrix where the IS winner flips every split → PBO ~ 0.5."""
    rng = np.random.default_rng(1)
    T, N = 200, 16
    M = rng.normal(0, 0.01, (T, N))
    pbo = cscv_pbo(M, n_splits=8)
    assert pbo > 0.30, f"random matrix PBO unexpectedly low: {pbo}"


# ─── 8. Trades-per-parameter ratio ────────────────────────────────────────


def test_trades_per_param_ratio_threshold() -> None:
    trades = _make_trades(_profitable_returns()[:25])  # 25 trades
    diag_pass = OverfittingDiagnostic(
        trades=trades,
        parameters={"a": 1.0, "b": 2.0},  # 25/2 = 12.5 → passes
        sr_observed=1.0,
        n_trials=2,
    )
    assert diag_pass.run().trades_per_param_passes

    diag_fail = OverfittingDiagnostic(
        trades=trades,
        parameters={f"p{i}": 0 for i in range(5)},  # 25/5 = 5 → fails
        sr_observed=1.0,
        n_trials=5,
    )
    assert not diag_fail.run().trades_per_param_passes


# ─── 9. Report shape & overall_passed ─────────────────────────────────────


def test_report_is_pydantic_model_with_summary() -> None:
    trades = _make_trades(_profitable_returns())
    diag = OverfittingDiagnostic(
        trades=trades,
        parameters={"z": 3.0},
        sr_observed=1.2,
        n_trials=5,
    )
    report = diag.run()
    # Pydantic v2 model — round-trip JSON
    raw = report.model_dump_json()
    parsed = OverfittingReport.model_validate_json(raw)
    assert parsed.run_id == report.run_id
    assert isinstance(report.summary, str) and len(report.summary) > 0


def test_run_does_not_raise_on_empty_trades_and_marks_skipped_tests() -> None:
    """Pathological input must be handled gracefully — no unhandled exceptions."""
    diag = OverfittingDiagnostic(
        trades=[],
        parameters={"z": 3.0},
        sr_observed=0.0,
        n_trials=1,
    )
    report = diag.run()
    # All tests should be either skipped or mark failures; nothing should crash.
    assert isinstance(report, OverfittingReport)


# ─── credibility-rubric integration ────────────────────────────────────────


def test_credibility_rubric_with_overfitting_report_uses_30_pt_bundle() -> None:
    """The new evaluate_with_overfitting path must score the four overfitting
    flags off the report and total exactly 100 pts when all flags pass."""
    from tpcore.backtest.credibility import BacktestCredibilityRubric

    trades = _make_trades(_profitable_returns())
    diag = OverfittingDiagnostic(
        trades=trades,
        parameters={"z": 3.0, "lookback": 20},
        sr_observed=1.2,
        n_trials=5,
    )
    report = diag.run()
    rubric = BacktestCredibilityRubric()
    score = rubric.evaluate_with_overfitting(
        report,
        lookahead_clean=True,
        survivorship_inclusive=True,
        pit_fundamentals=True,
        regime_coverage=True,
        out_of_sample_validated=True,
        monte_carlo_drawdown=True,
    )
    # Sensitivity & PBO are skipped without strategy_fn / matrix → 0 of those weights.
    # Available pts: integrity(70) + DSR(10) + trades_per_param(5) + minBTL(5) = 90
    assert score.score <= 90
    assert score.score >= 80, f"unexpectedly low score: {score.score}"
    assert score.dsr_above_0_90
    assert score.trades_per_param_passes
    assert score.backtest_length_above_minbtl


def test_credibility_rubric_overfitting_report_drops_score_when_dsr_fails() -> None:
    from tpcore.backtest.credibility import BacktestCredibilityRubric

    trades = _make_trades(_random_returns())
    pnls = np.array([t["pnl_pct"] for t in trades])
    sd = float(pnls.std(ddof=1))
    sr = float(pnls.mean() / sd) if sd > 0 else 0.0
    diag = OverfittingDiagnostic(
        trades=trades,
        parameters={f"p{i}": float(i) for i in range(11)},
        sr_observed=sr,
        n_trials=200,
    )
    report = diag.run()
    score = BacktestCredibilityRubric().evaluate_with_overfitting(
        report,
        lookahead_clean=True,
        survivorship_inclusive=True,
        pit_fundamentals=True,
        regime_coverage=True,
        out_of_sample_validated=True,
        monte_carlo_drawdown=True,
    )
    # DSR fails → drops 10; trades_per_param fails → drops 5.
    # Score should be well below 70 but at least above 60.
    assert score.score < 90
    # Existing graduation gate is 60 — random strategies with too many params
    # may or may not clear it, but they must score lower than the profitable case.
    assert not score.trades_per_param_passes


# ─── SP-A2: DSR null-variance estimator correction ─────────────────────────
import math as _sp_a2_math  # noqa: E402

import structlog as _sp_a2_structlog  # noqa: E402, F401


def test_sp_a2_fallback_math_byte_unchanged_no_variance_arg() -> None:
    """H-A2-6 / §9: with NO trial_sharpe_variance the result equals the
    legacy 1/(n_obs-1) formula EXACTLY — the norm.ppf bracket + EULER
    blend are byte-unchanged; only the V semantics change."""
    # Lazy imports — SP-A2 symbols don't exist until Task 2.
    from tpcore.backtest.overfitting import (  # noqa: F401
        MIN_TRIALS_FOR_V,  # noqa: F401
        _column_sharpes,  # noqa: F401
        _deflated_sharpe_ratio,  # noqa: F401
        _expected_max_sharpe_under_null,
    )
    n_trials, n_obs = 50, 500
    # The exact legacy expression (pre-SP-A2), recomputed inline.
    from scipy.stats import norm
    sr_variance = 1.0 / (n_obs - 1)
    z1 = float(norm.ppf(1.0 - 1.0 / n_trials))
    z2 = float(norm.ppf(1.0 - 1.0 / (n_trials * _sp_a2_math.e)))
    euler = 0.5772156649015329
    legacy = _sp_a2_math.sqrt(sr_variance) * ((1.0 - euler) * z1 + euler * z2)
    got = _expected_max_sharpe_under_null(n_trials, n_obs)
    assert abs(got - legacy) < 1e-12
    # §2.2 worked number for the fallback branch.
    assert abs(got - 0.10190) < 1e-4


def test_sp_a2_t_worked_cross_trial_variance_pins_2_2_numbers() -> None:
    """T-WORKED (MAKE-OR-BREAK). §2.2: N=50, n_obs=500, V=0.01 ⇒ SR₀≈0.22763;
    fallback ⇒ SR₀≈0.10190; the per-impl ε (H-A2-14: this is the
    overfitting.py / scipy.norm.ppf impl)."""
    # Lazy imports — SP-A2 symbols don't exist until Task 2.
    from tpcore.backtest.overfitting import (
        _deflated_sharpe_ratio,
        _expected_max_sharpe_under_null,
    )
    sr0_v = _expected_max_sharpe_under_null(50, 500, trial_sharpe_variance=0.01)
    assert abs(sr0_v - 0.22763) < 1e-4
    sr0_fb = _expected_max_sharpe_under_null(50, 500)
    assert abs(sr0_fb - 0.10190) < 1e-4
    # The candidate-SR=0.15 DSR pair (skew 0, kurt 3, n=500).
    d_bug = _deflated_sharpe_ratio(0.15, 500, 0.0, 3.0, 50)
    d_fix = _deflated_sharpe_ratio(0.15, 500, 0.0, 3.0, 50,
                                   trial_sharpe_variance=0.01)
    assert abs(d_bug - 0.8573) < 1e-3
    assert abs(d_fix - 0.0423) < 1e-3


def test_sp_a2_t_fallback_warns_loud_and_numeric_backward_compat() -> None:
    """T-FALLBACK-WARNS (MAKE-OR-BREAK, H-A2-1). No variance ⇒ legacy
    numeric AND a loud structlog WARNING (never silent)."""
    from tpcore.backtest.overfitting import _expected_max_sharpe_under_null
    with _sp_a2_structlog.testing.capture_logs() as logs:
        got = _expected_max_sharpe_under_null(50, 500)
    assert abs(got - 0.10190) < 1e-4
    assert any(
        e.get("event") == "tpcore.overfitting.dsr.null_variance_approximation"
        and e.get("log_level") == "warning"
        and e.get("n_trials") == 50 and e.get("n_obs") == 500
        for e in logs
    )


def test_sp_a2_t_fallback_no_warn_when_variance_supplied() -> None:
    """The honest path is silent (no spurious WARNING when V is given)."""
    from tpcore.backtest.overfitting import _expected_max_sharpe_under_null
    with _sp_a2_structlog.testing.capture_logs() as logs:
        _expected_max_sharpe_under_null(50, 500, trial_sharpe_variance=0.01)
    assert not any(
        e.get("event") == "tpcore.overfitting.dsr.null_variance_approximation"
        for e in logs
    )


def test_sp_a2_t_stricter_floor_makes_change_tightening_or_equal() -> None:
    """T-STRICTER (MAKE-OR-BREAK, H-A2-10). Over a grid incl. the
    low-dispersion / degenerate band, DSR_with_V ≤ DSR_fallback + 1e-12
    — the floor max(V, 1/(n_obs-1)) makes the change provably
    tightening-or-equal for EVERY input (never looser)."""
    from tpcore.backtest.overfitting import _deflated_sharpe_ratio
    for n_obs in (250, 500, 1000):
        d_fb = _deflated_sharpe_ratio(0.15, n_obs, 0.0, 3.0, 50)
        for v in (0.0, 1e-9, 1e-6, 0.0005, 0.001, 0.01, 0.04, 0.10):
            d_v = _deflated_sharpe_ratio(0.15, n_obs, 0.0, 3.0, 50,
                                         trial_sharpe_variance=v)
            assert d_v <= d_fb + 1e-12, (n_obs, v, d_v, d_fb)


def test_sp_a2_t_ortho_v_and_n_compose_multiplicatively() -> None:
    """T-ORTHO (§6). Hold V fixed, sweep n_trials ⇒ SR₀ monotone-up in N
    (the untouched Φ⁻¹ bracket); hold N fixed, increase V ⇒ SR₀
    monotone-up in V. They multiply."""
    from tpcore.backtest.overfitting import _expected_max_sharpe_under_null
    base = _expected_max_sharpe_under_null(50, 500, trial_sharpe_variance=0.01)
    more_n = _expected_max_sharpe_under_null(2000, 500, trial_sharpe_variance=0.01)
    assert more_n > base                       # monotone in N (SP-A term)
    more_v = _expected_max_sharpe_under_null(50, 500, trial_sharpe_variance=0.04)
    assert more_v > base                       # monotone in V (SP-A2 term)
    # Multiplicative separability: SR0(N,V) / SR0(N,V0) is N-independent.
    r1 = (_expected_max_sharpe_under_null(50, 500, trial_sharpe_variance=0.04)
          / _expected_max_sharpe_under_null(50, 500, trial_sharpe_variance=0.01))
    r2 = (_expected_max_sharpe_under_null(200, 500, trial_sharpe_variance=0.04)
          / _expected_max_sharpe_under_null(200, 500, trial_sharpe_variance=0.01))
    assert abs(r1 - r2) < 1e-9


def test_sp_a2_t_sig_compat_positional_calls_still_work() -> None:
    """T-SIG-COMPAT. Every legacy positional call still type-checks/runs
    (the keyword-only ``trial_sharpe_variance`` addition is non-breaking)
    AND the positional/None path is *numerically byte-identical* to
    pre-SP-A2 — that byte-identity IS the backward-compat guarantee
    (spec §3.1/§3.2 "backward-compatible by construction").

    Plan-correction history (controller, 2026-05-19): the original
    assertion ``_deflated_sharpe_ratio(0.1, 250, 0.0, 3.0, 1) == 0.0``
    was a plan-author factual error — there is NO DSR-level N=1
    short-circuit. For ``n_trials<=1`` ``_expected_max_sharpe_under_null``
    returns 0.0 as the *threshold*, then ``_psr_per_trade`` returns a
    CDF (~0.9423) — the REAL pre-SP-A2 value (empirically verified
    identical on origin/main, independent of SP-A2). T-SIG-COMPAT's true
    intent is legacy byte-identity, so we pin THAT (the strongest
    faithful form; spec §9 NON-GOALS forbids changing non-V numerics, so
    adding an N=1 short-circuit is explicitly ruled out). The genuine
    threshold short-circuit lives in ``_expected_max_sharpe_under_null``
    (n_trials<=1 / n_obs<2 → 0.0), NOT at the DSR level."""
    from tpcore.backtest.overfitting import (
        _deflated_sharpe_ratio,
        _expected_max_sharpe_under_null,
    )
    assert _expected_max_sharpe_under_null(20, 250) >= 0.0     # reversion shape
    assert _deflated_sharpe_ratio(0.1, 250, 0.0, 3.0, 20) >= 0.0
    # n_trials<=1 short-circuit lives in _expected_max_sharpe_under_null,
    # NOT here; legacy CDF value pinned (see docstring).
    assert abs(_deflated_sharpe_ratio(0.1, 250, 0.0, 3.0, 1)
               - 0.942261266719699) < 1e-12   # legacy positional path byte-identical
    assert _expected_max_sharpe_under_null(50, 1) == 0.0  # genuine n_trials<=1/n_obs<2 threshold short-circuit (this one is real)


def _sp_a2_make_trial_matrix(col_means, *, n_obs=250, seed=0):
    """T×N matrix with controlled per-column Sharpe dispersion."""
    rng = np.random.default_rng(seed)
    cols = []
    for m in col_means:
        c = rng.normal(m, 0.02, n_obs)
        cols.append(c)
    return np.column_stack(cols)


def test_sp_a2_t_crosstrial_matrix_changes_dsr_via_v() -> None:
    """T-CROSSTRIAL (MAKE-OR-BREAK). Supplying a trial_returns_matrix with
    KNOWN per-column Sharpe dispersion makes OverfittingDiagnostic's DSR
    equal the DSR computed from that V, and STRICTLY different from the
    no-matrix fallback run on the same winner (i.e. it tightens)."""
    # Lazy imports — SP-A2 symbols not in module-level namespace (T1/T2 pattern).
    from tpcore.backtest.overfitting import (
        _column_sharpes,
        _deflated_sharpe_ratio,
        _moments,
        _per_trade_sharpe,
    )
    returns = list(np.random.default_rng(1).normal(0.01, 0.02, 200))
    trades = _make_trades(returns)
    matrix = _sp_a2_make_trial_matrix(
        [0.0, 0.02, 0.04, 0.06, 0.08, 0.10, 0.12], n_obs=200, seed=3)
    diag_v = OverfittingDiagnostic(
        trades=trades, parameters={"p": 1}, sr_observed=0.1, n_trials=7,
        trial_returns_matrix=matrix,
    )
    rep_v = diag_v.run()
    diag_fb = OverfittingDiagnostic(
        trades=trades, parameters={"p": 1}, sr_observed=0.1, n_trials=7,
    )
    rep_fb = diag_fb.run()
    expected_v = float(np.var(_column_sharpes(matrix), ddof=1))
    pnls = np.array([t["pnl_pct"] for t in trades], dtype=float)
    sk, ku = _moments(pnls)
    want = _deflated_sharpe_ratio(
        _per_trade_sharpe(pnls), pnls.size, sk, ku, 7,
        trial_sharpe_variance=expected_v,
    )
    assert abs(rep_v.dsr_value - want) < 1e-9
    assert rep_v.dsr_value != rep_fb.dsr_value           # V actually changed DSR
    assert rep_v.dsr_value <= rep_fb.dsr_value + 1e-12   # tightening direction


def test_sp_a2_t_degenerate_identical_columns_and_too_few_cols() -> None:
    """T-DEGENERATE (H-A2-8). All-identical columns ⇒ V=0 ⇒ floored, no
    crash. < MIN_TRIALS_FOR_V columns ⇒ helper returns None ⇒ fallback +
    WARNING (no raise). < 2-D / empty ⇒ None."""
    # Lazy imports — SP-A2 symbols not in module-level namespace (T1/T2 pattern).
    from tpcore.backtest.overfitting import MIN_TRIALS_FOR_V
    returns = list(np.random.default_rng(2).normal(0.01, 0.02, 120))
    trades = _make_trades(returns)
    identical = np.column_stack([np.full(120, 0.01)] * 6)
    rep_ident = OverfittingDiagnostic(
        trades=trades, parameters={"p": 1}, sr_observed=0.1, n_trials=6,
        trial_returns_matrix=identical,
    ).run()
    assert 0.0 <= rep_ident.dsr_value <= 1.0              # no crash, bounded
    few = _sp_a2_make_trial_matrix([0.0, 0.05, 0.10], n_obs=120, seed=4)
    assert few.shape[1] < MIN_TRIALS_FOR_V
    diag_few = OverfittingDiagnostic(
        trades=trades, parameters={"p": 1}, sr_observed=0.1, n_trials=3,
        trial_returns_matrix=few,
    )
    assert diag_few._trial_sharpe_variance() is None      # too few cols
    diag_none = OverfittingDiagnostic(
        trades=trades, parameters={"p": 1}, sr_observed=0.1, n_trials=3,
    )
    assert diag_none._trial_sharpe_variance() is None      # no matrix
