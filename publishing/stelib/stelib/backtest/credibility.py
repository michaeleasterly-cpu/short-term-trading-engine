"""Backtest credibility rubric.

A backtest's *score* (0–100) gates whether its engine can graduate from
paper to live. Below 60 → blocked. The rubric is intentionally
checklist-driven so it's auditable and deterministic.

Each engine's backtest persists its computed score to
``platform.data_quality_log`` with ``source = "backtest_credibility.{engine}"``.
The Capital Gate's ``assert_can_graduate`` reads the latest row via
:func:`graduation_ready`; engines whose latest credibility row has
``confidence < 0.60`` cannot graduate even if their stats and the Data
Validation Suite are clean.

The 10 categories cover the conventional integrity checks plus the
statistical-validation suite added in 2026 (see
`tpcore/backtest/sensitivity.py`, `monte_carlo.py`, and
`statistical_significance.py`):

| category                       | weight |
| ------------------------------ | -----: |
| lookahead_clean                | 15 |
| survivorship_inclusive         | 10 |
| pit_fundamentals               | 10 |
| regime_coverage                |  5 |
| out_of_sample_validated        | 15 |
| monte_carlo_drawdown           |  5 |
| sensitivity_surface_flat       | 10 |
| monte_carlo_sequence_passed    | 15 |
| dsr_above_pass_threshold       | 10 |
| backtest_length_above_minbtl   |  5 |
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog
from pydantic import BaseModel, ConfigDict, Field

from .overfitting import DSR_PASS_THRESHOLD

if TYPE_CHECKING:  # pragma: no cover
    from .overfitting import OverfittingReport

logger = structlog.get_logger(__name__)

# Minimum score required to permit live promotion.
MIN_LIVE_SCORE = 60

# Source-prefix used when each engine's backtest writes its credibility
# score to platform.data_quality_log. The full source key is
# ``f"{CREDIBILITY_SOURCE_PREFIX}.{engine_name}"``.
CREDIBILITY_SOURCE_PREFIX = "backtest_credibility"


class CredibilityScoreInsufficientError(RuntimeError):
    """Raised when an engine's last persisted credibility score is < 60 (or absent)."""


