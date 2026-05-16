"""Unit tests for the generic self-heal orchestrator + registry.

The orchestrator is pure (run_stage injected), so these run with a
fake stage runner and a fake pool whose red-set advances per
validation cycle — no DB, no subprocess.
"""
from __future__ import annotations

from tpcore.selfheal.orchestrator import run_self_heal
from tpcore.selfheal.registry import HEAL_SPECS, registry_drift, spec_for


class _Conn:
    def __init__(self, pool: _Pool) -> None:
        self._pool = pool

    async def fetch(self, sql: str, *args):
        # Each validation cycle consumes the next red-set.
        reds = self._pool.red_sequence[self._pool.cycle]
        self._pool.cycle = min(self._pool.cycle + 1, len(self._pool.red_sequence) - 1)
        return [{"source": f"validation.{c}"} for c in reds]


class _AcquireCM:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _Conn:
        return self._conn

    async def __aexit__(self, *exc) -> None:
        return None


class _Pool:
    """red_sequence[i] = bare check names red after the i-th
    data_validation run."""

    def __init__(self, red_sequence: list[list[str]]) -> None:
        self.red_sequence = red_sequence or [[]]
        self.cycle = 0

    def acquire(self) -> _AcquireCM:
        return _AcquireCM(_Conn(self))


def _runner(*, fail_stage: str | None = None, fail_rc: int = 1):
    """Fake run_stage; records calls; optional forced failure."""
    calls: list[tuple[str, dict]] = []

    async def run_stage(stage: str, params: dict) -> int:
        calls.append((stage, dict(params)))
        if fail_stage is not None and stage == fail_stage:
            return fail_rc
        return 0

    run_stage.calls = calls  # type: ignore[attr-defined]
    return run_stage


async def test_green_first_pass() -> None:
    rs = _runner()
    out = await run_self_heal(_Pool([[]]), rs)
    assert out.green is True
    assert out.iterations == 1
    assert out.healed == []
    assert out.escalated == []
    # only the data_validation refresh ran, no repair
    assert [c[0] for c in rs.calls] == ["data_validation"]


async def test_heals_on_retry() -> None:
    rs = _runner()
    out = await run_self_heal(
        _Pool([["prices_daily_completeness"], []]), rs
    )
    assert out.green is True
    assert out.iterations == 2
    assert "daily_bars" in out.healed
    assert ("daily_bars", {"repair_gaps": "true"}) in rs.calls


async def test_unhealable_escalates_immediately() -> None:
    rs = _runner()
    out = await run_self_heal(_Pool([["fundamentals_integrity"]]), rs)
    assert out.green is False
    assert any("fundamentals" in s for s, _ in out.escalated)
    # no repair stage attempted — only the validation refresh
    assert [c[0] for c in rs.calls] == ["data_validation"]


async def test_unknown_red_escalates() -> None:
    rs = _runner()
    out = await run_self_heal(_Pool([["totally_new_check"]]), rs)
    assert out.green is False
    assert any("unknown red" in r for _, r in out.escalated)


async def test_failed_repair_escalates() -> None:
    rs = _runner(fail_stage="daily_bars", fail_rc=2)
    out = await run_self_heal(_Pool([["prices_daily_freshness"]]), rs)
    assert out.green is False
    assert any("exited 2" in r for _, r in out.escalated)


async def test_validation_stage_failure_escalates() -> None:
    rs = _runner(fail_stage="data_validation", fail_rc=3)
    out = await run_self_heal(_Pool([[]]), rs)
    assert out.green is False
    assert out.escalated and "data_validation" in out.escalated[0][0]


async def test_exhaustion_escalates() -> None:
    # Always red on a healable check; repair "succeeds" but never
    # clears → must exhaust and escalate, not loop forever.
    rs = _runner()
    out = await run_self_heal(
        _Pool([["prices_daily_completeness"]]), rs, max_iterations=3
    )
    assert out.green is False
    assert out.iterations == 3
    assert any("exhausted" in r for _, r in out.escalated)


def test_registry_in_lockstep_with_suite() -> None:
    """Clockwork guarantee: every validation check has a deliberate
    HealSpec decision; no missing, no extras. Adding a feed/check
    breaks this until a spec is recorded."""
    missing, extra = registry_drift()
    assert missing == set(), f"checks with no HealSpec: {missing}"
    assert extra == set(), f"HealSpecs for unknown checks: {extra}"


def test_every_spec_is_self_consistent() -> None:
    for name, spec in HEAL_SPECS.items():
        assert spec.check_name == name
        if spec.healable:
            assert spec.stage, f"{name}: healable but no stage"
        else:
            assert spec.unhealable_reason, f"{name}: unhealable but no reason"


def test_spec_for_unknown_is_none() -> None:
    assert spec_for("no_such_check") is None
