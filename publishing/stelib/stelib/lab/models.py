from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from stelib.backtest.credibility import CredibilityScore
from stelib.lab.target import LabPrimaryMetric


class LabCandidate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]+$")]
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
    # SP-D §2.4 / §8-A11 — DEFAULTED (NOT required). LabResult is
    # extra="forbid" and ops/engine_sdlc/_evidence.py model_validates
    # pre-existing on-disk sidecars that have NO `primary_metric` key
    # (verified: docs/lab/2026-05-18-exp1-SURVIVED-seed7.json). pydantic
    # v2 fills the default for an ABSENT key under extra="forbid" (forbid
    # rejects UNKNOWN keys, not absent defaulted ones), so legacy
    # sidecars validate -> SHARPE (semantically exact: every pre-SP-D run
    # WAS Sharpe-ranked). Display/provenance ONLY — the planner/ECR
    # NEVER reads this for a gate decision (it re-derives
    # verdict/dsr/credibility_score/winning_params, §0.2a).
    primary_metric: LabPrimaryMetric = LabPrimaryMetric.SHARPE
