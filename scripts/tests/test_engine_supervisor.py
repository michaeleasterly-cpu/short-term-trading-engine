import ast
import contextlib
import inspect
import json
import sys
import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

# ops/ vs scripts/ops.py top-level name collision guard (identical to
# scripts/tests/test_engine_dispatch.py — repo root first, evict any
# non-package `ops`/`ops.*` so the real ops/ package resolves).
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
for _m in [m for m in list(sys.modules) if m == "ops" or m.startswith("ops.")]:
    if not hasattr(sys.modules[_m], "__path__"):
        del sys.modules[_m]

from ops import engine_supervisor as es  # noqa: E402

# pytest-xdist: pin this ops-shadow module to one worker so its
# sys.modules['ops'] / scripts/ops.py loading stays single-process
# (the ops/ package-shadow is a single-process invariant). P1.3.
pytestmark = pytest.mark.xdist_group("ops_shadow")


class _RecConn:
    def __init__(self):
        self.inserts: list[tuple] = []

    async def fetchrow(self, *_a, **_k):
        return None

    async def fetch(self, *_a, **_k):
        return []

    async def fetchval(self, *_a, **_k):
        return None

    async def execute(self, sql, *args):
        self.inserts.append((sql, args))


class _RecPool:
    def __init__(self):
        self.conn = _RecConn()

    @contextlib.asynccontextmanager
    async def acquire(self):
        yield self.conn


async def test_emit_held_writes_locked_payload():
    pool = _RecPool()
    await es._emit_held(pool, "reversion", "h-1", "crashed_startup", "stale")
    sql, args = pool.conn.inserts[-1]
    assert "INSERT INTO platform.application_log" in sql
    payload = json.loads(args[-1])
    assert payload == {"schema": 1, "hold_id": "h-1", "engine": "reversion",
                       "failure_class": "crashed_startup", "reason": "stale"}
    assert args[2] == "ENGINE_HELD"


async def test_supervise_is_crash_isolated():
    # A detector raising must NOT propagate (sweep must never abort).
    with patch.object(es, "_detect_and_act",
                      new=AsyncMock(side_effect=RuntimeError("boom"))):
        await es.supervise(_RecPool(), "reversion",
                           datetime(2026, 5, 5, 21, 30, tzinfo=UTC),
                           AsyncMock())  # must not raise


def _rows_conn(rows_by_call):
    """A conn whose fetchrow returns queued rows in order, execute records."""
    class _C:
        def __init__(self):
            self.inserts = []
            self._q = list(rows_by_call)

        async def fetchrow(self, *_a, **_k):
            return self._q.pop(0) if self._q else None

        async def fetch(self, *_a, **_k):
            return []

        async def fetchval(self, *_a, **_k):
            return None

        async def execute(self, sql, *args):
            self.inserts.append((sql, args))
    return _C()


def _pool_for(conn):
    class _P:
        @contextlib.asynccontextmanager
        async def acquire(self):
            yield conn
    return _P()


async def test_crashed_startup_self_heals_then_recovered():
    now = datetime(2026, 5, 5, 21, 30, tzinfo=UTC)
    stale = datetime(2026, 5, 5, 14, 0, tzinfo=UTC)  # > 2h before now
    conn = _rows_conn([
        None,
        {"started_at": stale, "completed": False},
        {"started_at": now, "completed": True},
    ])
    invoke = AsyncMock()
    await es.supervise(_pool_for(conn), "reversion", now, invoke)
    invoke.assert_awaited()  # re-invoked as self-heal
    events = [a[2] for _s, a in conn.inserts]
    assert "ENGINE_SUPERVISOR_RECOVERED" in events
    assert "ENGINE_HELD" not in events


