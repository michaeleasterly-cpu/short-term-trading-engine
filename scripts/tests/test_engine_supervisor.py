import contextlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

# ops/ vs scripts/ops.py top-level name collision guard (identical to
# scripts/tests/test_engine_dispatch.py — repo root first, evict any
# non-package `ops`/`ops.*` so the real ops/ package resolves).
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
for _m in [m for m in list(sys.modules) if m == "ops" or m.startswith("ops.")]:
    if not hasattr(sys.modules[_m], "__path__"):
        del sys.modules[_m]

from ops import engine_supervisor as es  # noqa: E402


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
