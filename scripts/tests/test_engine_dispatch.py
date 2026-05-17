import contextlib
import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# scripts/ops.py (data-ops CLI) and the ops/ daemons package share the
# top-level name `ops`; tpcore/tests/test_ops.py does
# `sys.path.insert(0, scripts/); import ops`, so under full-suite
# collection sys.modules['ops'] is already bound to the scripts/ops.py
# MODULE (no .__path__) before this file is imported and Python won't
# re-resolve a cached name. Put repo root FIRST, then evict any
# non-package `ops`/`ops.*` so the real ops/ regular package
# (ops/__init__.py) resolves. The module OBJECT `ed` and the
# `dispatch_once`/`ROSTER` names are then bound from ONE import, so
# patching via patch.object(ed, ...) is identity-stable regardless of
# full-suite sys.modules churn. (Root collision = pre-existing tech-debt.)
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
for _m in [m for m in list(sys.modules) if m == "ops" or m.startswith("ops.")]:
    if not hasattr(sys.modules[_m], "__path__"):
        del sys.modules[_m]

from ops import engine_dispatch as ed  # noqa: E402
from ops.engine_dispatch import ROSTER, dispatch_once  # noqa: E402
from tpcore.engine_profile import FireDecision  # noqa: E402

# Save the real _invoke_allocator before the autouse fixture replaces it,
# so the three unit tests below can call the real implementation directly.
_real_invoke_allocator = ed._invoke_allocator


@pytest.fixture(autouse=True)
def _no_real_allocator():
    """Sub-project C: dispatch_once now runs the allocator first.
    Neutralize the real subprocess for every test that doesn't
    explicitly exercise it (allocator-specific tests patch
    ed._invoke_allocator / ed._dispatch_allocator themselves)."""
    with patch.object(ed, "_invoke_allocator", AsyncMock()):
        yield


class _Conn:
    async def fetchval(self, *_a, **_k): return None
    async def fetch(self, *_a, **_k): return []
    async def execute(self, *_a, **_k): return None
class _Pool:
    @contextlib.asynccontextmanager
    async def acquire(self):
        yield _Conn()


async def test_fires_only_engines_should_fire_approves():
    fire = FireDecision(True, "ready", {"data_ready": True})
    nofire = FireDecision(False, "not a cadence boundary", {"data_ready": True})
    sf = AsyncMock(side_effect=lambda eng, now, pool: fire if eng == "reversion" else nofire)
    invoked = []
    with patch.object(ed, "should_fire", sf), \
         patch.object(ed, "_invoke_scheduler", new=AsyncMock(side_effect=lambda e: invoked.append(e))):
        await dispatch_once(_Pool(), now=datetime(2026, 5, 5, 21, 30, tzinfo=UTC))
    assert invoked == ["reversion"]


async def test_roster_is_the_four_live_engines():
    assert ROSTER == ("reversion", "vector", "momentum", "sentinel")


async def test_data_blocked_emits_one_request_and_skips_never_heals():
    nofire = FireDecision(False, "data not ready: stale", {"data_ready": False})
    inserts = []
    class _C:
        async def fetchrow(self, *_a, **_k): return None  # no request this window
        async def fetchval(self, *_a, **_k): return None
        async def fetch(self, *_a, **_k): return []
        async def execute(self, sql, *args): inserts.append((sql, args))
    class _P:
        @contextlib.asynccontextmanager
        async def acquire(self): yield _C()
    with patch.object(ed, "should_fire", AsyncMock(return_value=nofire)), \
         patch.object(ed, "failing_sources_for_engine",
               new=AsyncMock(return_value=["prices_daily"])), \
         patch.object(ed, "_invoke_scheduler", new=AsyncMock()) as inv:
        await dispatch_once(_P(), now=datetime(2026,5,5,21,30,tzinfo=UTC))
    inv.assert_not_called()
    payloads = [a for s, a in inserts if "INSERT INTO platform.application_log" in s]
    assert len(payloads) == 5  # allocator + one ENGINE_DATA_REQUEST per ROSTER engine (all data-blocked)
    data = json.loads(payloads[1][-1])  # payloads[0] is allocator; [1] is first ROSTER engine
    assert data["schema"] == 1 and data["engine"] in ROSTER
    assert data["sources"] == ["prices_daily"]
    uuid.UUID(data["request_id"])  # valid uuid


