import contextlib
import json
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

from ops.engine_dispatch import ROSTER, dispatch_once
from tpcore.engine_profile import FireDecision


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
    with patch("ops.engine_dispatch.should_fire", sf), \
         patch("ops.engine_dispatch._invoke_scheduler", new=AsyncMock(side_effect=lambda e: invoked.append(e))):
        await dispatch_once(_Pool(), now=datetime(2026, 5, 5, 21, 30, tzinfo=UTC))
    assert invoked == ["reversion"]


async def test_roster_is_the_four_live_engines():
    assert ROSTER == ("reversion", "vector", "momentum", "sentinel")


async def test_data_blocked_emits_one_request_and_skips_never_heals():
    nofire = FireDecision(False, "data not ready: stale", {"data_ready": False})
    inserts = []
    class _C:
        async def fetchval(self, *_a, **_k): return None  # no open request
        async def fetch(self, *_a, **_k): return []
        async def execute(self, sql, *args): inserts.append((sql, args))
    class _P:
        @contextlib.asynccontextmanager
        async def acquire(self): yield _C()
    with patch("ops.engine_dispatch.should_fire", AsyncMock(return_value=nofire)), \
         patch("ops.engine_dispatch.failing_sources_for_engine",
               new=AsyncMock(return_value=["prices_daily"])), \
         patch("ops.engine_dispatch._invoke_scheduler", new=AsyncMock()) as inv:
        await dispatch_once(_P(), now=datetime(2026,5,5,21,30,tzinfo=UTC))
    inv.assert_not_called()
    payloads = [a for s, a in inserts if "INSERT INTO platform.application_log" in s]
    assert len(payloads) == 4  # one ENGINE_DATA_REQUEST per ROSTER engine (all data-blocked here)
    data = json.loads(payloads[0][-1])
    assert data["schema"] == 1 and data["engine"] in ROSTER
    assert data["sources"] == ["prices_daily"]
    uuid.UUID(data["request_id"])  # valid uuid


async def test_open_request_is_not_re_emitted():
    nofire = FireDecision(False, "data not ready", {"data_ready": False})
    class _C:
        async def fetchval(self, *_a, **_k): return 1   # an OPEN request exists
        async def fetch(self, *_a, **_k): return []
        async def execute(self, *_a, **_k): raise AssertionError("must not insert when request open")
    class _P:
        @contextlib.asynccontextmanager
        async def acquire(self): yield _C()
    with patch("ops.engine_dispatch.should_fire", AsyncMock(return_value=nofire)), \
         patch("ops.engine_dispatch.failing_sources_for_engine", new=AsyncMock(return_value=["prices_daily"])), \
         patch("ops.engine_dispatch._invoke_scheduler", new=AsyncMock()):
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
    with patch("ops.engine_dispatch.should_fire", AsyncMock(return_value=already)), \
         patch("ops.engine_dispatch._invoke_scheduler", new=AsyncMock()) as inv:
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
    with patch("ops.engine_dispatch.should_fire", AsyncMock(return_value=already)), \
         patch("ops.engine_dispatch._invoke_scheduler", new=AsyncMock()) as inv:
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
    with patch("ops.engine_dispatch.should_fire", AsyncMock(return_value=already)), \
         patch("ops.engine_dispatch._invoke_scheduler", new=AsyncMock()) as inv:
        await dispatch_once(_P(), now=datetime(2026,5,5,21,30,tzinfo=UTC))
    inv.assert_not_called()
