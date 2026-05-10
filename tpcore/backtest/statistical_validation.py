"""Renders the Statistical Validation report.

The caller assembles the four inputs — one or more parameter-sensitivity
sweeps, a Monte Carlo result, a PSR/DSR pair, and a MinBTL — and this
module turns them into a single human-readable report block. Each engine
backtest does its own sweep (different parameters) then plugs results
into ``render`` so the output format stays consistent across engines.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from io import StringIO

from .monte_carlo import MCResult, SHARPE_NULL_DECISION_THRESHOLD
from .sensitivity import FLATNESS_ROBUST_THRESHOLD, SensitivityResult
from .statistical_significance import (
    deflated_sharpe_ratio,
    minimum_backtest_length,
    probabilistic_sharpe_ratio,
)


@dataclass(frozen=True)
class StatValidationReport:
    sweeps: list[SensitivityResult]
    mc: MCResult
    psr: float
    dsr: float
    n_trials: int
    minbtl_periods: int
    backtest_periods: int
    sharpe_annualized: float

    @property
    def all_significant(self) -> bool:
        flat = all(s.is_flat for s in self.sweeps)
        return (
            flat
            and self.mc.observed_is_significant
            and self.dsr >= 0.90
            and self.backtest_periods >= self.minbtl_periods
        )


def build_report(
    trades_returns: list[float],
    *,
    sweeps: list[SensitivityResult],
    sharpe_annualized: float,
    backtest_periods: int,
    n_trials: int,
    seed: int | None = 42,
) -> StatValidationReport:
    """One-shot helper: build MCResult + PSR + DSR + MinBTL from inputs."""
    from .monte_carlo import monte_carlo_sequence_test  # local to avoid cycle

    trades_dicts = [{"return_pct": r} for r in trades_returns]
    mc = monte_carlo_sequence_test(trades_dicts, n_simulations=1000, seed=seed)
    psr = probabilistic_sharpe_ratio(trades_returns, benchmark_sr=0.0)
    dsr = deflated_sharpe_ratio(trades_returns, n_trials=n_trials, benchmark_sr=1.0)
    minbtl = minimum_backtest_length(sharpe_annualized, n_trials=n_trials)
    return StatValidationReport(
        sweeps=sweeps,
        mc=mc,
        psr=psr,
        dsr=dsr,
        n_trials=n_trials,
        minbtl_periods=minbtl,
        backtest_periods=backtest_periods,
        sharpe_annualized=sharpe_annualized,
    )


def render(report: StatValidationReport, *, title: str = "Statistical Validation") -> str:
    out = StringIO()
    out.write(f"\n{title}\n")
    out.write("=" * len(title) + "\n\n")

    # ── Parameter sensitivity ───────────────────────────────────────────
    out.write("Parameter sensitivity sweep\n")
    for s in report.sweeps:
        verdict = "FLAT (robust)" if s.is_flat else "SPIKEY (overfit risk)"
        out.write(f"  {s.param_name:20s} flatness={s.flatness_score:.3f}  {verdict}\n")
        for p in s.points:
            pf = p.metrics.get("profit_factor", float("nan"))
            sh = p.metrics.get("sharpe", float("nan"))
            out.write(f"    {str(p.param_value):>16s}  PF={_fmt_float(pf)}  Sharpe={_fmt_float(sh)}\n")
    out.write(
        f"  threshold for FLAT: flatness < {FLATNESS_ROBUST_THRESHOLD}\n\n"
    )

    # ── Monte Carlo ─────────────────────────────────────────────────────
    out.write("Monte Carlo sequence stress test\n")
    pct = report.mc.observed_sharpe_percentile
    sig = "PASS" if report.mc.observed_is_significant else "FAIL"
    out.write(
        f"  observed Sharpe                      {report.mc.observed_sharpe:+.3f}\n"
    )
    out.write(
        f"  observed percentile in null          {pct*100:.1f}%  "
        f"(threshold ≥ {SHARPE_NULL_DECISION_THRESHOLD*100:.0f}% → {sig})\n"
    )
    out.write(
        f"  probability of ruin (50% capital)    {report.mc.probability_of_ruin*100:.1f}%\n"
    )
    out.write(
        f"  worst drawdown p95                   {report.mc.worst_drawdown_p95*100:+.2f}%\n"
    )
    out.write(f"  simulations                          {report.mc.n_simulations}\n\n")

    # ── PSR / DSR / MinBTL ───────────────────────────────────────────────
    out.write("Significance after multiple-testing\n")
    psr_v = "PASS" if report.psr >= 0.95 else "FAIL"
    dsr_v = "PASS" if report.dsr >= 0.90 else "FAIL"
    minbtl_v = "PASS" if report.backtest_periods >= report.minbtl_periods else "FAIL"
    out.write(
        f"  PSR (P[true SR > 0])                 {report.psr:.3f}  "
        f"(threshold ≥ 0.95 → {psr_v})\n"
    )
    out.write(
        f"  DSR (n_trials = {report.n_trials})            "
        f"        {report.dsr:.3f}  (threshold ≥ 0.90 → {dsr_v})\n"
    )
    if report.minbtl_periods >= 1_000_000:
        minbtl_str = "∞ (Sharpe ≤ 0 — no length suffices)"
    else:
        minbtl_str = f"{report.minbtl_periods} periods"
    out.write(
        f"  MinBTL (annualized SR={report.sharpe_annualized:+.2f})    "
        f"{minbtl_str}\n"
    )
    out.write(
        f"  observed length                      {report.backtest_periods} periods  "
        f"({minbtl_v})\n\n"
    )

    overall = "PASS" if report.all_significant else "FAIL"
    out.write(f"Overall statistical validation: {overall}\n")
    return out.getvalue()


def _fmt_float(x: float) -> str:
    if x is None:
        return "—"
    if math.isnan(x):
        return "  NaN"
    if math.isinf(x):
        return "  inf"
    return f"{x:6.2f}"


__all__ = ["StatValidationReport", "build_report", "render"]
