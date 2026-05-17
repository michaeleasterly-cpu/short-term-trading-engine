import contextlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

# ops/ vs scripts/ops.py name-collision guard (identical to
# scripts/tests/test_engine_supervisor.py).
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
for _m in [m for m in list(sys.modules) if m == "ops" or m.startswith("ops.")]:
    if not hasattr(sys.modules[_m], "__path__"):
        del sys.modules[_m]

from ops import aar_autotune as at  # noqa: E402


def _rows_conn(rows_by_call):
    """conn.fetch returns the queued list; conn.execute records inserts."""
    class _C:
        def __init__(self):
            self.inserts = []
            self._q = list(rows_by_call)

        async def fetch(self, *_a, **_k):
            return self._q.pop(0) if self._q else []

        async def fetchrow(self, *_a, **_k):
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


async def test_emit_held_writes_locked_behavioral_payload():
    rec = _rows_conn([])
    await at._emit_held(_pool_for(rec), "reversion", "h-1",
                        "drawdown_period: fp-9", ["fp-9"])
    sql, args = rec.inserts[-1]
    assert "INSERT INTO platform.application_log" in sql
    assert args[2] == "ENGINE_HELD"
    payload = json.loads(args[-1])
    assert payload == {"schema": 1, "hold_id": "h-1", "engine": "reversion",
                       "failure_class": "behavioral",
                       "reason": "drawdown_period: fp-9",
                       "triggers": ["fp-9"]}


async def test_autotune_is_crash_isolated():
    with patch.object(at, "_decide_and_act",
                      new=AsyncMock(side_effect=RuntimeError("boom"))):
        await at.autotune(_pool_for(_rows_conn([])), "reversion",
                          datetime(2026, 5, 5, 21, 30, tzinfo=UTC))  # no raise


async def test_loss_cluster_hold_len_default_is_5():
    assert at.LOSS_CLUSTER_HOLD_LEN == 5
