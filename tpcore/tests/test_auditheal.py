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


def test_no_production_cross_ref_class_is_remediable() -> None:
    """Post-Tradier-drop (Plan 2 Phase 0 / migration 0300) the
    ``tradier_options_chains`` checks — the sole remediable class —
    are gone for good. None of the 19 remaining cross-table checks has
    a proven-safe auto-delete (deleting an earnings/fundamentals row
    for a transiently prices-missing ticker would destroy correct
    data), so the production catalog is now ENTIRELY escalate-only.
    Every spec must carry an honest ``escalate_reason``; the lane still
    DETECTS every red and escalates (100%-green-or-don't-trade holds —
    it just no longer auto-remediates anything)."""
    remediable = {k for k, s in REMEDIATION_SPECS.items() if s.remediable}
    assert remediable == set()
    assert REMEDIATION_SPECS, "catalog must still cover every cross-table check"
    for s in REMEDIATION_SPECS.values():
        assert s.remediable is False
        assert s.escalate_reason


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


# The production catalog is now entirely escalate-only (the sole
# remediable class, tradier_options_chains, was dropped in Plan 2
# Phase 0). The remediation MACHINERY (remediate-then-green, failed
# remediation, exhaustion) must stay covered even though no production
# class currently exercises it. We inject a SYNTHETIC remediable spec
# into the registry the orchestrator reads via spec_for — exactly the
# shape a future proven-safe class would take — rather than reviving
# the dropped tradier example.
_SYNTHETIC_KEY = "synthetic_table/synthetic_check"
_SYNTHETIC_STAGE = "synthetic_cleanup"


@pytest.fixture()
def remediable_spec(monkeypatch: pytest.MonkeyPatch) -> str:
    """Register one synthetic remediable RemediationSpec keyed by
    ``_SYNTHETIC_KEY`` so the machinery tests have a remediable class to
    drive. Returns the key. Reverted automatically by monkeypatch."""
    from tpcore.auditheal import registry

    spec = RemediationSpec(
        check_key=_SYNTHETIC_KEY, table="synthetic_table",
        check_name="synthetic_check", remediable=True,
        stage=_SYNTHETIC_STAGE, max_attempts=2,
    )
    patched = dict(registry.REMEDIATION_SPECS)
    patched[_SYNTHETIC_KEY] = spec
    monkeypatch.setattr(registry, "REMEDIATION_SPECS", patched)
    return _SYNTHETIC_KEY


async def test_green_first_pass() -> None:
    out = await run_audit_heal(_Pool([[]]), _runner(), _audit())
    assert out.green and out.iterations == 1 and out.remediated == []


async def test_remediates_then_green(remediable_spec: str) -> None:
    rs = _runner()
    out = await run_audit_heal(
        _Pool([[remediable_spec], []]),
        rs, _audit(),
    )
    assert out.green and out.iterations == 2
    assert (_SYNTHETIC_STAGE, {}) in rs.calls


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


async def test_failed_remediation_escalates(remediable_spec: str) -> None:
    out = await run_audit_heal(
        _Pool([[remediable_spec]]),
        _runner(fail_stage=_SYNTHETIC_STAGE), _audit(),
    )
    assert out.green is False
    assert any("exited 1" in r for _, r in out.escalated)


async def test_exhaustion_escalates(remediable_spec: str) -> None:
    out = await run_audit_heal(
        _Pool([[remediable_spec]]),
        _runner(), _audit(), max_iterations=3,
    )
    assert out.green is False and out.iterations == 3
    assert any("exhausted" in r for _, r in out.escalated)