async def test_open_request_is_not_re_emitted():
    # T3 dedup intent preserved under the new state-based probe: an open
    # request with NO terminal event and within the timeout window must
    # NOT trigger a re-emit (one open request per engine/cadence-window).
    nofire = FireDecision(False, "data not ready", {"data_ready": False})
    class _C:
        async def fetchrow(self, *_a, **_k):
            # open request, no terminal, recent (30m before `now`)
            return {"request_id": "open-req", "terminal": None, "green": None,
                    "req_ts": datetime(2026, 5, 5, 21, 0, tzinfo=UTC)}
        async def fetchval(self, *_a, **_k): return None
        async def fetch(self, *_a, **_k): return []
        async def execute(self, *_a, **_k): raise AssertionError("must not insert when request open")
    class _P:
        @contextlib.asynccontextmanager
        async def acquire(self): yield _C()
    with patch.object(ed, "should_fire", AsyncMock(return_value=nofire)), \
         patch.object(ed, "failing_sources_for_engine", new=AsyncMock(return_value=["prices_daily"])), \
         patch.object(ed, "_invoke_scheduler", new=AsyncMock()):
        await dispatch_once(_P(), now=datetime(2026,5,5,21,30,tzinfo=UTC))


async def test_stale_startup_without_completion_is_refired():
    # DA-1: crashed-startup re-invoke is now owned by engine_supervisor.
    # dispatch_once calls supervise(pool, engine, now, invoke) per actor
    # BEFORE _dispatch_engine; assert the supervisor is awaited for each
    # actor (re-invoke is the supervisor's job — tested in test_engine_supervisor.py).
    already = FireDecision(False, "already ran this cycle",
                           {"data_ready": True, "not_already_run": False})
    sup = AsyncMock()
    with patch.object(ed, "should_fire", AsyncMock(return_value=already)), \
         patch.object(ed.engine_supervisor, "supervise", sup), \
         patch.object(ed, "_invoke_scheduler", new=AsyncMock()) as inv:
        await dispatch_once(object(), now=datetime(2026,5,5,21,30,tzinfo=UTC))
    # supervisor was called for allocator + every ROSTER engine
    assert sup.await_count == 1 + len(ROSTER)
    engines_supervised = [c.args[1] for c in sup.await_args_list]
    assert engines_supervised[0] == "allocator"
    assert engines_supervised[1:] == list(ROSTER)
    # dispatch path only logs skip (no re-invoke from _dispatch_engine)
    inv.assert_not_called()



async def test_already_ran_branch_is_skip_only_never_reinvokes():
    """DA-1: the crashed-startup re-invoke moved to engine_supervisor.
    The dispatch already-ran branch must now be a pure skip — it must
    NOT re-invoke (regression guard against re-adding refire here).
    Boundary semantics (recent/completed → no refire) are covered in
    test_engine_supervisor.py."""
    ran = FireDecision(False, "already ran this cycle",
                       {"profiled": True, "cadence": True,
                        "market_closed": True, "supervisor_held": True,
                        "data_ready": True, "not_already_run": False})
    with patch.object(ed, "should_fire", AsyncMock(return_value=ran)), \
         patch.object(ed.engine_supervisor, "supervise", AsyncMock()), \
         patch.object(ed, "_invoke_scheduler", new=AsyncMock()) as inv, \
         patch.object(ed, "_invoke_allocator", new=AsyncMock()) as alloc:
        await dispatch_once(_Pool(), now=datetime(2026, 5, 5, 21, 30, tzinfo=UTC))
    inv.assert_not_called()     # already-ran → no engine scheduler re-invoke
    alloc.assert_not_called()   # already-ran → no allocator re-invoke


# ---------------------------------------------------------------------------
# TASK 5 — terminal-event handling + bounded timeout
# ---------------------------------------------------------------------------
_NOW = datetime(2026, 5, 5, 21, 30, tzinfo=UTC)


def _state_pool(state_row):
    """Pool/conn whose fetchrow returns the _open_request_state shape and
    whose execute records inserts."""
    inserts: list = []

    class _C:
        async def fetchrow(self, *_a, **_k):
            return state_row
        async def fetchval(self, *_a, **_k):
            return None
        async def fetch(self, *_a, **_k):
            return []
        async def execute(self, sql, *args):
            inserts.append((sql, args))

    class _P:
        @contextlib.asynccontextmanager
        async def acquire(self):
            yield _C()

    return _P(), inserts


