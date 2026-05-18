from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


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


class LabResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    candidate: str
    target_engine: str
    intent: Literal["promote_new", "fold_existing"]
    verdict: Literal["SURVIVED", "FAILED"]
    dsr: float
    credibility_score: int
    credibility_rubric: dict[str, Any]
    held_metrics: dict[str, Any]
    winning_params: dict[str, Any]
    param_diff: list[ParamDelta]
    recommended_exit: Literal["promote_new", "fold_existing", "none"]
    ranked_alternatives: list[dict[str, Any]]
    walk_windows: list[Any]
    n_trials: int
    seed: int
    generated_at: datetime
