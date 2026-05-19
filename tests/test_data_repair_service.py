"""Unit tests for ops/data_repair_service.py.

Pure + injected, mirroring tpcore/tests/test_selfheal.py style: a fake
asyncpg pool (no DB), an injected/monkeypatched canonical runner and
``run_self_heal`` (no subprocess), and an overridable lock dir (no
collision with a real run_data_operations.sh).

Coverage:
  1. already-green sources                → COMPLETE green=true, no heal
  2. heal makes requested green           → COMPLETE green=true
  3. partial heal                         → COMPLETE green=false + still_red
  4. escalation                           → DATA_REPAIR_ESCALATED + attempts
  5. exactly-once: pre-existing terminal  → skipped, no 2nd emit
  6. lock contention (live pid)           → defer, no heal, cursor frozen,
                                            then heals next tick
  7. malformed / duplicate request_id in a poll batch handled idempotently
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tpcore.selfheal.orchestrator import SelfHealOutcome

# NOTE: ``scripts/ops.py`` is a single-file module that some other test
# (tpcore/tests/test_ops_helpers.py) imports as the top-level name
# ``ops`` after putting ``scripts/`` on sys.path — that permanently
# shadows the ``ops/`` *package* in sys.modules for the whole pytest
# session. So ``import ops.data_repair_service`` is collection-order
# fragile. Load the module by file path under a private, collision-free
# name instead — robust regardless of which test ran first.
_DRS_PATH = Path(__file__).resolve().parent.parent / "ops" / "data_repair_service.py"
_spec = importlib.util.spec_from_file_location("_drs_under_test", _DRS_PATH)
assert _spec is not None and _spec.loader is not None
drs = importlib.util.module_from_spec(_spec)
sys.modules["_drs_under_test"] = drs
_spec.loader.exec_module(drs)

# ────────────────────────────────────────────────────────────────────────
# Fakes.
# ────────────────────────────────────────────────────────────────────────


# pytest-xdist: pin this ops-shadow module to one worker so its
# sys.modules['ops'] / scripts/ops.py loading stays single-process
# (the ops/ package-shadow is a single-process invariant). P1.3.
pytestmark = pytest.mark.xdist_group("ops_shadow")


class _Conn:
    def __init__(self, pool: _Pool) -> None:
        self._pool = pool

    async def fetch(self, sql: str, *args):
        if "FROM platform.data_quality_log" in sql:
            # _RED_SQL: each call consumes the next red-set in sequence.
            reds = self._pool.red_sequence[self._pool.cycle]
            self._pool.cycle = min(
                self._pool.cycle + 1, len(self._pool.red_sequence) - 1
            )
            return [{"source": f"validation.{c}"} for c in reds]
        if "WHERE event_type = $1" in sql:
            # _NEW_REQUESTS_SQL: requests with recorded_at > cursor.
            cursor = args[1]
            return [
                {"recorded_at": ts, "data": data}
                for ts, data in self._pool.requests
                if ts > cursor
            ]
        raise AssertionError(f"unexpected fetch SQL: {sql!r}")

    async def fetchrow(self, sql: str, *args):
        if "data->>'request_id'" in sql:
            # _TERMINAL_EXISTS_SQL.
            request_id = args[1]
            for ev in self._pool.emitted:
                if (
                    ev["event_type"] in drs.TERMINAL_EVENT_TYPES
                    and ev["data"].get("request_id") == request_id
                ):
                    return {"?column?": 1}
            return None
        raise AssertionError(f"unexpected fetchrow SQL: {sql!r}")

    async def execute(self, sql: str, *args):
        if "INSERT INTO platform.application_log" in sql:
            engine, run_id, event_type, severity, message, data_json = args
            self._pool.emitted.append(
                {
                    "engine": engine,
                    "event_type": event_type,
                    "severity": severity,
                    "message": message,
                    "data": json.loads(data_json),
                }
            )
            return None
        raise AssertionError(f"unexpected execute SQL: {sql!r}")


class _AcquireCM:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _Conn:
        return self._conn

    async def __aexit__(self, *exc) -> None:
        return None


class _Pool:
    def __init__(
        self,
        red_sequence: list[list[str]],
        requests: list[tuple[datetime, dict]] | None = None,
    ) -> None:
        self.red_sequence = red_sequence or [[]]
        self.cycle = 0
        self.requests = requests or []
        self.emitted: list[dict] = []

    def acquire(self) -> _AcquireCM:
        return _AcquireCM(_Conn(self))


def _request(
    request_id: str, sources: list[str], engine: str = "vector"
) -> dict:
    return {
        "schema": 1,
        "request_id": request_id,
        "engine": engine,
        "sources": sources,
        "reason": "test",
    }


@pytest.fixture
def lock_dir(tmp_path) -> str:
    """Overridable lock dir so tests never touch a real data-ops run."""
    return os.path.join(str(tmp_path), "ste-data-operations.lock")


def _patch_heal(monkeypatch, *, outcome: SelfHealOutcome | None, calls: list):
    """Stub make_canonical_runner (records stage calls) + run_self_heal."""

    def fake_make_runner(run_id: str):
        async def run_stage(stage: str, params: dict) -> int:
            calls.append((stage, dict(params)))
            return 0

        return run_stage

    async def fake_run_self_heal(pool, runner, **kw):
        calls.append(("__run_self_heal__", {}))
        assert outcome is not None
        return outcome

    monkeypatch.setattr(drs, "make_canonical_runner", fake_make_runner)
    monkeypatch.setattr(drs, "run_self_heal", fake_run_self_heal)


# ────────────────────────────────────────────────────────────────────────
# 1. Already-green → COMPLETE green=true, run_self_heal NOT called.
# ────────────────────────────────────────────────────────────────────────


async def test_already_green_completes_without_heal(monkeypatch, lock_dir):
    calls: list = []
    _patch_heal(monkeypatch, outcome=None, calls=calls)
    pool = _Pool(red_sequence=[[]])  # nothing red

    terminated = await drs._handle_request(  # noqa: SLF001
        pool, _request("r1", ["prices_daily"]), lock_dir
    )

    assert terminated is True
    assert ("__run_self_heal__", {}) not in calls  # no heal
    assert [c[0] for c in calls] == ["data_validation"]
    assert len(pool.emitted) == 1
    ev = pool.emitted[0]
    assert ev["event_type"] == drs.COMPLETE_EVENT_TYPE
    assert ev["data"] == {
        "schema": 1,
        "request_id": "r1",
        "sources_healed": ["prices_daily"],
        "sources_still_red": [],
        "green": True,
    }
    assert not os.path.exists(lock_dir)  # released


# ────────────────────────────────────────────────────────────────────────
# 2. Heal makes requested green → COMPLETE green=true.
# ────────────────────────────────────────────────────────────────────────


async def test_heal_makes_green(monkeypatch, lock_dir):
    calls: list = []
    _patch_heal(
        monkeypatch,
        outcome=SelfHealOutcome(green=True, iterations=2, healed=["daily_bars"]),
        calls=calls,
    )
    # red before heal (validate-first), green after heal re-query.
    pool = _Pool(red_sequence=[["prices_daily_completeness"], []])

    terminated = await drs._handle_request(  # noqa: SLF001
        pool, _request("r2", ["prices_daily"]), lock_dir
    )

    assert terminated is True
    assert ("__run_self_heal__", {}) in calls  # heal ran
    ev = pool.emitted[-1]
    assert ev["event_type"] == drs.COMPLETE_EVENT_TYPE
    assert ev["data"]["green"] is True
    assert ev["data"]["sources_healed"] == ["prices_daily"]
    assert ev["data"]["sources_still_red"] == []


# ────────────────────────────────────────────────────────────────────────
# 3. Partial heal → COMPLETE green=false, correct sources_still_red.
# ────────────────────────────────────────────────────────────────────────


async def test_partial_heal_complete_green_false(monkeypatch, lock_dir):
    calls: list = []
    # outcome.escalated empty → transient partial, NOT escalation.
    _patch_heal(
        monkeypatch,
        outcome=SelfHealOutcome(green=False, iterations=4, healed=[]),
        calls=calls,
    )
    # Two requested sources red; after heal earnings_events recovers
    # but sec_insider_transactions stays red.
    pool = _Pool(
        red_sequence=[
            ["earnings_events_freshness", "sec_filings_freshness"],
            ["sec_filings_freshness"],
        ]
    )

    await drs._handle_request(  # noqa: SLF001
        pool,
        _request("r3", ["earnings_events", "sec_insider_transactions"]),
        lock_dir,
    )

    ev = pool.emitted[-1]
    assert ev["event_type"] == drs.COMPLETE_EVENT_TYPE
    assert ev["data"]["green"] is False
    assert ev["data"]["sources_healed"] == ["earnings_events"]
    assert ev["data"]["sources_still_red"] == ["sec_insider_transactions"]


# ────────────────────────────────────────────────────────────────────────
# 4. Escalation → DATA_REPAIR_ESCALATED with attempts.
# ────────────────────────────────────────────────────────────────────────


async def test_escalation_emits_escalated(monkeypatch, lock_dir):
    calls: list = []
    _patch_heal(
        monkeypatch,
        outcome=SelfHealOutcome(
            green=False,
            iterations=3,
            healed=[],
            escalated=[
                ("fundamentals_quarterly", "fundamentals_integrity: corruption")
            ],
        ),
        calls=calls,
    )
    pool = _Pool(
        red_sequence=[
            ["fundamentals_integrity"],
            ["fundamentals_integrity"],
        ]
    )

    await drs._handle_request(  # noqa: SLF001
        pool, _request("r4", ["fundamentals_quarterly"]), lock_dir
    )

    ev = pool.emitted[-1]
    assert ev["event_type"] == drs.ESCALATED_EVENT_TYPE
    assert ev["data"]["sources_unhealed"] == ["fundamentals_quarterly"]
    assert "corruption" in ev["data"]["reason"]
    assert ev["data"]["attempts"] == 3
    assert ev["severity"] == "ERROR"


# ────────────────────────────────────────────────────────────────────────
# 5. Exactly-once: a request whose terminal already exists is skipped.
# ────────────────────────────────────────────────────────────────────────


async def test_preexisting_terminal_skips_no_second_emit(
    monkeypatch, lock_dir
):
    calls: list = []
    _patch_heal(monkeypatch, outcome=None, calls=calls)
    pool = _Pool(red_sequence=[[]])
    # Simulate a terminal already persisted for r5 (e.g. emitted before
    # a daemon crash/restart).
    pool.emitted.append(
        {
            "engine": drs.DAEMON_ENGINE_TAG,
            "event_type": drs.COMPLETE_EVENT_TYPE,
            "severity": "INFO",
            "message": "prior",
            "data": {"schema": 1, "request_id": "r5", "green": True},
        }
    )

    terminated = await drs._handle_request(  # noqa: SLF001
        pool, _request("r5", ["prices_daily"]), lock_dir
    )

    assert terminated is True  # cursor may advance — already terminal
    assert calls == []  # no validation, no heal
    assert len(pool.emitted) == 1  # NO second emit
    assert not os.path.exists(lock_dir)  # lock never acquired


# ────────────────────────────────────────────────────────────────────────
# 6. Lock contention: live pid → defer, no heal, cursor frozen, then
#    heals on the next tick once the lock frees.
# ────────────────────────────────────────────────────────────────────────


async def test_lock_held_by_live_pid_defers_then_heals(
    monkeypatch, lock_dir
):
    calls: list = []
    _patch_heal(monkeypatch, outcome=None, calls=calls)

    t0 = datetime.now(UTC) - timedelta(minutes=5)
    pool = _Pool(
        red_sequence=[[]],  # green once it gets to run
        requests=[(t0, _request("r6", ["prices_daily"]))],
    )

    # Hold the lock with THIS (alive) process's pid.
    os.mkdir(lock_dir)
    with open(os.path.join(lock_dir, "pid"), "w", encoding="utf-8") as fh:
        fh.write(str(os.getpid()))

    cursor = t0 - timedelta(seconds=1)

    # Tick 1: lock held by a live pid → defer.
    new_cursor = await drs._process_batch(pool, cursor, lock_dir)  # noqa: SLF001
    assert new_cursor == cursor  # cursor NOT advanced past deferred req
    assert pool.emitted == []  # no terminal emitted
    assert calls == []  # no validation, no heal
    # The held lock is untouched (belongs to the "other" process).
    assert os.path.exists(lock_dir)

    # Other process finishes — release the lock.
    drs._release_lock(lock_dir)  # noqa: SLF001

    # Tick 2: lock free → request is retried and heals (green path).
    new_cursor = await drs._process_batch(pool, cursor, lock_dir)  # noqa: SLF001
    assert new_cursor == t0  # advanced past now-terminated request
    assert len(pool.emitted) == 1
    assert pool.emitted[0]["event_type"] == drs.COMPLETE_EVENT_TYPE
    assert pool.emitted[0]["data"]["request_id"] == "r6"
    assert not os.path.exists(lock_dir)  # released after heal


async def test_dead_pid_lock_is_reclaimed(monkeypatch, lock_dir):
    calls: list = []
    _patch_heal(monkeypatch, outcome=None, calls=calls)
    pool = _Pool(red_sequence=[[]])

    # Stale lock dir with a dead pid (PID 2**31-1 won't exist).
    os.mkdir(lock_dir)
    with open(os.path.join(lock_dir, "pid"), "w", encoding="utf-8") as fh:
        fh.write(str(2**31 - 1))

    terminated = await drs._handle_request(  # noqa: SLF001
        pool, _request("r7", ["prices_daily"]), lock_dir
    )

    assert terminated is True  # reclaimed + healed, not deferred
    assert pool.emitted[-1]["event_type"] == drs.COMPLETE_EVENT_TYPE
    assert not os.path.exists(lock_dir)


# ────────────────────────────────────────────────────────────────────────
# 7. Malformed + duplicate request_id within one poll batch.
# ────────────────────────────────────────────────────────────────────────


async def test_malformed_request_advances_without_emit(
    monkeypatch, lock_dir
):
    calls: list = []
    _patch_heal(monkeypatch, outcome=None, calls=calls)
    pool = _Pool(red_sequence=[[]])

    # No request_id — cannot correlate a reply; drop (advance past).
    terminated = await drs._handle_request(  # noqa: SLF001
        pool, {"schema": 1, "engine": "vector", "sources": ["prices_daily"]},
        lock_dir,
    )
    assert terminated is True
    assert pool.emitted == []
    assert calls == []
    assert not os.path.exists(lock_dir)


async def test_duplicate_request_id_in_batch_idempotent(
    monkeypatch, lock_dir
):
    calls: list = []
    _patch_heal(monkeypatch, outcome=None, calls=calls)

    t0 = datetime.now(UTC) - timedelta(minutes=5)
    t1 = t0 + timedelta(seconds=1)
    # Same request_id appears twice in one poll batch (engine retry).
    pool = _Pool(
        red_sequence=[[]],
        requests=[
            (t0, _request("dup", ["prices_daily"])),
            (t1, _request("dup", ["prices_daily"])),
        ],
    )
    cursor = t0 - timedelta(seconds=1)

    new_cursor = await drs._process_batch(pool, cursor, lock_dir)  # noqa: SLF001

    # Exactly ONE terminal for request_id 'dup' — the 2nd occurrence
    # sees the terminal the 1st emitted and is skipped.
    terminals = [
        e
        for e in pool.emitted
        if e["data"].get("request_id") == "dup"
        and e["event_type"] in drs.TERMINAL_EVENT_TYPES
    ]
    assert len(terminals) == 1
    assert new_cursor == t1  # advanced past both