async def test_crashed_startup_unrecovered_escalates_and_holds():
    now = datetime(2026, 5, 5, 21, 30, tzinfo=UTC)
    stale = datetime(2026, 5, 5, 14, 0, tzinfo=UTC)
    rows = [None, {"started_at": stale, "completed": False}]
    rows += [{"started_at": stale, "completed": False}] * (es._MAX_REINVOKE + 1)
    conn = _rows_conn(rows)
    await es.supervise(_pool_for(conn), "reversion", now, AsyncMock())
    events = [a[2] for _s, a in conn.inserts]
    assert "ENGINE_ESCALATED" in events
    assert "ENGINE_HELD" in events
    assert "ENGINE_SUPERVISOR_RECOVERED" not in events


async def test_no_failure_no_events():
    now = datetime(2026, 5, 5, 21, 30, tzinfo=UTC)
    conn = _rows_conn([None, {"started_at": None, "completed": False}])
    invoke = AsyncMock()
    await es.supervise(_pool_for(conn), "reversion", now, invoke)
    invoke.assert_not_awaited()
    assert conn.inserts == []


async def test_already_held_skips_redetection_idempotent():
    now = datetime(2026, 5, 5, 21, 30, tzinfo=UTC)
    from tpcore.supervisor_state import HoldState
    held = HoldState("h-1", "crashed_startup", "stale",
                     datetime(2026, 5, 5, 14, 0, tzinfo=UTC))
    with patch.object(es, "current_hold", new=AsyncMock(return_value=held)), \
         patch.object(es, "_auto_clear", new=AsyncMock()) as clear:
        conn = _rows_conn([])
        await es.supervise(_pool_for(conn), "reversion", now, AsyncMock())
    assert all(a[2] != "ENGINE_HELD" for _s, a in conn.inserts)
    clear.assert_awaited_once()


async def test_scheduler_crash_nonzero_shutdown_detected_and_self_heals():
    now = datetime(2026, 5, 5, 21, 30, tzinfo=UTC)
    conn = _rows_conn([
        None,                                   # current_hold
        {"started_at": None, "completed": False},  # crashed_startup: no
        {"crashed": True},                      # scheduler_crash detect
        {"crashed": False},                     # verify after re-invoke
    ])
    invoke = AsyncMock()
    await es.supervise(_pool_for(conn), "reversion", now, invoke)
    invoke.assert_awaited()
    assert any(a[2] == "ENGINE_SUPERVISOR_RECOVERED" for _s, a in conn.inserts)


async def test_data_repair_escalated_holds_without_selfheal():
    now = datetime(2026, 5, 5, 21, 30, tzinfo=UTC)
    conn = _rows_conn([
        None,                                   # current_hold
        {"started_at": None, "completed": False},  # crashed_startup: no
        {"crashed": False},                     # scheduler_crash: no
        {"open": False},                        # data_request_timeout: no
        {"escalated": True},                    # data_repair_escalated: yes
    ])
    invoke = AsyncMock()
    await es.supervise(_pool_for(conn), "vector", now, invoke)
    invoke.assert_not_awaited()  # no self-heal possible
    events = [a[2] for _s, a in conn.inserts]
    assert "ENGINE_ESCALATED" in events and "ENGINE_HELD" in events


async def test_missed_cycle_excludes_held_windows():
    now = datetime(2026, 5, 5, 21, 30, tzinfo=UTC)
    conn = _rows_conn([
        None,                                   # current_hold
        {"started_at": None, "completed": False},  # crashed_startup: no
        {"crashed": False},                     # scheduler_crash: no
        {"open": False},                        # data_request_timeout: no
        {"escalated": False},                   # data_repair_escalated: no
        {"startups": 0, "eligible_windows": es._MISSED_CYCLES_N},  # missed
    ])
    invoke = AsyncMock()
    await es.supervise(_pool_for(conn), "momentum", now, invoke)
    invoke.assert_awaited()  # missed_cycle self-heals via re-invoke


