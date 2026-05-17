"""Unit tests for tpcore.auditheal — mirrors test_selfheal.py.

Pure: fake pool whose red-set advances per re-audit cycle, fake
run_stage + fake run_audit recorders. No DB, no subprocess.
"""
from __future__ import annotations

import pytest

from tpcore.auditheal.registry import REMEDIATION_SPECS, registry_drift, spec_for
from tpcore.auditheal.spec import RemediationSpec


def test_remediable_requires_stage() -> None:
    with pytest.raises(ValueError, match="remediable=True requires a stage"):
        RemediationSpec(check_key="t/c", table="t", check_name="c",
                        remediable=True)


def test_unremediable_requires_reason() -> None:
    with pytest.raises(ValueError, match="escalate_reason"):
        RemediationSpec(check_key="t/c", table="t", check_name="c",
                        remediable=False)


def test_valid_specs_construct() -> None:
    a = RemediationSpec(check_key="t/c", table="t", check_name="c",
                        remediable=True, stage="cross_ref_cleanup")
    b = RemediationSpec(check_key="t/d", table="t", check_name="d",
                        remediable=False, escalate_reason="no safe delete")
    assert a.stage == "cross_ref_cleanup" and b.remediable is False


def test_registry_in_lockstep_with_cross_table_sot() -> None:
    """Clockwork: every CROSS_TABLE_CHECKS key has a deliberate
    RemediationSpec; no missing, no extras."""
    missing, extra = registry_drift()
    assert missing == set(), f"checks with no RemediationSpec: {missing}"
    assert extra == set(), f"RemediationSpecs for unknown checks: {extra}"


def test_only_tradier_cross_ref_class_is_remediable() -> None:
    remediable = {k for k, s in REMEDIATION_SPECS.items() if s.remediable}
    assert remediable == {
        "tradier_options_chains/expiration_in_past",
        "tradier_options_chains/orphan_no_prices",
    }
    for k in remediable:
        assert REMEDIATION_SPECS[k].stage == "cross_ref_cleanup"


def test_every_spec_self_consistent() -> None:
    for key, s in REMEDIATION_SPECS.items():
        assert s.check_key == key
        if s.remediable:
            assert s.stage
        else:
            assert s.escalate_reason
