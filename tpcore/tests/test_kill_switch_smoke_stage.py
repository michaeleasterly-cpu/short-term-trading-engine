"""``ops.py --stage kill_switch_smoke`` — engine-scheduler kill-switch
short-circuit verification against the live DB.

Migrated 2026-05-21 from ``scripts/test_kill_switch.py`` (orphan-
scripts zero-allowlist sweep; operator overruled the prior keep-as-
helper disposition). The stage flips
``platform.risk_state.kill_switch_active`` for the named engine,
runs the engine's ``scheduler.run_once()``, asserts
``n_candidates == 0`` and ``n_submitted == 0``, then resets the kill
switch in a ``finally`` block — even on failure — so the live engine
is never left frozen.

Asserts the stage (1) is registered as ``--stage kill_switch_smoke``
and is NOT in the daily ``--update`` cadence, (2) requires an
``engine`` param + restricts it to the supported choices, (3)
drives the canonical ensure-row → flip → run → assert → reset
sequence and persists the kill-switch state via the canonical UPDATE
SQL, (4) returns the documented detail-dict shape on the happy
path, (5) hard-fails when the scheduler reports any non-zero work
under the kill switch, and (6) ALWAYS resets the kill switch in
finally — including after a hard fail.

No real DB / Alpaca / FMP touched. The pool fakes the UPDATE/INSERT
SQL the stage emits, and the engine scheduler is patched in-body.
pytest-xdist ops-shadow group per the package-shadow rule.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import scripts.ops as ops
from dashboard_components.health import OPS_UPDATE_STAGES

pytestmark = pytest.mark.xdist_group("ops_shadow")


class _Conn:
    """Captures every execute() so the test can pin the canonical
    UPDATE / INSERT SQL the stage emits."""

    def __init__(self) -> None:
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, sql: str, *args: Any) -> str:
        self.execute_calls.append((sql, args))
        return "UPDATE 1"


class _AcquireCM:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _Conn:
        return self._conn

    async def __aexit__(self, *exc: object) -> None:
        return None


class _Pool:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    def acquire(self) -> _AcquireCM:
        return _AcquireCM(self._conn)


class _FakeRiskState:
    def __init__(self, *, kill_switch_active: bool) -> None:
        self.kill_switch_active = kill_switch_active


class _FakeStore:
    """Replaces ``PostgresRiskStateStore`` so the kill-switch read-back
    returns the expected post-flip state without touching the DB."""

    def __init__(self, _pool: Any) -> None:
        pass

    async def get(self, _engine: str) -> _FakeRiskState:
        return _FakeRiskState(kill_switch_active=True)


class _FakeSummary:
    """Mirrors the engine-specific RunSummary shape the stage duck-
    types — only ``n_candidates`` and ``n_submitted`` matter here."""

    def __init__(self, *, n_candidates: int, n_submitted: int) -> None:
        self.n_candidates = n_candidates
        self.n_submitted = n_submitted


def _patch_scheduler(
    monkeypatch: pytest.MonkeyPatch, *, n_candidates: int, n_submitted: int,
) -> None:
    """Patch ``ReversionScheduler.run_once`` to return a synthetic
    summary so the stage can run end-to-end without a real scheduler."""
    class _FakeScheduler:
        async def run_once(self, *, as_of: Any) -> _FakeSummary:
            return _FakeSummary(
                n_candidates=n_candidates, n_submitted=n_submitted,
            )

    monkeypatch.setattr(
        "reversion.scheduler.ReversionScheduler", _FakeScheduler,
    )


async def test_happy_path_short_circuits_and_resets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kill switch flips on, scheduler returns 0/0, kill switch resets
    — verified by inspecting the captured execute() calls."""
    _patch_scheduler(monkeypatch, n_candidates=0, n_submitted=0)
    monkeypatch.setattr(
        "tpcore.risk.persistent_store.PostgresRiskStateStore", _FakeStore,
    )
    conn = _Conn()
    result = await ops._stage_kill_switch_smoke(
        _Pool(conn), config={"engine": "reversion"},
    )
    assert result == {
        "verified": True, "engine": "reversion",
        "n_candidates": 0, "n_submitted": 0,
    }
    # The execute call sequence must be: ensure-row INSERT, flip ON
    # UPDATE, reset OFF UPDATE — three writes in that order.
    assert len(conn.execute_calls) == 3
    insert_sql, _ = conn.execute_calls[0]
    assert "INSERT INTO platform.risk_state" in insert_sql
    set_on_sql, set_on_args = conn.execute_calls[1]
    assert "UPDATE platform.risk_state" in set_on_sql
    assert "kill_switch_active = $2" in set_on_sql
    assert set_on_args[1] is True
    assert set_on_args[2] is not None  # reason set
    reset_sql, reset_args = conn.execute_calls[2]
    assert "UPDATE platform.risk_state" in reset_sql
    assert reset_args[1] is False
    assert reset_args[2] is None  # reason cleared


async def test_nonzero_candidates_raises_and_still_resets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the scheduler reports ANY non-zero candidate count under
    the kill switch, the stage must hard-fail — but the ``finally``
    block must still reset the kill switch so a live engine isn't
    left frozen."""
    _patch_scheduler(monkeypatch, n_candidates=3, n_submitted=0)
    monkeypatch.setattr(
        "tpcore.risk.persistent_store.PostgresRiskStateStore", _FakeStore,
    )
    conn = _Conn()
    with pytest.raises(SystemExit, match="scanned 3 candidates"):
        await ops._stage_kill_switch_smoke(
            _Pool(conn), config={"engine": "reversion"},
        )
    # Three execute calls: ensure-row, flip ON, reset OFF — proves
    # the reset fired in finally despite the SystemExit.
    assert len(conn.execute_calls) == 3
    final_sql, final_args = conn.execute_calls[-1]
    assert "UPDATE platform.risk_state" in final_sql
    assert final_args[1] is False


async def test_missing_engine_param_raises() -> None:
    """Missing or invalid ``engine`` param ⇒ ``SystemExit`` with the
    valid-choices list in the message."""
    conn = _Conn()
    with pytest.raises(SystemExit, match="reversion"):
        await ops._stage_kill_switch_smoke(_Pool(conn), config={})

    with pytest.raises(SystemExit, match="reversion"):
        await ops._stage_kill_switch_smoke(
            _Pool(conn), config={"engine": "momentum"},
        )


def test_stage_registered_operator_on_demand_only() -> None:
    """Registration-pin: ``kill_switch_smoke`` in ``_STAGE_SPECS`` +
    ``KNOWN_STAGES``, NOT in ``OPS_UPDATE_STAGES`` — daily ``--update``
    must not flip the live kill switch."""
    spec_names = [n for n, _, _ in ops._STAGE_SPECS]
    assert "kill_switch_smoke" in spec_names
    assert "kill_switch_smoke" in ops.KNOWN_STAGES
    assert "kill_switch_smoke" not in OPS_UPDATE_STAGES, (
        "kill_switch_smoke is operator-on-demand verification — it "
        "must NOT be in the daily --update cadence (flips the live "
        "platform.risk_state kill switch)"
    )


def test_orphan_allowlist_entry_removed_and_script_deleted() -> None:
    """Sentinel: ``scripts/test_kill_switch.py`` is gone + the
    allowlist entry was removed."""
    repo_root = Path(__file__).resolve().parents[2]
    script = repo_root / "scripts/test_kill_switch.py"
    assert not script.exists()
    text = (
        repo_root / "scripts/tests/test_no_orphan_scripts.py"
    ).read_text(encoding="utf-8")
    assert '"test_kill_switch"' not in text


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