async def test_auto_clear_requires_clean_shutdown_not_just_startup():
    from tpcore.supervisor_state import HoldState
    now = datetime(2026, 5, 6, 21, 30, tzinfo=UTC)
    held = HoldState("h-1", "crashed_startup", "stale",
                     datetime(2026, 5, 5, 21, 0, tzinfo=UTC))
    conn = _rows_conn([{"clean": False}])  # post-hold STARTUP but NO clean SHUTDOWN
    with patch.object(es, "current_hold", new=AsyncMock(return_value=held)):
        await es.supervise(_pool_for(conn), "reversion", now, AsyncMock())
    assert all(a[2] != "ENGINE_CLEARED" for _s, a in conn.inserts)


async def test_auto_clear_emits_cleared_on_clean_cycle():
    from tpcore.supervisor_state import HoldState
    now = datetime(2026, 5, 6, 21, 30, tzinfo=UTC)
    held = HoldState("h-1", "crashed_startup", "stale",
                     datetime(2026, 5, 5, 21, 0, tzinfo=UTC))
    conn = _rows_conn([{"clean": True}])  # STARTUP + clean SHUTDOWN exit0 post-hold
    with patch.object(es, "current_hold", new=AsyncMock(return_value=held)):
        await es.supervise(_pool_for(conn), "reversion", now, AsyncMock())
    cleared = [a for _s, a in conn.inserts if a[2] == "ENGINE_CLEARED"]
    assert len(cleared) == 1
    payload = json.loads(cleared[0][-1])
    assert payload["hold_id"] == "h-1" and payload["schema"] == 1


async def test_auto_clear_repair_escalated_needs_repair_complete_green():
    from tpcore.supervisor_state import HoldState
    now = datetime(2026, 5, 6, 21, 30, tzinfo=UTC)
    held = HoldState("h-2", "data_repair_escalated", "data lane exhausted",
                     datetime(2026, 5, 5, 21, 0, tzinfo=UTC))
    conn = _rows_conn([{"clean": True}, {"green": False}])  # clean but no repair-green
    with patch.object(es, "current_hold", new=AsyncMock(return_value=held)):
        await es.supervise(_pool_for(conn), "vector", now, AsyncMock())
    assert all(a[2] != "ENGINE_CLEARED" for _s, a in conn.inserts)


