"""The known_knowns adapter_contract check: coverage OK, pending WARN,
recent-escalation FAIL."""
from __future__ import annotations

from scripts.audit_data_pipeline import _adapter_contract_findings


class _Conn:
    def __init__(self, escalations: int) -> None:
        self._n = escalations

    async def fetchval(self, *a, **k):
        return self._n


class _CM:
    def __init__(self, c): self._c = c
    async def __aenter__(self): return self._c
    async def __aexit__(self, *e): return None


class _Pool:
    def __init__(self, escalations: int = 0) -> None:
        self._c = _Conn(escalations)

    def acquire(self): return _CM(self._c)


async def test_coverage_ok_and_pending_warn() -> None:
    findings = await _adapter_contract_findings(_Pool(0))
    sevs = {(f.check_name, f.severity) for f in findings}
    assert ("adapter_contract", "OK") in sevs
    assert any(f.severity == "WARN" and "guard_pending" in f.summary
               for f in findings)


async def test_recent_escalation_fails() -> None:
    findings = await _adapter_contract_findings(_Pool(2))
    assert any(f.check_name == "adapter_contract" and f.severity == "FAIL"
               and "escalation" in f.summary.lower() for f in findings)
