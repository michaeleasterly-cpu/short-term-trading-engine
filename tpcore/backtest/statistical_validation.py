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
    MIN_LIVE_SCORE,
    BacktestCredibilityRubric,
    CredibilityScore,
)
from .monte_carlo import SHARPE_NULL_DECISION_THRESHOLD, MCResult
from .overfitting import DSR_PASS_THRESHOLD
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
            and self.dsr >= DSR_PASS_THRESHOLD
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
    dsr_v = "PASS" if report.dsr >= DSR_PASS_THRESHOLD else "FAIL"
    minbtl_v = "PASS" if report.backtest_periods >= report.minbtl_periods else "FAIL"
    out.write(
        f"  PSR (P[true SR > 0])                 {report.psr:.3f}  "
        f"(threshold ≥ 0.95 → {psr_v})\n"
    )
    out.write(
        f"  DSR (n_trials = {report.n_trials})            "
        f"        {report.dsr:.3f}  (threshold ≥ {DSR_PASS_THRESHOLD} → {dsr_v})\n"
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
    * ``dsr_above_pass_threshold`` — deflated Sharpe ≥ ``DSR_PASS_THRESHOLD``.
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
        dsr_above_pass_threshold=report.dsr >= DSR_PASS_THRESHOLD,
        backtest_length_above_minbtl=report.backtest_periods >= report.minbtl_periods,
        notes=notes,
    )


async def write_credibility_score(
    pool: asyncpg.Pool,
    *,
    engine_name: str,
    score: CredibilityScore,
    timestamp: datetime | None = None,
) -> bool:
    """Persist ``score`` to ``platform.data_quality_log`` for the gate to read.

    The score is encoded in ``confidence`` as ``score / 100``; ``stale``
    is True iff score < 60 (the live-promotion threshold). Returns True
    iff a new row was written (False on conflict or no pool).

    F1 (2026-06-01) — when a CALLER wants to also record a structured
    failed-alpha record on the dedicated ``platform.failed_alpha_ledger``
    table, use :func:`write_credibility_score_with_failed_alpha`. This
    plain entry-point is kept for backward compatibility with the
    existing engine-backtest callers; semantics are unchanged.
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


async def write_credibility_score_with_failed_alpha(
    pool: asyncpg.Pool,
    *,
    engine_name: str,
    score: CredibilityScore,
    failed_alpha_record: object | None = None,
    blocking_constraint: object | None = None,
    timestamp: datetime | None = None,
) -> tuple[bool, object | None]:
    """F1 (2026-06-01) — write credibility AND emit a structured
    failed-alpha row on low score.

    Behaviour:

      * Score ≥ ``MIN_LIVE_SCORE`` (=60) → write credibility row only,
        return ``(True, None)``. ``failed_alpha_record`` /
        ``blocking_constraint`` are accepted but IGNORED on a passing
        score (a PASS is not a research-failure event).

      * Score < ``MIN_LIVE_SCORE`` AND ``failed_alpha_record`` is
        provided → write credibility row AND insert the failed-alpha
        record. Returns ``(credibility_inserted, RecordResult)``.

      * Score < ``MIN_LIVE_SCORE`` AND ``failed_alpha_record`` is None
        BUT ``blocking_constraint`` is provided → raise ``ValueError``.
        The caller must supply either a complete ``FailedAlphaRecord``
        OR omit both — passing a constraint without the rest of the
        record means the caller hasn't classified the failure yet.
        Operator hard rule: every ledger row must self-explain
        (failure_summary + blocking_constraint + sweep_id + window).

      * Score < ``MIN_LIVE_SCORE`` AND NEITHER provided → write
        credibility row only (back-compat path). Logs a structlog
        warning so the caller knows a failed-alpha row WAS NOT
        recorded; useful for legacy engine backtests that haven't
        adopted the F1 classification step yet.

    The companion ``write_credibility_score`` keeps its original
    contract for back-compat; new callers use this entry-point.

    The signature uses ``object`` for the F1 model types to avoid an
    eager import of ``tpcore.forensics.alpha_ledger`` at module-import
    time (which would create a top-level dependency on a brand-new
    module from a long-stable file). The runtime check below imports
    lazily.
    """
    # Always write the credibility row first.
    credibility_inserted = await write_credibility_score(
        pool, engine_name=engine_name, score=score, timestamp=timestamp,
    )

    # Pass path — short-circuit.
    if score.score >= MIN_LIVE_SCORE:
        return credibility_inserted, None

    # FAIL path. Three sub-cases below.
    from tpcore.forensics.alpha_ledger import (
        BlockingConstraint as _BC,
    )
    from tpcore.forensics.alpha_ledger import (
        FailedAlphaRecord as _FAR,
    )
    from tpcore.forensics.alpha_ledger import (
        record_failed_alpha as _record,
    )

    if failed_alpha_record is not None and not isinstance(
        failed_alpha_record, _FAR
    ):
        raise TypeError(
            "failed_alpha_record must be a "
            "tpcore.forensics.alpha_ledger.FailedAlphaRecord; "
            f"got {type(failed_alpha_record).__name__}"
        )
    if blocking_constraint is not None and not isinstance(
        blocking_constraint, _BC
    ):
        raise TypeError(
            "blocking_constraint must be a "
            "tpcore.forensics.alpha_ledger.BlockingConstraint; "
            f"got {type(blocking_constraint).__name__}"
        )

    if failed_alpha_record is not None:
        # Complete classification provided — record it.
        rec_result = await _record(pool, failed_alpha_record)
        return credibility_inserted, rec_result

    if blocking_constraint is not None:
        # Partial classification — refuse rather than silently fabricate.
        raise ValueError(
            f"engine={engine_name!r} score={score.score} < "
            f"{MIN_LIVE_SCORE} and blocking_constraint="
            f"{blocking_constraint.value!r} was provided WITHOUT a "
            "complete FailedAlphaRecord. The caller must supply a "
            "full record (sweep_id, data_window_start, "
            "data_window_end, universe, n_trials, failure_summary, "
            "etc.) — the auto-emission path will not fabricate the "
            "remaining fields on the operator's behalf."
        )

    # Back-compat path — neither provided, just log a warning.
    import structlog
    log = structlog.get_logger(__name__)
    log.warning(
        "tpcore.statistical_validation.failed_alpha_not_recorded",
        engine=engine_name, score=score.score,
        detail=(
            "credibility < 60 but no FailedAlphaRecord was passed — "
            "legacy back-compat path. Migrate this caller to pass a "
            "FailedAlphaRecord with the dispositive blocking_constraint."
        ),
    )
    return credibility_inserted, None


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
        ("dsr_above_pass_threshold", score.dsr_above_pass_threshold),
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
    "write_credibility_score_with_failed_alpha",
]
