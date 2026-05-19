import contextlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

# ops/ vs scripts/ops.py name-collision guard (identical to
# scripts/tests/test_engine_supervisor.py).
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
for _m in [m for m in list(sys.modules) if m == "ops" or m.startswith("ops.")]:
    if not hasattr(sys.modules[_m], "__path__"):
        del sys.modules[_m]

from ops import aar_autotune as at  # noqa: E402

# pytest-xdist: pin this ops-shadow module to one worker so its
# sys.modules['ops'] / scripts/ops.py loading stays single-process
# (the ops/ package-shadow is a single-process invariant). P1.3.
pytestmark = pytest.mark.xdist_group("ops_shadow")


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
    await at._emit_held(_pool_for(rec), "reversion", "h-1",  # noqa: SLF001
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


def _trig(kind, fp, **payload):
    return {"id": 1, "trigger_kind": kind,
            "payload": {"engine": "reversion", "fingerprint": fp, **payload}}


async def _run(open_triggers, hold=None):
    rec = _rows_conn([open_triggers])
    with patch.object(at, "current_hold",
                      new=AsyncMock(return_value=hold)):
        await at.autotune(_pool_for(rec), "reversion",
                          datetime(2026, 5, 5, 21, 30, tzinfo=UTC))
    return [a[2] for _s, a in rec.inserts]


async def test_outlier_loss_escalate_only_no_hold():
    events = await _run([_trig("outlier_loss", "fp-o")])
    assert "ENGINE_ESCALATED" in events
    assert "ENGINE_HELD" not in events


async def test_loss_cluster_short_escalate_only():
    for n in (3, 4):
        events = await _run([_trig("loss_cluster", f"fp-c{n}",
                                   streak_length=n)])
        assert events.count("ENGINE_ESCALATED") == 1
        assert "ENGINE_HELD" not in events


async def test_loss_cluster_long_holds_and_escalates():
    events = await _run([_trig("loss_cluster", "fp-c5", streak_length=5)])
    assert "ENGINE_HELD" in events
    assert "ENGINE_ESCALATED" in events


async def test_drawdown_holds_and_escalates():
    events = await _run([_trig("drawdown_period", "fp-d",
                               drawdown_pct="0.1234", days_in_drawdown=20)])
    assert "ENGINE_HELD" in events
    assert "ENGINE_ESCALATED" in events


async def test_no_open_triggers_no_events():
    assert await _run([]) == []


async def test_one_hold_rule_skips_when_already_held():
    from tpcore.supervisor_state import HoldState
    infra = HoldState("h-i", "crashed_startup", "x",
                      datetime(2026, 5, 5, tzinfo=UTC))
    events = await _run([_trig("drawdown_period", "fp-d")], hold=infra)
    assert events == []


async def test_hold_payload_carries_kind_and_fingerprints():
    rec = _rows_conn([[_trig("drawdown_period", "fp-d")]])
    with patch.object(at, "current_hold", new=AsyncMock(return_value=None)):
        await at.autotune(_pool_for(rec), "reversion",
                          datetime(2026, 5, 5, 21, 30, tzinfo=UTC))
    held = [a for _s, a in rec.inserts if a[2] == "ENGINE_HELD"][0]
    p = json.loads(held[-1])
    assert p["failure_class"] == "behavioral"
    assert "drawdown_period" in p["reason"]
    assert p["triggers"] == ["fp-d"]


def _beh_hold(hold_id="h-b"):
    from tpcore.supervisor_state import HoldState
    return HoldState(hold_id, "behavioral", "drawdown_period: fp-d",
                     datetime(2026, 5, 5, 21, 0, tzinfo=UTC))


async def test_behavioral_held_not_cleared_while_holdeligible_open():
    events = await _run([_trig("drawdown_period", "fp-d")],
                        hold=_beh_hold())
    assert "ENGINE_CLEARED" not in events
    assert "ENGINE_HELD" not in events  # one-hold rule: no re-hold either


async def test_behavioral_held_cleared_when_no_holdeligible_open():
    rec = _rows_conn([[]])
    with patch.object(at, "current_hold",
                      new=AsyncMock(return_value=_beh_hold("h-z"))):
        await at.autotune(_pool_for(rec), "reversion",
                          datetime(2026, 5, 5, 21, 30, tzinfo=UTC))
    cleared = [a for _s, a in rec.inserts if a[2] == "ENGINE_CLEARED"]
    assert len(cleared) == 1
    p = json.loads(cleared[0][-1])
    assert p["hold_id"] == "h-z" and p["schema"] == 1


async def test_behavioral_held_cleared_when_only_escalate_triggers_open():
    # original drawdown resolved; an outlier_loss is open but NOT
    # hold-eligible → re-eval has no hold-eligible → clear.
    rec = _rows_conn([[_trig("outlier_loss", "fp-o")]])
    with patch.object(at, "current_hold",
                      new=AsyncMock(return_value=_beh_hold("h-q"))):
        await at.autotune(_pool_for(rec), "reversion",
                          datetime(2026, 5, 5, 21, 30, tzinfo=UTC))
    assert any(a[2] == "ENGINE_CLEARED" for _s, a in rec.inserts)


async def test_behavioral_held_kept_when_newer_holdeligible_fires():
    # original trigger resolved but a NEW drawdown is open → still
    # hold-eligible → NOT cleared (re-eval, not fingerprint match).
    events = await _run([_trig("drawdown_period", "fp-NEW")],
                        hold=_beh_hold("h-k"))
    assert "ENGINE_CLEARED" not in events


async def test_infra_hold_not_cleared_by_da2():
    # DA-2 must only clear BEHAVIORAL holds; an infra hold is DA-1's.
    from tpcore.supervisor_state import HoldState
    infra = HoldState("h-i", "crashed_startup", "x",
                      datetime(2026, 5, 5, tzinfo=UTC))
    events = await _run([], hold=infra)
    assert "ENGINE_CLEARED" not in events
