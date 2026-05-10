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
from datetime import UTC, datetime
from decimal import Decimal
from io import StringIO
from typing import TYPE_CHECKING

from tpcore.quality.data_quality import DataQualityScore, DataQualityWriter

from .credibility import (
    CREDIBILITY_SOURCE_PREFIX,
    BacktestCredibilityRubric,
    CredibilityScore,
    MIN_LIVE_SCORE,
)
from .monte_carlo import MCResult, SHARPE_NULL_DECISION_THRESHOLD
from .sensitivity import FLATNESS_ROBUST_THRESHOLD, SensitivityResult
from .statistical_significance import (
    deflated_sharpe_ratio,
    minimum_backtest_length,
    probabilistic_sharpe_ratio,
)

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg


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


def evaluate_rubric_from_report(
    report: StatValidationReport,
    *,
    lookahead_clean: bool = True,
    survivorship_inclusive: bool = True,
    pit_fundamentals: bool = True,
    regime_coverage: bool = True,
    out_of_sample_validated: bool = False,
    monte_carlo_drawdown: bool = True,
    notes: str | None = None,
) -> CredibilityScore:
    """Combine the auto-derivable validation flags with caller-asserted ones.

    The four auto-flags are read off ``report``:

    * ``sensitivity_surface_flat`` — every sweep's flatness is below threshold.
    * ``monte_carlo_sequence_passed`` — observed Sharpe in top decile of null.
    * ``dsr_above_0_90`` — deflated Sharpe ≥ 0.90.
    * ``backtest_length_above_minbtl`` — observed periods ≥ MinBTL.

    The other six are caller-asserted because they depend on backtest
    construction (PIT discipline, OOS holdout, etc.) — the rubric is
    auditable, not magical.
    """
    return BacktestCredibilityRubric().evaluate(
        lookahead_clean=lookahead_clean,
        survivorship_inclusive=survivorship_inclusive,
        pit_fundamentals=pit_fundamentals,
        regime_coverage=regime_coverage,
        out_of_sample_validated=out_of_sample_validated,
        monte_carlo_drawdown=monte_carlo_drawdown,
        sensitivity_surface_flat=all(s.is_flat for s in report.sweeps),
        monte_carlo_sequence_passed=report.mc.observed_is_significant,
        dsr_above_0_90=report.dsr >= 0.90,
        backtest_length_above_minbtl=report.backtest_periods >= report.minbtl_periods,
        notes=notes,
    )


async def write_credibility_score(
    pool: "asyncpg.Pool",
    *,
    engine_name: str,
    score: CredibilityScore,
    timestamp: datetime | None = None,
) -> bool:
    """Persist ``score`` to ``platform.data_quality_log`` for the gate to read.

    The score is encoded in ``confidence`` as ``score / 100``; ``stale``
    is True iff score < 60 (the live-promotion threshold). Returns True
    iff a new row was written (False on conflict or no pool).
    """
    ts = timestamp or datetime.now(UTC)
    dq = DataQualityScore(
        source=f"{CREDIBILITY_SOURCE_PREFIX}.{engine_name}",
        timestamp=ts,
        latency_ms=0,
        missing_bars=0,
        stale=score.score < MIN_LIVE_SCORE,
        confidence=Decimal(score.score) / Decimal(100),
        notes=score.model_dump_json(),
    )
    return await DataQualityWriter(pool).write(dq)


def render_rubric(score: CredibilityScore) -> str:
    """One-block rendering of the credibility rubric — paired with `render`."""
    out = StringIO()
    out.write(f"\nBacktest credibility rubric  →  score {score.score}/100  ")
    out.write("(LIVE OK)\n" if score.passes_gate else f"(< {MIN_LIVE_SCORE}: BLOCKED)\n")
    items = [
        ("lookahead_clean", score.lookahead_clean),
        ("survivorship_inclusive", score.survivorship_inclusive),
        ("pit_fundamentals", score.pit_fundamentals),
        ("regime_coverage", score.regime_coverage),
        ("out_of_sample_validated", score.out_of_sample_validated),
        ("monte_carlo_drawdown", score.monte_carlo_drawdown),
        ("sensitivity_surface_flat", score.sensitivity_surface_flat),
        ("monte_carlo_sequence_passed", score.monte_carlo_sequence_passed),
        ("dsr_above_0_90", score.dsr_above_0_90),
        ("backtest_length_above_minbtl", score.backtest_length_above_minbtl),
    ]
    weights = BacktestCredibilityRubric.WEIGHTS
    for label, ok in items:
        mark = "✓" if ok else "✗"
        out.write(f"  [{mark}] {label:32s} ({weights[label]:>2d} pts)\n")
    return out.getvalue()


def _fmt_float(x: float) -> str:
    if x is None:
        return "—"
    if math.isnan(x):
        return "  NaN"
    if math.isinf(x):
        return "  inf"
    return f"{x:6.2f}"


__all__ = [
    "StatValidationReport",
    "build_report",
    "evaluate_rubric_from_report",
    "render",
    "render_rubric",
    "write_credibility_score",
]