async def test_crashed_startup_recent_incomplete_is_not_detected():
    now = datetime(2026, 5, 5, 21, 30, tzinfo=UTC)
    recent = now - timedelta(seconds=es._STALE_STARTUP_SECONDS // 2)  # NOT stale
    conn = _rows_conn([
        None,                                           # current_hold: not held
        {"started_at": recent, "completed": False},     # crashed_startup detect
        {"crashed": False},                             # scheduler_crash
        {"open": False},                                # data_request_timeout
        {"escalated": False},                           # data_repair_escalated
        {"startups": 1, "eligible_windows": 1},         # missed_cycle: no
    ])
    invoke = AsyncMock()
    await es.supervise(_pool_for(conn), "reversion", now, invoke)
    invoke.assert_not_awaited()           # recent-incomplete is in-flight, NOT crashed
    assert conn.inserts == []             # no HELD/ESCALATED/RECOVERED


async def test_crashed_startup_completed_run_is_not_detected():
    now = datetime(2026, 5, 5, 21, 30, tzinfo=UTC)
    stale = now - timedelta(seconds=es._STALE_STARTUP_SECONDS * 2)
    conn = _rows_conn([
        None,                                           # current_hold: not held
        {"started_at": stale, "completed": True},       # completed → NOT crashed
        {"crashed": False},                             # scheduler_crash
        {"open": False},                                # data_request_timeout
        {"escalated": False},                           # data_repair_escalated
        {"startups": 1, "eligible_windows": 1},         # missed_cycle: no
    ])
    invoke = AsyncMock()
    await es.supervise(_pool_for(conn), "reversion", now, invoke)
    invoke.assert_not_awaited()
    assert conn.inserts == []


async def test_auto_clear_ignores_behavioral_holds():
    """DA-2 seam guard: DA-1's _auto_clear must NOT clear a hold whose
    failure_class is not one of DA-1's infra classes (behavioral holds
    are DA-2-owned, operator-cleared)."""
    from tpcore.supervisor_state import HoldState
    now = datetime(2026, 5, 6, 21, 30, tzinfo=UTC)
    held = HoldState("h-b", "behavioral", "drawdown_period: fp-1",
                     datetime(2026, 5, 5, 21, 0, tzinfo=UTC))
    conn = _rows_conn([{"clean": True}])
    with patch.object(es, "current_hold", new=AsyncMock(return_value=held)):
        await es.supervise(_pool_for(conn), "reversion", now, AsyncMock())
    assert all(a[2] != "ENGINE_CLEARED" for _s, a in conn.inserts)


async def test_auto_clear_still_clears_each_infra_class():
    """Behavior-preserving: every DA-1 infra class still auto-clears on
    a clean cycle (the guard must not regress DA-1)."""
    from tpcore.supervisor_state import HoldState
    now = datetime(2026, 5, 6, 21, 30, tzinfo=UTC)
    for fc in ("crashed_startup", "scheduler_crash",
               "data_request_timeout", "missed_cycle"):
        held = HoldState(f"h-{fc}", fc, "x",
                         datetime(2026, 5, 5, 21, 0, tzinfo=UTC))
        conn = _rows_conn([{"clean": True}])
        with patch.object(es, "current_hold",
                          new=AsyncMock(return_value=held)):
            await es.supervise(_pool_for(conn), "reversion", now,
                               AsyncMock())
        cleared = [a for _s, a in conn.inserts if a[2] == "ENGINE_CLEARED"]
        assert len(cleared) == 1, f"{fc} must still auto-clear"


def test_classify_emittable_set_is_pinned_to_constant():
    """GENUINE clockwork pin (spec §3/D-EL-9): AST-walk _classify and
    collect every string literal returned as the failure-class element
    of `return "<cls>", <bool>`. That set MUST equal
    INFRA_FAILURE_CLASSES. A new DA-1 detector adding a `return
    "new_cls", x` arm fails THIS test until INFRA_FAILURE_CLASSES (and,
    via the engine-ladder drift test, a DispositionPolicy) is updated —
    closing the most common add-a-class path. Not a hand-maintained
    list: it reads the real function source."""
    src = inspect.getsource(es._classify)
    tree = ast.parse(textwrap.dedent(src))
    emitted: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Tuple):
            first = node.value.elts[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                emitted.add(first.value)
    assert emitted == set(es.INFRA_FAILURE_CLASSES), (
        f"_classify emits {emitted} but INFRA_FAILURE_CLASSES is "
        f"{set(es.INFRA_FAILURE_CLASSES)} — a DA-1 class changed "
        f"without updating the SoT (R2 tooth)")


async def test_classify_known_detectors_yield_their_class():
    """Non-vacuous behavior smoke: each of the 5 known detectors, True
    in isolation, drives _classify to its own class (the AST test above
    is the actual clockwork tooth; this guards the wiring stays sane)."""
    cases = (
        ("_detect_crashed_startup", "crashed_startup"),
        ("_detect_scheduler_crash", "scheduler_crash"),
        ("_detect_data_request_timeout", "data_request_timeout"),
        ("_detect_data_repair_escalated", "data_repair_escalated"),
        ("_detect_missed_cycle", "missed_cycle"),
    )
    names = tuple(n for n, _ in cases)
    for detector, expected in cases:
        ctx = [patch.object(es, d, new=AsyncMock(return_value=(d == detector)))
               for d in names]
        for c in ctx:
            c.__enter__()
        try:
            cls, _heal = await es._classify(
                object(), "reversion",
                datetime(2026, 5, 6, tzinfo=UTC),
                datetime(2026, 5, 6, tzinfo=UTC))
        finally:
            for c in ctx:
                c.__exit__(None, None, None)
        assert cls == expected
        assert cls in es.INFRA_FAILURE_CLASSES


def test_infra_failure_classes_is_the_five_da1_classes():
    assert es.INFRA_FAILURE_CLASSES == frozenset({
        "crashed_startup", "scheduler_crash", "data_request_timeout",
        "data_repair_escalated", "missed_cycle"})