async def test_repair_complete_green_refires_when_should_fire_now_true():
    nofire = FireDecision(False, "data not ready: stale", {"data_ready": False})
    refire = FireDecision(True, "ready", {"data_ready": True})
    state = {
        "request_id": "req-green-1",
        "req_ts": datetime(2026, 5, 5, 20, 0, tzinfo=UTC),
        "terminal": "DATA_REPAIR_COMPLETE",
        "green": True,
    }
    pool, inserts = _state_pool(state)
    # First call (per-engine loop) → data-blocked; the re-decision after
    # repair → fire. side_effect cycles: nofire then refire, repeated.
    seq = []

    async def _sf(eng, now, pool_):
        seq.append(eng)
        # odd index = initial loop decision, even = re-decision
        return nofire if seq.count(eng) == 1 else refire

    invoked = []
    with patch.object(ed, "should_fire", AsyncMock(side_effect=_sf)), \
         patch.object(ed, "failing_sources_for_engine",
               new=AsyncMock(return_value=["prices_daily"])), \
         patch.object(ed, "_invoke_scheduler",
               new=AsyncMock(side_effect=lambda e: invoked.append(e))):
        await dispatch_once(pool, now=_NOW)
    assert invoked == list(ROSTER)
    # no new ENGINE_DATA_REQUEST emitted when a terminal already exists
    assert not [a for s, a in inserts
                if "INSERT INTO platform.application_log" in s]


async def test_repair_complete_green_but_still_no_fire_does_not_invoke():
    nofire = FireDecision(False, "data not ready", {"data_ready": False})
    still = FireDecision(False, "not a cadence boundary", {"data_ready": True})
    state = {
        "request_id": "req-green-2",
        "req_ts": datetime(2026, 5, 5, 20, 0, tzinfo=UTC),
        "terminal": "DATA_REPAIR_COMPLETE",
        "green": True,
    }
    pool, inserts = _state_pool(state)
    seq = []

    async def _sf(eng, now, pool_):
        seq.append(eng)
        return nofire if seq.count(eng) == 1 else still

    with patch.object(ed, "should_fire", AsyncMock(side_effect=_sf)), \
         patch.object(ed, "failing_sources_for_engine",
               new=AsyncMock(return_value=["prices_daily"])), \
         patch.object(ed, "_invoke_scheduler", new=AsyncMock()) as inv:
        await dispatch_once(pool, now=_NOW)
    inv.assert_not_called()
    assert not [a for s, a in inserts
                if "INSERT INTO platform.application_log" in s]


async def test_repair_escalated_skips_and_does_not_invoke():
    nofire = FireDecision(False, "data not ready", {"data_ready": False})
    state = {
        "request_id": "req-esc-1",
        "req_ts": datetime(2026, 5, 5, 20, 0, tzinfo=UTC),
        "terminal": "DATA_REPAIR_ESCALATED",
        "green": None,
    }
    pool, inserts = _state_pool(state)
    with patch.object(ed, "should_fire", AsyncMock(return_value=nofire)), \
         patch.object(ed, "failing_sources_for_engine",
               new=AsyncMock(return_value=["prices_daily"])), \
         patch.object(ed, "_invoke_scheduler", new=AsyncMock()) as inv:
        await dispatch_once(pool, now=_NOW)
    inv.assert_not_called()
    assert not [a for s, a in inserts
                if "INSERT INTO platform.application_log" in s]


async def test_repair_complete_not_green_skips_and_does_not_invoke():
    nofire = FireDecision(False, "data not ready", {"data_ready": False})
    state = {
        "request_id": "req-notgreen-1",
        "req_ts": datetime(2026, 5, 5, 20, 0, tzinfo=UTC),
        "terminal": "DATA_REPAIR_COMPLETE",
        "green": False,
    }
    pool, inserts = _state_pool(state)
    with patch.object(ed, "should_fire", AsyncMock(return_value=nofire)), \
         patch.object(ed, "failing_sources_for_engine",
               new=AsyncMock(return_value=["prices_daily"])), \
         patch.object(ed, "_invoke_scheduler", new=AsyncMock()) as inv:
        await dispatch_once(pool, now=_NOW)
    inv.assert_not_called()
    assert not [a for s, a in inserts
                if "INSERT INTO platform.application_log" in s]


