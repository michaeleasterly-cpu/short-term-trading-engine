"""Engine-Lane Escalation & Hardening Ladder (sub-project after canary).

Closes the silent-best-effort gap: DA-1 (engine_supervisor) and DA-2
(aar_autotune) emit ENGINE_ESCALATED with ZERO consumers. This module
makes every engine escalation CLASS carry a recorded disposition
(clockwork-enforced — a new class fails CI: R2) and (in later tasks)
every undispositioned INSTANCE past grace surface via
`python -m ops.engine_ladder list` with a `disposition` verb (R3).
Engine-native; symmetry-references the data-lane ladder
(tpcore/ladder + weekly_digest) but touches NO data-lane file.
"""
from __future__ import annotations

import enum

from pydantic import BaseModel, ConfigDict

from ops.aar_autotune import _BEHAVIORAL
from ops.engine_supervisor import INFRA_FAILURE_CLASSES


class EngineEscalationDisposition(enum.StrEnum):
    """Every engine escalation terminates in exactly one of these.
    No AUTO_CONVERTED: the engine lane has no auto-conversion actor."""

    CONVERTED = "converted"
    STRUCTURAL = "structural"
    REMOVED = "removed"


class DispositionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    class_name: str
    default: EngineEscalationDisposition
    rationale: str


_D = EngineEscalationDisposition

DISPOSITION_POLICIES: dict[str, DispositionPolicy] = {
    "crashed_startup": DispositionPolicy(
        class_name="crashed_startup", default=_D.STRUCTURAL,
        rationale="DA-1 bounded re-invoke exhausted; persistence ⇒ a "
                  "structural scheduler/runtime fix."),
    "scheduler_crash": DispositionPolicy(
        class_name="scheduler_crash", default=_D.STRUCTURAL,
        rationale="non-zero scheduler exit survived self-heal ⇒ a "
                  "code/runtime defect to fix structurally."),
    "data_request_timeout": DispositionPolicy(
        class_name="data_request_timeout", default=_D.STRUCTURAL,
        rationale="data lane never answered in-window ⇒ the structural "
                  "fix is typically in the DATA LANE's request "
                  "fulfillment/timeout, NOT this engine; disposition "
                  "records the operator confirmed cross-lane ownership."),
    "data_repair_escalated": DispositionPolicy(
        class_name="data_repair_escalated", default=_D.STRUCTURAL,
        rationale="the DATA-LANE escalation owns the fix; this engine "
                  "is HELD (not removed) and auto-clears on "
                  "DATA_REPAIR_COMPLETE green via DA-1 _auto_clear; "
                  "escalate to REMOVED only if the source is "
                  "permanently retired."),
    "missed_cycle": DispositionPolicy(
        class_name="missed_cycle", default=_D.STRUCTURAL,
        rationale="engine silently failed to start over N cycles ⇒ a "
                  "structural scheduling/dispatch fix."),
    _BEHAVIORAL: DispositionPolicy(
        class_name=_BEHAVIORAL, default=_D.STRUCTURAL,
        rationale="DA-2 loss_cluster≥5 / drawdown ⇒ edge-decay; a "
                  "structural strategy review, or REMOVED if the edge "
                  "is gone (snap-out via the Engine SDLC)."),
}

KNOWN_ESCALATION_CLASSES: frozenset[str] = (
    INFRA_FAILURE_CLASSES | {_BEHAVIORAL})


def _drift_for(*, known: set[str] | frozenset[str],
               policies: dict[str, DispositionPolicy]
               ) -> tuple[set[str], set[str]]:
    have = set(policies)
    known_s = set(known)
    return known_s - have, have - known_s


def escalation_drift() -> tuple[set[str], set[str]]:
    """(missing, extra) of the DERIVED KNOWN set vs DISPOSITION_POLICIES.
    No args (mirrors tpcore.ladder.disposition.disposition_drift). Both
    empty == lockstep. A new DA-1/DA-2 class grows KNOWN (via the pinned
    constants) ⇒ missing non-empty ⇒ the clockwork test fails the build
    until a policy is recorded — the R2 tooth."""
    return _drift_for(known=KNOWN_ESCALATION_CLASSES,
                      policies=DISPOSITION_POLICIES)


def policy_for(class_name: str) -> DispositionPolicy | None:
    return DISPOSITION_POLICIES.get(class_name)
