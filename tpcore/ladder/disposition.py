"""Data-lane escalation disposition SoT + clockwork drift.

Rung-2-covered classes (selfheal/auditheal/contract) DERIVE their
disposition from the existing registries (no duplicate SoT); the
non-rung-2 classes (audit known_knowns checks + the two escalation
event types) are declared explicitly. A clockwork test asserts the
union is covered — a new escalation class fails the build until a
disposition decision is recorded.
"""
from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from tpcore.auditheal.registry import REMEDIATION_SPECS
from tpcore.ingestion.adapter_contract import ADAPTER_CONTRACTS
from tpcore.selfheal.registry import HEAL_SPECS


class Disposition(StrEnum):
    AUTO_CONVERTED = "auto_converted"
    ESCALATE_OPERATOR = "escalate_operator"
    STRUCTURAL = "structural"
    REMOVED = "removed"


class DispositionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    cls: str
    disposition: Disposition
    derived: bool
    capability: str = ""
    reason: str = ""
    evidence: str = ""


_AUDIT_KK_CHECKS: tuple[str, ...] = (
    "adapter_contract", "row_count", "freshness", "validation_status",
    "ingestion_jobs", "sentinel_basket", "credit_spread_history",
    "csv_archive_presence", "shrinkage_detector", "governor_enforcement",
    "hy_spread_decommission", "insider_sentiment_period",
)

DISPOSITION_POLICIES: dict[str, DispositionPolicy] = {
    "event:DATA_REPAIR_ESCALATED": DispositionPolicy(
        cls="event:DATA_REPAIR_ESCALATED",
        disposition=Disposition.ESCALATE_OPERATOR, derived=False,
        reason="data_repair_service exhausted bounded self-heal for a "
               "request; operator dispositions each open instance via "
               "the weekly digest (rung-3 instance teeth).",
        evidence="ops/data_repair_service.ESCALATED_EVENT_TYPE; "
                 "resolving terminal DATA_REPAIR_COMPLETE."),
    "event:DATA_SOURCE_ESCALATED": DispositionPolicy(
        cls="event:DATA_SOURCE_ESCALATED",
        disposition=Disposition.ESCALATE_OPERATOR, derived=False,
        reason="datasupervisor escalated a source held >= M cycles; "
               "operator dispositions each open instance via the "
               "weekly digest.",
        evidence="tpcore/datasupervisor/state.ESCALATED_EVENT; "
                 "resolving terminal DATA_SOURCE_CLEARED."),
    **{
        f"audit_kk:{c}": DispositionPolicy(
            cls=f"audit_kk:{c}",
            disposition=Disposition.ESCALATE_OPERATOR, derived=False,
            reason="audit_data_pipeline known_knowns FAIL — hard-gated; "
                   "operator investigates + dispositions (convert to a "
                   "bounded check / structural fix / remove the source).",
            evidence="scripts/audit_data_pipeline.py run_known_knowns "
                     f"check_name={c!r}.")
        for c in _AUDIT_KK_CHECKS
    },
}


def _derive(cls: str) -> DispositionPolicy | None:
    if cls.startswith("selfheal:"):
        spec = HEAL_SPECS.get(cls.removeprefix("selfheal:"))
        if spec is None:
            return None
        if spec.healable:
            return DispositionPolicy(
                cls=cls, disposition=Disposition.AUTO_CONVERTED,
                derived=True, capability=f"ops.py --stage {spec.stage}")
        return DispositionPolicy(
            cls=cls, disposition=Disposition.ESCALATE_OPERATOR,
            derived=True, reason=spec.unhealable_reason)
    if cls.startswith("auditheal:"):
        spec = REMEDIATION_SPECS.get(cls.removeprefix("auditheal:"))
        if spec is None:
            return None
        if spec.remediable:
            return DispositionPolicy(
                cls=cls, disposition=Disposition.AUTO_CONVERTED,
                derived=True, capability=f"ops.py --stage {spec.stage}")
        return DispositionPolicy(
            cls=cls, disposition=Disposition.ESCALATE_OPERATOR,
            derived=True, reason=spec.escalate_reason)
    if cls.startswith("contract:"):
        c = ADAPTER_CONTRACTS.get(cls.removeprefix("contract:"))
        if c is None:
            return None
        suffix = " (guard pending)" if c.guard_pending else ""
        return DispositionPolicy(
            cls=cls, disposition=Disposition.ESCALATE_OPERATOR,
            derived=True,
            reason=f"adapter contract drift — escalate-only by design"
                   f"{suffix}")
    return None


def _rung2_classes() -> set[str]:
    return (
        {f"selfheal:{k}" for k in HEAL_SPECS}
        | {f"auditheal:{k}" for k in REMEDIATION_SPECS}
        | {f"contract:{k}" for k in ADAPTER_CONTRACTS}
    )


def data_lane_escalation_classes() -> set[str]:
    return _rung2_classes() | set(DISPOSITION_POLICIES)


def policy_for(cls: str) -> DispositionPolicy:
    derived = _derive(cls)
    if derived is not None:
        return derived
    pol = DISPOSITION_POLICIES.get(cls)
    if pol is None:
        raise KeyError(f"no disposition for escalation class {cls!r}")
    return pol


def _resolvable(cls: str) -> bool:
    try:
        policy_for(cls)
        return True
    except KeyError:
        return False


def disposition_drift() -> tuple[set[str], set[str]]:
    known = data_lane_escalation_classes()
    missing = {c for c in known if not _resolvable(c)}
    rung2 = _rung2_classes()
    extra = {k for k in DISPOSITION_POLICIES if k not in known or k in rung2}
    return missing, extra


__all__ = [
    "DISPOSITION_POLICIES",
    "Disposition",
    "DispositionPolicy",
    "data_lane_escalation_classes",
    "disposition_drift",
    "policy_for",
]
