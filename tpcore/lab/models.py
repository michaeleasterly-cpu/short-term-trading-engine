from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict

from tpcore.backtest.credibility import CredibilityScore


class LabCandidate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str
    target_engine: str
    param_overrides: dict[str, Any]
    intent: Literal["promote_new", "fold_existing"]
    notes: str = ""


class ParamDelta(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str
    current: Any
    winning: Any


class WalkWindowRecord(BaseModel):
    """Frozen mirror of scripts.search_parameters.WalkWindow — one
    walk-forward window (5-year train, 2-year holdout, both inclusive).
    Typed so the frozen SP2→SP3 contract carries structured windows,
    not an untyped bag."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    train_start: date
    train_end: date
    holdout_start: date
    holdout_end: date


class LabResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    candidate: str
    target_engine: str
    intent: Literal["promote_new", "fold_existing"]
    verdict: Literal["SURVIVED", "FAILED"]
    dsr: float
    credibility_score: int
    credibility_rubric: CredibilityScore
    held_metrics: dict[str, Any]
    winning_params: dict[str, Any]
    param_diff: list[ParamDelta]
    recommended_exit: Literal["promote_new", "fold_existing", "none"]
    ranked_alternatives: list[dict[str, Any]]
    walk_windows: list[WalkWindowRecord]
    n_trials: int
    seed: int
    generated_at: AwareDatetime
