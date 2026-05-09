"""Backtest credibility rubric.

A backtest's *score* (0–100) gates whether its engine can graduate from
paper to live. Below 60 → blocked. The rubric is intentionally
checklist-driven so it's auditable and deterministic.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

# Minimum score required to permit live promotion.
MIN_LIVE_SCORE = 60


class CredibilityScore(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lookahead_clean: bool = Field(
        description="No future data leaked into past decisions (PIT discipline)."
    )
    survivorship_inclusive: bool = Field(description="Delisted symbols included in the universe.")
    pit_fundamentals: bool = Field(description="Fundamentals dated by ``as_of``, not snapshot.")
    regime_coverage: bool = Field(description="Backtest spans bull, bear, and chop regimes.")
    out_of_sample_validated: bool = Field(description="Held-out OOS window meets target metrics.")
    monte_carlo_drawdown: bool = Field(description="MC resampling shows tolerable drawdown bounds.")

    score: int = Field(ge=0, le=100, default=0)
    notes: str | None = None

    @property
    def passes_gate(self) -> bool:
        return self.score >= MIN_LIVE_SCORE


class BacktestCredibilityRubric:
    """Computes a credibility score from a checklist of facts about the backtest."""

    WEIGHTS = {
        "lookahead_clean": 25,
        "survivorship_inclusive": 15,
        "pit_fundamentals": 15,
        "regime_coverage": 15,
        "out_of_sample_validated": 20,
        "monte_carlo_drawdown": 10,
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
        notes: str | None = None,
    ) -> CredibilityScore:
        flags = {
            "lookahead_clean": lookahead_clean,
            "survivorship_inclusive": survivorship_inclusive,
            "pit_fundamentals": pit_fundamentals,
            "regime_coverage": regime_coverage,
            "out_of_sample_validated": out_of_sample_validated,
            "monte_carlo_drawdown": monte_carlo_drawdown,
        }
        score = sum(self.WEIGHTS[k] for k, v in flags.items() if v)
        return CredibilityScore(score=score, notes=notes, **flags)
