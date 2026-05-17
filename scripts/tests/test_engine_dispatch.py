import contextlib
import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

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
    assert len(payloads) == 4  # one ENGINE_DATA_REQUEST per ROSTER engine (all data-blocked here)
    data = json.loads(payloads[0][-1])
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
    already = FireDecision(False, "already ran this cycle",
                           {"data_ready": True, "not_already_run": False})
    class _C:
        async def fetchrow(self, *_a, **_k):
            # STARTUP 3h before `now`, no completion
            return {"started_at": datetime(2026,5,5,18,0,tzinfo=UTC), "completed": False}
        async def fetchval(self,*_a,**_k): return None
        async def fetch(self,*_a,**_k): return []
        async def execute(self,*_a,**_k): return None
    class _P:
        @contextlib.asynccontextmanager
        async def acquire(self): yield _C()
    with patch.object(ed, "should_fire", AsyncMock(return_value=already)), \
         patch.object(ed, "_invoke_scheduler", new=AsyncMock()) as inv:
        await dispatch_once(_P(), now=datetime(2026,5,5,21,30,tzinfo=UTC))
    assert inv.await_count >= 1
    assert all(c.args[0] in ROSTER for c in inv.await_args_list)


async def test_recent_startup_without_completion_is_not_refired():
    already = FireDecision(False, "already ran this cycle",
                           {"data_ready": True, "not_already_run": False})
    class _C:
        async def fetchrow(self,*_a,**_k):
            return {"started_at": datetime(2026,5,5,21,20,tzinfo=UTC), "completed": False}  # 10m ago
        async def fetchval(self,*_a,**_k): return None
        async def fetch(self,*_a,**_k): return []
        async def execute(self,*_a,**_k): return None
    class _P:
        @contextlib.asynccontextmanager
        async def acquire(self): yield _C()
    with patch.object(ed, "should_fire", AsyncMock(return_value=already)), \
         patch.object(ed, "_invoke_scheduler", new=AsyncMock()) as inv:
        await dispatch_once(_P(), now=datetime(2026,5,5,21,30,tzinfo=UTC))
    inv.assert_not_called()


async def test_completed_run_is_not_refired():
    already = FireDecision(False, "already ran this cycle",
                           {"data_ready": True, "not_already_run": False})
    class _C:
        async def fetchrow(self,*_a,**_k):
            return {"started_at": datetime(2026,5,5,18,0,tzinfo=UTC), "completed": True}
        async def fetchval(self,*_a,**_k): return None
        async def fetch(self,*_a,**_k): return []
        async def execute(self,*_a,**_k): return None
    class _P:
        @contextlib.asynccontextmanager
        async def acquire(self): yield _C()
    with patch.object(ed, "should_fire", AsyncMock(return_value=already)), \
         patch.object(ed, "_invoke_scheduler", new=AsyncMock()) as inv:
        await dispatch_once(_P(), now=datetime(2026,5,5,21,30,tzinfo=UTC))
    inv.assert_not_called()


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
    assert len(payloads) == len(ROSTER)  # one request per engine
    data = json.loads(payloads[0][-1])
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
