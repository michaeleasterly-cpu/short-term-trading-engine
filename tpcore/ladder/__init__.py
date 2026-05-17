"""Data-lane Escalation & Hardening Ladder (rung-3 forcing function).

Codifies the principle: every data-lane escalation terminates in
converted | structural | removed — never by loosening an agent. The
disposition SoT is DERIVED from the rung-2 registries
(HEAL_SPECS/REMEDIATION_SPECS/ADAPTER_CONTRACTS — no duplicate SoT)
plus an explicit registry for the non-rung-2 classes.
"""
from tpcore.ladder.disposition import (
    DISPOSITION_POLICIES,
    Disposition,
    DispositionPolicy,
    data_lane_escalation_classes,
    disposition_drift,
    policy_for,
)

__all__ = [
    "DISPOSITION_POLICIES",
    "Disposition",
    "DispositionPolicy",
    "data_lane_escalation_classes",
    "disposition_drift",
    "policy_for",
]
