"""Backtest credibility rubric.

A backtest's *score* (0–100) gates whether its engine can graduate from
paper to live. Below 60 → blocked. The rubric is intentionally
checklist-driven so it's auditable and deterministic.

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
| dsr_above_0_90                 | 10 |
| backtest_length_above_minbtl   |  5 |
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

# Minimum score required to permit live promotion.
MIN_LIVE_SCORE = 60


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
    dsr_above_0_90: bool = Field(
        default=False,
        description="Deflated Sharpe Ratio > 0.90 after accounting for n_trials parameter combinations.",
    )
    backtest_length_above_minbtl: bool = Field(
        default=False,
        description="Number of observations in the backtest exceeds MinBTL for the strategy's Sharpe.",
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
        "dsr_above_0_90": 10,
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
        dsr_above_0_90: bool = False,
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
            "dsr_above_0_90": dsr_above_0_90,
            "backtest_length_above_minbtl": backtest_length_above_minbtl,
        }
        score = sum(self.WEIGHTS[k] for k, v in flags.items() if v)
        return CredibilityScore(score=score, notes=notes, **flags)
