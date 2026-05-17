"""Unit tests for tpcore.auditheal — mirrors test_selfheal.py.

Pure: fake pool whose red-set advances per re-audit cycle, fake
run_stage + fake run_audit recorders. No DB, no subprocess.
"""
from __future__ import annotations

import pytest

from tpcore.auditheal.orchestrator import run_audit_heal
from tpcore.auditheal.registry import REMEDIATION_SPECS, registry_drift
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


class _Conn:
    def __init__(self, pool: _Pool) -> None:
        self._p = pool

    async def fetch(self, sql: str):
        reds = self._p.red_sequence[self._p.cycle]
        self._p.cycle = min(self._p.cycle + 1,
                            len(self._p.red_sequence) - 1)
        return [{"source": f"cross_table_audit.{k.replace('/', '.', 1)}"}
                for k in reds]


class _ACM:
    def __init__(self, c: _Conn) -> None:
        self._c = c

    async def __aenter__(self) -> _Conn:
        return self._c

    async def __aexit__(self, *e) -> None:
        return None


class _Pool:
    """red_sequence[i] = keys red after the i-th re-audit."""

    def __init__(self, red_sequence: list[list[str]]) -> None:
        self.red_sequence = red_sequence or [[]]
        self.cycle = 0

    def acquire(self) -> _ACM:
        return _ACM(_Conn(self))


def _audit(rc: int = 0):
    calls = []

    async def run_audit() -> int:
        calls.append("audit")
        return rc

    run_audit.calls = calls  # type: ignore[attr-defined]
    return run_audit


def _runner(*, fail_stage: str | None = None):
    calls: list[tuple[str, dict]] = []

    async def run_stage(stage: str, params: dict) -> int:
        calls.append((stage, dict(params)))
        return 1 if stage == fail_stage else 0

    run_stage.calls = calls  # type: ignore[attr-defined]
    return run_stage


async def test_green_first_pass() -> None:
    out = await run_audit_heal(_Pool([[]]), _runner(), _audit())
    assert out.green and out.iterations == 1 and out.remediated == []


async def test_remediates_then_green() -> None:
    rs = _runner()
    out = await run_audit_heal(
        _Pool([["tradier_options_chains/expiration_in_past"], []]),
        rs, _audit(),
    )
    assert out.green and out.iterations == 2
    assert ("cross_ref_cleanup", {}) in rs.calls


async def test_unremediable_escalates_immediately() -> None:
    rs = _runner()
    out = await run_audit_heal(
        _Pool([["earnings_events/orphan_no_prices"]]), rs, _audit()
    )
    assert out.green is False
    assert any("no proven-safe" in r for _, r in out.escalated)
    assert rs.calls == []


async def test_unknown_red_escalates() -> None:
    out = await run_audit_heal(
        _Pool([["mystery_table/mystery_check"]]), _runner(), _audit()
    )
    assert out.green is False
    assert any("unknown cross-table red" in r for _, r in out.escalated)


async def test_audit_failure_escalates() -> None:
    out = await run_audit_heal(_Pool([[]]), _runner(), _audit(rc=2))
    assert out.green is False
    assert out.escalated and "cross_table_audit" in out.escalated[0][0]


async def test_failed_remediation_escalates() -> None:
    out = await run_audit_heal(
        _Pool([["tradier_options_chains/orphan_no_prices"]]),
        _runner(fail_stage="cross_ref_cleanup"), _audit(),
    )
    assert out.green is False
    assert any("exited 1" in r for _, r in out.escalated)


async def test_exhaustion_escalates() -> None:
    out = await run_audit_heal(
        _Pool([["tradier_options_chains/expiration_in_past"]]),
        _runner(), _audit(), max_iterations=3,
    )
    assert out.green is False and out.iterations == 3
    assert any("exhausted" in r for _, r in out.escalated)