class CredibilityScore(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # ─── Integrity checks (data, design) ───────────────────────────────────
    lookahead_clean: bool = Field(
        description="No future data leaked into past decisions (PIT discipline)."
    )
    survivorship_inclusive: bool = Field(description="Delisted symbols included in the universe.")
    pit_fundamentals: bool = Field(description="Fundamentals dated by ``as_of``, not snapshot.")
    regime_coverage: bool = Field(description="Backtest spans bull, bear, and chop regimes.")
    out_of_sample_validated: bool = Field(description="Held-out OOS window meets target metrics.")
    monte_carlo_drawdown: bool = Field(description="MC resampling shows tolerable drawdown bounds.")

    # ─── Statistical validation (López de Prado-style overfitting tests) ──
    sensitivity_surface_flat: bool = Field(
        default=False,
        description="Parameter sensitivity sweep performed and surface flatness < 0.20.",
    )
    monte_carlo_sequence_passed: bool = Field(
        default=False,
        description="Block-bootstrap MC: observed Sharpe in top decile of null distribution (p < 0.10).",
    )
    dsr_above_pass_threshold: bool = Field(
        default=False,
        description=(
            f"Deflated Sharpe Ratio ≥ {DSR_PASS_THRESHOLD} (the real gate "
            "DSR_PASS_THRESHOLD) after accounting for n_trials parameter "
            "combinations."
        ),
    )
    backtest_length_above_minbtl: bool = Field(
        default=False,
        description="Number of observations in the backtest exceeds MinBTL for the strategy's Sharpe.",
    )

    # ─── Overfitting-bundle additions (set by evaluate_with_overfitting) ──
    pbo_passes: bool = Field(
        default=False,
        description="CSCV Probability of Backtest Overfitting < 0.50.",
    )
    trades_per_param_passes: bool = Field(
        default=False,
        description="At least 10 trades per parameter tuned.",
    )

    score: int = Field(ge=0, le=100, default=0)
    notes: str | None = None

    @property
    def passes_gate(self) -> bool:
        return self.score >= MIN_LIVE_SCORE


class BacktestCredibilityRubric:
    """Computes a credibility score from a checklist of facts about the backtest."""

    WEIGHTS = {
        # Integrity
        "lookahead_clean": 15,
        "survivorship_inclusive": 10,
        "pit_fundamentals": 10,
        "regime_coverage": 5,
        "out_of_sample_validated": 15,
        "monte_carlo_drawdown": 5,
        # Statistical validation
        "sensitivity_surface_flat": 10,
        "monte_carlo_sequence_passed": 15,
        "dsr_above_pass_threshold": 10,
        "backtest_length_above_minbtl": 5,
    }

    # When an OverfittingReport is supplied, the rubric scores the
    # 30-point overfitting bundle below in place of `monte_carlo_sequence_passed`
    # (which becomes one *input* to the overall MC test inside the diagnostic).
    # The seven non-overfitting items keep their original weights so the total
    # remains 100.
    WEIGHTS_WITH_OVERFITTING = {
        # Integrity (70 pts)
        "lookahead_clean": 15,
        "survivorship_inclusive": 10,
        "pit_fundamentals": 10,
        "regime_coverage": 5,
        "out_of_sample_validated": 15,
        "monte_carlo_drawdown": 5,
        "sensitivity_surface_flat": 10,
        # Overfitting bundle (30 pts)
        "dsr_above_pass_threshold": 10,
        "pbo_passes": 10,
        "trades_per_param_passes": 5,
        "backtest_length_above_minbtl": 5,
    }

    def evaluate(
        self,
        *,
        lookahead_clean: bool,
        survivorship_inclusive: bool,
        pit_fundamentals: bool,
        regime_coverage: bool,
        out_of_sample_validated: bool,
        monte_carlo_drawdown: bool,
        sensitivity_surface_flat: bool = False,
        monte_carlo_sequence_passed: bool = False,
        dsr_above_pass_threshold: bool = False,
        backtest_length_above_minbtl: bool = False,
        notes: str | None = None,
    ) -> CredibilityScore:
        flags = {
            "lookahead_clean": lookahead_clean,
            "survivorship_inclusive": survivorship_inclusive,
            "pit_fundamentals": pit_fundamentals,
            "regime_coverage": regime_coverage,
            "out_of_sample_validated": out_of_sample_validated,
            "monte_carlo_drawdown": monte_carlo_drawdown,
            "sensitivity_surface_flat": sensitivity_surface_flat,
            "monte_carlo_sequence_passed": monte_carlo_sequence_passed,
            "dsr_above_pass_threshold": dsr_above_pass_threshold,
            "backtest_length_above_minbtl": backtest_length_above_minbtl,
        }
        score = sum(self.WEIGHTS[k] for k, v in flags.items() if v)
        return CredibilityScore(score=score, notes=notes, **flags)

    def evaluate_with_overfitting(
        self,
        overfitting_report: OverfittingReport,
        *,
        lookahead_clean: bool,
        survivorship_inclusive: bool,
        pit_fundamentals: bool,
        regime_coverage: bool,
        out_of_sample_validated: bool,
        monte_carlo_drawdown: bool,
        notes: str | None = None,
    ) -> CredibilityScore:
        """Score using the overfitting-aware 100-pt rubric.

        The seven integrity flags come from the caller. The four
        overfitting-bundle flags are read off ``overfitting_report``:

        * ``sensitivity_surface_flat`` ← report.sensitivity_passes (or False if skipped)
        * ``dsr_above_pass_threshold`` ← report.dsr_passes
        * ``pbo_passes``               ← report.pbo_passes (or False if skipped)
        * ``trades_per_param_passes``  ← report.trades_per_param_passes
        * ``backtest_length_above_minbtl`` ← report.min_btl_passes

        ``monte_carlo_sequence_passed`` is preserved as an attribute on the
        returned score (read from ``report.mc_passes``) but is NOT weighted
        in this scoring path — it has been folded into the broader
        diagnostic and is now informational here.
        """
        sens = overfitting_report.sensitivity_passes
        flags_for_score = {
            "lookahead_clean": lookahead_clean,
            "survivorship_inclusive": survivorship_inclusive,
            "pit_fundamentals": pit_fundamentals,
            "regime_coverage": regime_coverage,
            "out_of_sample_validated": out_of_sample_validated,
            "monte_carlo_drawdown": monte_carlo_drawdown,
            "sensitivity_surface_flat": bool(sens) if sens is not None else False,
            "dsr_above_pass_threshold": overfitting_report.dsr_passes,
            "pbo_passes": bool(overfitting_report.pbo_passes) if overfitting_report.pbo_passes is not None else False,
            "trades_per_param_passes": overfitting_report.trades_per_param_passes,
            "backtest_length_above_minbtl": overfitting_report.min_btl_passes,
        }
        score = sum(self.WEIGHTS_WITH_OVERFITTING[k] for k, v in flags_for_score.items() if v)
        # Persist mc_sequence_passed as an attribute even though it does not
        # contribute weight on this scoring path — callers/UIs may still display it.
        return CredibilityScore(
            score=score,
            notes=notes,
            **flags_for_score,
            monte_carlo_sequence_passed=overfitting_report.mc_passes,
        )


async def graduation_ready(pool: Any, engine_name: str) -> bool:
    """Return True iff the engine's latest backtest credibility score is ≥ 60.

    Reads ``platform.data_quality_log`` for the most recent row with
    ``source = "backtest_credibility.{engine_name}"``. Returns ``False`` if
    no row exists (no rubric run on record). The score is encoded in the
    ``confidence`` column as ``score / 100``.
    """
    sql = """
        SELECT confidence
        FROM platform.data_quality_log
        WHERE source = $1
        ORDER BY timestamp DESC
        LIMIT 1
    """
    source = f"{CREDIBILITY_SOURCE_PREFIX}.{engine_name}"
    async with pool.acquire() as conn:
        row = await conn.fetchrow(sql, source)
    if row is None:
        logger.warning(
            "tpcore.credibility.graduation_ready.no_row",
            engine=engine_name,
            source=source,
        )
        return False
    score = int(round(float(row["confidence"]) * 100))
    ready = score >= MIN_LIVE_SCORE
    logger.debug(
        "tpcore.credibility.graduation_ready",
        engine=engine_name,
        score=score,
        ready=ready,
    )
    return ready
