"""``HealSpec`` — the declarative per-feed self-heal contract.

One frozen spec per validation check. The orchestrator never contains
source-specific logic; ALL per-feed knowledge lives here as data:

* which canonical ``ops.py --stage`` fixes this check's failure class
* the bounded ``--param`` set for the TARGETED repair (never a
  whole-universe force_refresh — that was proven 2026-05-15 to exceed
  the 3600s stage timeout and so could never self-heal)
* whether the failure class is auto-healable at all (``healable``).
  ``healable=False`` is honest, not lazy: a check whose failure a bars
  backfill cannot fix (fundamentals integrity, a broken constituent
  snapshot, …) MUST escalate to a human — faking a heal there is the
  dishonest cross-source "heal" the standard forbids.

The bounded-targeting itself lives in the stage's repair mode (e.g.
``daily_bars --param repair_gaps=true``) and is computed from the SAME
evaluation as the validation check (cf. ``_evaluate`` shared by
``check_prices_daily_completeness`` + ``compute_gap_repair_targets``),
so detector and healer can never disagree.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class HealSpec(BaseModel):
    """How to heal one validation check (or why it can't be)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # Validation check name. Maps to data_quality_log
    # ``source = 'validation.<check_name>'``.
    check_name: str
    # Human-facing data-feed identity (for logs / escalation messages).
    source: str
    # False → this failure class is NOT bars/stage auto-fixable; always
    # escalate. Honest-heal rule: never fake-green an unhealable red.
    healable: bool
    # Canonical ops.py stage that performs the BOUNDED targeted repair.
    # Empty iff not healable.
    stage: str = ""
    # --param k=v for the bounded repair (string values; ops.py
    # _parse_params coerces). Empty iff not healable.
    params: dict[str, str] = Field(default_factory=dict)
    # Per-spec bounded retry ceiling (global loop also bounded).
    max_attempts: int = 3
    # Why it's unhealable (required when healable is False) — surfaced
    # in the escalation so the operator knows it's by-design, not a bug.
    unhealable_reason: str = ""

    def model_post_init(self, _ctx: object) -> None:  # noqa: D401
        if self.healable:
            if not self.stage:
                raise ValueError(
                    f"HealSpec[{self.check_name}]: healable=True requires a stage"
                )
        else:
            if not self.unhealable_reason:
                raise ValueError(
                    f"HealSpec[{self.check_name}]: healable=False requires "
                    "unhealable_reason (honest escalation, not a silent gap)"
                )


__all__ = ["HealSpec"]
