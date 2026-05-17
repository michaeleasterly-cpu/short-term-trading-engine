import contextlib
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