async def test_open_request_timed_out_skips_and_alarms():
    nofire = FireDecision(False, "data not ready", {"data_ready": False})
    # req_ts > 90 min before now (now=21:30; req at 19:00 = 150 min)
    state = {
        "request_id": "req-timeout-1",
        "req_ts": datetime(2026, 5, 5, 19, 0, tzinfo=UTC),
        "terminal": None,
        "green": None,
    }
    pool, inserts = _state_pool(state)
    with patch.object(ed, "should_fire", AsyncMock(return_value=nofire)), \
         patch.object(ed, "failing_sources_for_engine",
               new=AsyncMock(return_value=["prices_daily"])), \
         patch.object(ed, "_invoke_scheduler", new=AsyncMock()) as inv, \
         patch.object(ed, "logger") as log:
        await dispatch_once(pool, now=_NOW)
    inv.assert_not_called()
    assert not [a for s, a in inserts
                if "INSERT INTO platform.application_log" in s]
    assert any(c.args and c.args[0] == "engine_dispatch.data_request_timeout"
               for c in log.error.call_args_list)


async def test_open_request_within_timeout_skips_no_emit_no_invoke():
    nofire = FireDecision(False, "data not ready", {"data_ready": False})
    # req_ts only 30 min before now → still in flight, no terminal yet
    state = {
        "request_id": "req-open-1",
        "req_ts": datetime(2026, 5, 5, 21, 0, tzinfo=UTC),
        "terminal": None,
        "green": None,
    }
    pool, inserts = _state_pool(state)
    with patch.object(ed, "should_fire", AsyncMock(return_value=nofire)), \
         patch.object(ed, "failing_sources_for_engine",
               new=AsyncMock(return_value=["prices_daily"])), \
         patch.object(ed, "_invoke_scheduler", new=AsyncMock()) as inv:
        await dispatch_once(pool, now=_NOW)
    inv.assert_not_called()
    # DEDUP: open request, not timed out → MUST NOT re-emit
    assert not [a for s, a in inserts
                if "INSERT INTO platform.application_log" in s]


async def test_no_request_yet_emits_one_data_request():
    nofire = FireDecision(False, "data not ready: stale", {"data_ready": False})
    pool, inserts = _state_pool(None)  # _open_request_state → None
    with patch.object(ed, "should_fire", AsyncMock(return_value=nofire)), \
         patch.object(ed, "failing_sources_for_engine",
               new=AsyncMock(return_value=["prices_daily"])), \
         patch.object(ed, "_invoke_scheduler", new=AsyncMock()) as inv:
        await dispatch_once(pool, now=_NOW)
    inv.assert_not_called()
    payloads = [a for s, a in inserts
                if "INSERT INTO platform.application_log" in s]
    assert len(payloads) == len(ROSTER) + 1  # allocator + one request per ROSTER engine
    data = json.loads(payloads[1][-1])  # payloads[0] is allocator; [1] is first ROSTER engine
    assert data["schema"] == 1 and data["engine"] in ROSTER
    assert data["sources"] == ["prices_daily"]
    uuid.UUID(data["request_id"])


async def test_invoke_failure_is_isolated_per_engine():
    """A raising _invoke_scheduler for one engine must not abort the sweep."""
    fire = FireDecision(True, "ready", {"data_ready": True})
    calls = []

    async def _inv(engine):
        calls.append(engine)
        if engine == "reversion":
            raise OSError("subprocess spawn failed")
        return 0

    with patch.object(ed, "should_fire", AsyncMock(return_value=fire)), \
         patch.object(ed, "_invoke_scheduler",
               new=AsyncMock(side_effect=_inv)), \
         patch.object(ed, "logger") as log:
        await dispatch_once(_Pool(), now=_NOW)
    # every engine attempted despite reversion raising
    assert calls == list(ROSTER)
    assert any(c.args and c.args[0] == "engine_dispatch.invoke_failed"
               for c in log.error.call_args_list)


async def test_dispatch_once_delegates_each_roster_engine_to_dispatch_engine():
    """dispatch_once delegates per-engine work to _dispatch_engine with
    _safe_invoke as the injected invoker (the extraction seam)."""
    calls: list[tuple[str, object]] = []

    async def _spy(pool, now, engine, invoke):
        calls.append((engine, invoke))

    pool = object()
    now = datetime(2026, 5, 18, 13, 0, tzinfo=UTC)
    with patch.object(ed, "_dispatch_engine", _spy), \
         patch.object(ed, "_dispatch_allocator", AsyncMock()):
        await dispatch_once(pool, now)

    assert [c[0] for c in calls] == list(ROSTER)
    assert all(c[1] is ed._safe_invoke for c in calls)


