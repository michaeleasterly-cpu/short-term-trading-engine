"""Unit tests for the data-lane Ladder disposition SoT (rung-3)."""
from __future__ import annotations

import pathlib
import re

import pytest

from tpcore.auditheal.registry import REMEDIATION_SPECS
from tpcore.ingestion.adapter_contract import ADAPTER_CONTRACTS
from tpcore.ladder.disposition import (
    DISPOSITION_POLICIES,
    Disposition,
    data_lane_escalation_classes,
    disposition_drift,
    policy_for,
)
from tpcore.selfheal.registry import HEAL_SPECS


def test_no_drift_full_class_set_covered() -> None:
    missing, extra = disposition_drift()
    assert missing == set(), f"classes with no disposition: {missing}"
    assert extra == set(), f"disposition entries for unknown classes: {extra}"


def test_classes_are_namespaced_union() -> None:
    classes = data_lane_escalation_classes()
    assert {f"selfheal:{k}" for k in HEAL_SPECS} <= classes
    assert {f"auditheal:{k}" for k in REMEDIATION_SPECS} <= classes
    assert {f"contract:{k}" for k in ADAPTER_CONTRACTS} <= classes
    assert "event:DATA_REPAIR_ESCALATED" in classes
    assert "event:DATA_SOURCE_ESCALATED" in classes


def test_selfheal_healable_derives_auto_converted() -> None:
    p = policy_for("selfheal:prices_daily_completeness")
    assert p.disposition is Disposition.AUTO_CONVERTED
    assert p.capability and p.derived is True


def test_selfheal_unhealable_derives_escalate_operator() -> None:
    p = policy_for("selfheal:row_integrity")
    assert p.disposition is Disposition.ESCALATE_OPERATOR
    assert p.reason and p.derived is True


def test_contract_classes_derive_escalate_operator() -> None:
    any_feed = next(iter(ADAPTER_CONTRACTS))
    p = policy_for(f"contract:{any_feed}")
    assert p.disposition is Disposition.ESCALATE_OPERATOR
    assert p.derived is True


def test_event_classes_are_explicit_not_derived() -> None:
    p = policy_for("event:DATA_SOURCE_ESCALATED")
    assert p.derived is False
    assert p.disposition in set(Disposition)
    assert "event:DATA_SOURCE_ESCALATED" in DISPOSITION_POLICIES


def test_explicit_registry_only_holds_non_rung2_keys() -> None:
    for key in DISPOSITION_POLICIES:
        assert key.startswith(("audit_kk:", "event:")), key


def test_policy_for_unknown_raises() -> None:
    with pytest.raises(KeyError):
        policy_for("selfheal:does_not_exist")


def test_audit_kk_checks_match_live_source() -> None:
    from tpcore.ladder.disposition import _AUDIT_KK_CHECKS

    src = (pathlib.Path(__file__).resolve().parents[2] / "scripts" / "audit_data_pipeline.py").read_text()
    m = re.search(r"async def run_known_knowns\(.*?\n(?=async def )",
                  src, re.S)
    body = m.group(0) if m else src
    found = set(re.findall(r'check_name="([a-z_]+)"', body))
    kk = set(re.findall(
        r'phase="known_knowns",\s*check_name="([a-z_]+)"', src))
    live = found | kk
    assert set(_AUDIT_KK_CHECKS) == live, (
        f"missing={live - set(_AUDIT_KK_CHECKS)} "
        f"extra={set(_AUDIT_KK_CHECKS) - live}")
