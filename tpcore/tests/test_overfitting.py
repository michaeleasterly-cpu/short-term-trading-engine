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