async def test_invoke_allocator_runs_canonical_command_exit_zero():
    proc = AsyncMock()
    proc.wait = AsyncMock(return_value=0)
    with patch.object(ed.asyncio, "create_subprocess_exec",
                      AsyncMock(return_value=proc)) as spawn, \
         patch.object(ed, "logger") as log:
        await _real_invoke_allocator("allocator")
    args = spawn.call_args[0]
    assert args[0] == sys.executable
    assert args[1:] == ("scripts/ops.py", "--allocate")
    assert any(c.args and c.args[0] == "engine_dispatch.allocator_done"
               for c in log.info.call_args_list)
    assert not any(c.args and c.args[0] == "engine_dispatch.allocator_failed"
                   for c in log.error.call_args_list)


async def test_invoke_allocator_nonzero_exit_alarms_and_returns():
    proc = AsyncMock()
    proc.wait = AsyncMock(return_value=2)
    with patch.object(ed.asyncio, "create_subprocess_exec",
                      AsyncMock(return_value=proc)), \
         patch.object(ed, "logger") as log:
        await _real_invoke_allocator("allocator")  # must NOT raise
    assert any(c.args and c.args[0] == "engine_dispatch.allocator_failed"
               for c in log.error.call_args_list)


async def test_invoke_allocator_spawn_raises_is_isolated():
    with patch.object(ed.asyncio, "create_subprocess_exec",
                      AsyncMock(side_effect=OSError("no fork"))), \
         patch.object(ed, "logger") as log:
        await _real_invoke_allocator("allocator")  # must NOT raise
    assert any(c.args and c.args[0] == "engine_dispatch.allocator_failed"
               for c in log.error.call_args_list)


# ---------------------------------------------------------------------------
# TASK C-T3 — allocator as first gated step in dispatch_once
# ---------------------------------------------------------------------------

def _fire(reason="fire"):
    return FireDecision(fire=True, reason=reason,
                        checks={"profiled": True, "cadence": True,
                                "market_closed": True, "data_ready": True,
                                "not_already_run": True})


def _skip(reason="off cadence"):
    return FireDecision(fire=False, reason=reason,
                        checks={"profiled": True, "cadence": False})


def _blocked():
    return FireDecision(fire=False, reason="data not ready",
                        checks={"profiled": True, "cadence": True,
                                "market_closed": True, "data_ready": False})


async def test_allocator_fires_before_any_roster_engine():
    order: list[str] = []

    async def _sf(engine, now, pool):
        return _fire()

    async def _alloc(engine="allocator"):
        order.append("allocator")

    async def _eng(engine):
        order.append(engine)

    with patch.object(ed, "should_fire", _sf), \
         patch.object(ed, "_invoke_allocator", _alloc), \
         patch.object(ed, "_safe_invoke", _eng):
        await dispatch_once(object(), datetime(2026, 5, 18, 13, 0, tzinfo=UTC))

    assert order[0] == "allocator"
    assert order[1:] == list(ROSTER)


async def test_allocator_failure_does_not_abort_roster():
    async def _sf(engine, now, pool):
        return _fire()

    ran: list[str] = []

    async def _alloc(engine="allocator"):
        ed.logger.error("engine_dispatch.allocator_failed", returncode=2)

    async def _eng(engine):
        ran.append(engine)

    with patch.object(ed, "logger") as mock_logger, \
         patch.object(ed, "should_fire", _sf), \
         patch.object(ed, "_invoke_allocator", _alloc), \
         patch.object(ed, "_safe_invoke", _eng):
        await dispatch_once(object(), datetime(2026, 5, 18, 13, 0, tzinfo=UTC))

    error_events = [c.args[0] for c in mock_logger.error.call_args_list]
    assert "engine_dispatch.allocator_failed" in error_events
    assert ran == list(ROSTER)  # engines still ran (Q3)


async def test_allocator_data_blocked_emits_request_for_allocator():
    async def _sf(engine, now, pool):
        return _blocked() if engine == "allocator" else _skip()

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    with patch.object(ed, "should_fire", _sf), \
         patch.object(ed, "failing_sources_for_engine",
                      AsyncMock(return_value=["prices_daily"])), \
         patch.object(ed, "_emit_data_request",
                      AsyncMock(return_value="rid-1")) as emit, \
         patch.object(ed, "_invoke_allocator", AsyncMock()) as alloc, \
         patch.object(ed, "_safe_invoke", AsyncMock()):
        await dispatch_once(pool, datetime(2026, 5, 18, 13, 0, tzinfo=UTC))

    emit.assert_awaited_once()
    assert emit.call_args[0][1] == "allocator"
    assert emit.call_args[0][2] == ["prices_daily"]
    alloc.assert_not_awaited()


async def test_allocator_off_cadence_skips_no_invoke():
    async def _sf(engine, now, pool):
        return _skip()

    with patch.object(ed, "should_fire", _sf), \
         patch.object(ed, "_invoke_allocator", AsyncMock()) as alloc, \
         patch.object(ed, "_safe_invoke", AsyncMock()):
        await dispatch_once(object(), datetime(2026, 5, 19, 13, 0, tzinfo=UTC))

    alloc.assert_not_awaited()


# ---------------------------------------------------------------------------
# TASK C-T6 — retire allocator launchd installer
# ---------------------------------------------------------------------------

def test_install_all_daemons_no_longer_references_allocator_launchd():
    repo = REPO_ROOT
    sh = (repo / "scripts" / "install_all_daemons.sh").read_text()
    assert "install_launchd_allocator" not in sh
    assert not (repo / "scripts" / "install_launchd_allocator.sh").exists()


# ---------------------------------------------------------------------------
# TASK DA-1 — wire engine_supervisor per actor
# ---------------------------------------------------------------------------

async def test_supervise_called_per_actor_before_dispatch():
    order: list[str] = []

    async def _sup(pool, engine, now, invoke):
        order.append(f"supervise:{engine}")

    async def _de(pool, now, engine, invoke):
        order.append(f"dispatch:{engine}")

    with patch.object(ed.engine_supervisor, "supervise", _sup), \
         patch.object(ed, "_dispatch_engine", _de), \
         patch.object(ed, "_invoke_allocator", AsyncMock()):
        await dispatch_once(object(), datetime(2026, 5, 18, 13, 0, tzinfo=UTC))

    for i, item in enumerate(order):
        if item.startswith("dispatch:"):
            assert order[i - 1] == item.replace("dispatch:", "supervise:")
    assert order[0] == "supervise:allocator"


async def test_supervise_failure_does_not_abort_sweep():
    ran: list[str] = []

    async def _de(pool, now, engine, invoke):
        ran.append(engine)

    with patch.object(ed.engine_supervisor, "supervise",
                      AsyncMock(side_effect=RuntimeError("supervisor boom"))), \
         patch.object(ed, "_dispatch_engine", _de), \
         patch.object(ed, "_invoke_allocator", AsyncMock()):
        await dispatch_once(object(), datetime(2026, 5, 18, 13, 0, tzinfo=UTC))

    assert ran == ["allocator", *ROSTER]  # every actor dispatched despite supervisor raising


# ---------------------------------------------------------------------------
# TASK DA-2 — wire aar_autotune per actor (between supervise and dispatch)
# ---------------------------------------------------------------------------

async def test_autotune_called_per_actor_between_supervise_and_dispatch():
    order: list[str] = []

    async def _sup(pool, engine, now, invoke):
        order.append(f"supervise:{engine}")

    async def _at(pool, engine, now):
        order.append(f"autotune:{engine}")

    async def _de(pool, now, engine, invoke):
        order.append(f"dispatch:{engine}")

    with patch.object(ed.engine_supervisor, "supervise", _sup), \
         patch.object(ed.aar_autotune, "autotune", _at), \
         patch.object(ed, "_dispatch_engine", _de), \
         patch.object(ed, "_invoke_allocator", AsyncMock()):
        await dispatch_once(object(), datetime(2026, 5, 18, 13, 0, tzinfo=UTC))

    assert order[0:3] == ["supervise:allocator", "autotune:allocator",
                          "dispatch:allocator"]
    for i, item in enumerate(order):
        if item.startswith("autotune:"):
            eng = item.split(":")[1]
            assert order[i - 1] == f"supervise:{eng}"
            assert order[i + 1] == f"dispatch:{eng}"


async def test_autotune_failure_does_not_abort_sweep():
    ran: list[str] = []

    async def _de(pool, now, engine, invoke):
        ran.append(engine)

    with patch.object(ed.engine_supervisor, "supervise", AsyncMock()), \
         patch.object(ed.aar_autotune, "autotune",
                      AsyncMock(side_effect=RuntimeError("autotune boom"))), \
         patch.object(ed, "_dispatch_engine", _de), \
         patch.object(ed, "_invoke_allocator", AsyncMock()):
        await dispatch_once(object(), datetime(2026, 5, 18, 13, 0, tzinfo=UTC))

    assert ran == ["allocator", *ROSTER]
