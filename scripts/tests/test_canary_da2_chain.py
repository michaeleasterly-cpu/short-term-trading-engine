"""DA-2 end-to-end chain proof: canary injection → aar_autotune → should_fire.

Proves the full DA-2 branch table against engine='canary':
  * HOLD kinds (loss_cluster streak≥5, drawdown_period) → ENGINE_HELD+ESCALATED
    → should_fire "supervisor hold" → operator resolves → ENGINE_CLEARED → unblocked
  * ESCALATE-only kinds (outlier_loss, loss_cluster streak<5) → ESCALATED only,
    never ENGINE_HELD → should_fire never "supervisor hold"
  * Teardown deletes only canary_injection rows, leaves other rows untouched

Drives the REAL _stage_canary_inject_trigger, aar_autotune.autotune, and
should_fire. Patches ONLY: current_hold in both consumer modules (cleaner than
emulating the complex LEFT JOIN SQL; both modules import it), plus should_fire's
data-gate / cadence / already-ran neutralisers (the test_engine_profile idiom).
"""
from __future__ import annotations

import contextlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

# ops/ vs scripts/ops.py name-collision guard (identical to
# scripts/tests/test_aar_autotune.py).
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
for _m in [m for m in list(sys.modules) if m == "ops" or m.startswith("ops.")]:
    if not hasattr(sys.modules[_m], "__path__"):
        del sys.modules[_m]

from ops import aar_autotune as at  # noqa: E402
from scripts.ops import _stage_canary_inject_trigger  # noqa: E402
from tpcore.engine_profile import should_fire  # noqa: E402
from tpcore.supervisor_state import HoldState, current_hold  # noqa: E402,F401

NOW = datetime(2026, 5, 17, 21, 30, tzinfo=UTC)
_CANARY = "canary"
_INJ_SOURCE = "canary_injection"

# ---------------------------------------------------------------------------
# In-memory store backing forensics_triggers + application_log
# ---------------------------------------------------------------------------


# pytest-xdist: pin this ops-shadow module to one worker so its
# sys.modules['ops'] / scripts/ops.py loading stays single-process
# (the ops/ package-shadow is a single-process invariant). P1.3.
pytestmark = pytest.mark.xdist_group("ops_shadow")


class _Store:
    """Shared in-memory backing store for all fake DB ops."""

    def __init__(self) -> None:
        self._triggers: list[dict] = []
        self._app_log: list[dict] = []
        self._id_seq = 0

    # --- forensics_triggers helpers -----------------------------------------

    def insert_trigger(self, trigger_kind: str, payload: dict, fired_at: datetime) -> dict:
        self._id_seq += 1
        row = {
            "id": self._id_seq,
            "trigger_kind": trigger_kind,
            "payload": payload,
            "resolved_at": None,
            "fired_at": fired_at,
        }
        self._triggers.append(row)
        return row

    def dedup_trigger(self, trigger_kind: str, fingerprint: str):
        """Return a truthy row if a trigger with that kind+fingerprint exists."""
        for r in self._triggers:
            if r["trigger_kind"] == trigger_kind and r["payload"].get("fingerprint") == fingerprint:
                return {"1": 1}
        return None

    def open_triggers_for(self, engine: str) -> list[dict]:
        """Unresolved triggers for engine, newest first."""
        rows = [
            r for r in self._triggers
            if r["resolved_at"] is None and r["payload"].get("engine") == engine
        ]
        rows.sort(key=lambda r: r["fired_at"], reverse=True)
        return [{"id": r["id"], "trigger_kind": r["trigger_kind"], "payload": r["payload"]}
                for r in rows]

    def delete_by_source(self, source: str) -> None:
        self._triggers = [r for r in self._triggers if r["payload"].get("source") != source]

    def resolve_canary_triggers(self) -> None:
        """Simulate the operator setting resolved_at on all open canary triggers."""
        for r in self._triggers:
            if r["payload"].get("engine") == _CANARY and r["resolved_at"] is None:
                r["resolved_at"] = NOW

    def seed_non_canary_trigger(self) -> None:
        """Plant a forensics row for another engine (must survive teardown)."""
        self._id_seq += 1
        self._triggers.append({
            "id": self._id_seq,
            "trigger_kind": "outlier_loss",
            "payload": {"engine": "reversion", "source": "forensics_scanner", "fingerprint": "rev-fp"},
            "resolved_at": None,
            "fired_at": NOW,
        })

    # --- application_log helpers --------------------------------------------

    def insert_app_log(self, engine: str, run_id, event_type: str,
                       severity: str, message: str, data_json: str) -> None:
        self._app_log.append({
            "engine": engine,
            "run_id": run_id,
            "event_type": event_type,
            "severity": severity,
            "message": message,
            "data": json.loads(data_json),
            "recorded_at": NOW,
        })

    def current_hold_for(self, engine: str) -> HoldState | None:
        """Compute hold state from application_log (avoids emulating the LEFT JOIN).

        Latest ENGINE_HELD for engine whose hold_id has no later ENGINE_CLEARED.
        """
        from tpcore.supervisor_state import CLEARED_EVENT, HELD_EVENT
        held_rows = [r for r in self._app_log
                     if r["event_type"] == HELD_EVENT and r["engine"] == engine]
        if not held_rows:
            return None
        latest = held_rows[-1]
        hold_id = latest["data"].get("hold_id")
        cleared = any(
            r["event_type"] == CLEARED_EVENT
            and r["data"].get("hold_id") == hold_id
            for r in self._app_log
        )
        if cleared:
            return None
        return HoldState(
            hold_id=hold_id,
            failure_class=latest["data"].get("failure_class", ""),
            reason=latest["data"].get("reason", ""),
            held_at=latest["recorded_at"],
        )

    def app_log_events_for(self, engine: str) -> list[str]:
        return [r["event_type"] for r in self._app_log if r["engine"] == engine]

    def reset(self) -> None:
        self._triggers.clear()
        self._app_log.clear()
        self._id_seq = 0


# ---------------------------------------------------------------------------
# Fake pool / conn  — routes real SQL fragments to the _Store
# ---------------------------------------------------------------------------

class _Conn:
    """Plan 2: forensics triggers live in platform.data_quality_log
    (kind='forensics_trigger'); the producer routes through
    tpcore.forensics.dql_store. The fragments below mirror that store's SQL
    (EXISTS + INSERT via fetchval; open-for-engine via fetch with
    notes->>'resolved_at' IS NULL; teardown DELETE on notes->>'source')."""

    def __init__(self, store: _Store) -> None:
        self._s = store

    async def fetchrow(self, sql: str, *args) -> dict | None:
        # No longer used for forensics dedup (now fetchval); kept defensive.
        return None

    async def fetch(self, sql: str, *args) -> list[dict]:
        sql_norm = " ".join(sql.split())
        # dql_store.OPEN_FOR_ENGINE_SQL: notes->>'resolved_at' IS NULL
        #                                AND notes->>'engine' = $1
        if "data_quality_log" in sql_norm and "notes->>'resolved_at' IS NULL" in sql_norm:
            engine = args[0]
            rows = self._s.open_triggers_for(engine)
            # Return FakeRecord objects so r["field"] works
            return [_FakeRecord(r) for r in rows]
        return []

    async def fetchval(self, sql: str, *args) -> object:
        sql_norm = " ".join(sql.split())
        # dql_store EXISTS check: kind=$1 AND notes->>'trigger_kind'=$2
        #                         AND notes->>'fingerprint'=$3
        if "data_quality_log" in sql_norm and "notes->>'fingerprint'" in sql_norm:
            _kind_const, trigger_kind, fp = args[0], args[1], args[2]
            row = self._s.dedup_trigger(trigger_kind, fp)
            return 1 if row else None
        # dql_store INSERT: (kind_const, source, fired_at, notes_json) RETURNING id
        if "INSERT INTO platform.data_quality_log" in sql_norm:
            notes_json, fired_at = args[3], args[2]
            notes = json.loads(notes_json) if isinstance(notes_json, str) else notes_json
            trigger_kind = notes.get("trigger_kind")
            self._s.insert_trigger(trigger_kind, notes, fired_at)
            return "ca-uuid-0001"
        # _already_ran: patched neutral, but route defensively
        return None

    async def execute(self, sql: str, *args) -> str:
        sql_norm = " ".join(sql.split())
        # teardown DELETE: kind=$1 AND notes->>'source'=$2
        if "DELETE FROM platform.data_quality_log" in sql_norm:
            source = args[1]
            self._s.delete_by_source(source)
            return "DELETE N"
        # INSERT application_log
        if "INSERT INTO platform.application_log" in sql_norm:
            engine, run_id, event_type, severity, message, data_json = (
                args[0], args[1], args[2], args[3], args[4], args[5])
            self._s.insert_app_log(engine, run_id, event_type, severity, message, data_json)
            return "INSERT 0 1"
        return "OK"


class _FakeRecord:
    """Minimal asyncpg-like record that supports both dict-style and attr access."""

    def __init__(self, data: dict) -> None:
        self._d = data

    def __getitem__(self, key: str):
        return self._d[key]

    def get(self, key: str, default=None):
        return self._d.get(key, default)


class _Pool:
    def __init__(self, store: _Store) -> None:
        self._store = store

    @contextlib.asynccontextmanager
    async def acquire(self):
        yield _Conn(self._store)


# ---------------------------------------------------------------------------
# Patching helpers
# ---------------------------------------------------------------------------

def _make_current_hold_fn(store: _Store):
    """Return an async function that reads hold state from the store.

    Patched into BOTH at.current_hold and tpcore.engine_profile.current_hold
    so autotune's hold check AND should_fire's gate agree.
    """
    async def _current_hold(pool, engine: str):  # noqa: ARG001
        return store.current_hold_for(engine)
    return _current_hold


def _patch_should_fire_gates(store: _Store) -> contextlib.AbstractContextManager:
    """Neutralise everything in should_fire EXCEPT the behavioral-hold check.

    Reuses the exact same patch targets as test_engine_profile._patch_all:
      - tpcore.engine_profile._cadence_boundary  → True
      - tpcore.engine_profile.cal.session_contains → False (market closed)
      - tpcore.engine_profile.assert_passed_for_engine → no-op
      - tpcore.engine_profile._already_ran → False
    current_hold is patched separately (and shared with autotune).
    """
    cm = contextlib.ExitStack()
    cm.enter_context(patch("tpcore.engine_profile._cadence_boundary", return_value=True))
    cm.enter_context(patch("tpcore.engine_profile.cal.session_contains", return_value=False))
    cm.enter_context(patch("tpcore.engine_profile.assert_passed_for_engine",
                           new=AsyncMock(return_value=None)))
    cm.enter_context(patch("tpcore.engine_profile._already_ran",
                           new=AsyncMock(return_value=False)))
    # current_hold in engine_profile must see the store's live state
    cm.enter_context(patch("tpcore.engine_profile.current_hold",
                           new=_make_current_hold_fn(store)))
    return cm


# ---------------------------------------------------------------------------
# HOLD kind tests — loss_cluster streak≥5
# ---------------------------------------------------------------------------

async def test_loss_cluster_hold_chain():
    """loss_cluster streak=5 → HELD+ESCALATED → should_fire blocked →
    operator resolves → CLEARED → unblocked."""
    store = _Store()
    pool = _Pool(store)

    # Step 1: inject
    await _stage_canary_inject_trigger(pool, {"kind": "loss_cluster", "streak": 5})
    assert len(store._triggers) == 1  # noqa: SLF001

    # Step 2: autotune — must hold & escalate
    with patch.object(at, "current_hold", new=_make_current_hold_fn(store)):
        await at.autotune(pool, _CANARY, NOW)

    events = store.app_log_events_for(_CANARY)
    assert "ENGINE_HELD" in events, f"expected ENGINE_HELD, got {events}"
    assert "ENGINE_ESCALATED" in events, f"expected ENGINE_ESCALATED, got {events}"

    held_rows = [r for r in store._app_log  # noqa: SLF001
                 if r["event_type"] == "ENGINE_HELD" and r["engine"] == _CANARY]
    assert len(held_rows) == 1
    assert held_rows[0]["data"]["failure_class"] == "behavioral"

    # Step 3: should_fire is blocked
    with _patch_should_fire_gates(store):
        d = await should_fire(_CANARY, NOW, pool)
    assert d.fire is False
    assert d.reason == "supervisor hold"

    # Step 4: operator resolves
    store.resolve_canary_triggers()

    # Step 5: autotune again — must clear
    with patch.object(at, "current_hold", new=_make_current_hold_fn(store)):
        await at.autotune(pool, _CANARY, NOW)

    events2 = store.app_log_events_for(_CANARY)
    assert "ENGINE_CLEARED" in events2, f"expected ENGINE_CLEARED, got {events2}"

    # Step 6: should_fire is now unblocked
    with _patch_should_fire_gates(store):
        d2 = await should_fire(_CANARY, NOW, pool)
    assert d2.reason != "supervisor hold", f"still blocked after clear: {d2}"


# ---------------------------------------------------------------------------
# HOLD kind tests — drawdown_period
# ---------------------------------------------------------------------------

async def test_drawdown_period_hold_chain():
    """drawdown_period → HELD+ESCALATED → should_fire blocked →
    operator resolves → CLEARED → unblocked."""
    store = _Store()
    pool = _Pool(store)

    await _stage_canary_inject_trigger(pool, {"kind": "drawdown_period"})
    assert len(store._triggers) == 1  # noqa: SLF001

    with patch.object(at, "current_hold", new=_make_current_hold_fn(store)):
        await at.autotune(pool, _CANARY, NOW)

    events = store.app_log_events_for(_CANARY)
    assert "ENGINE_HELD" in events
    assert "ENGINE_ESCALATED" in events

    held_rows = [r for r in store._app_log  # noqa: SLF001
                 if r["event_type"] == "ENGINE_HELD" and r["engine"] == _CANARY]
    assert held_rows[0]["data"]["failure_class"] == "behavioral"

    with _patch_should_fire_gates(store):
        d = await should_fire(_CANARY, NOW, pool)
    assert d.fire is False
    assert d.reason == "supervisor hold"

    store.resolve_canary_triggers()

    with patch.object(at, "current_hold", new=_make_current_hold_fn(store)):
        await at.autotune(pool, _CANARY, NOW)

    assert "ENGINE_CLEARED" in store.app_log_events_for(_CANARY)

    with _patch_should_fire_gates(store):
        d2 = await should_fire(_CANARY, NOW, pool)
    assert d2.reason != "supervisor hold"


# ---------------------------------------------------------------------------
# ESCALATE-only tests — outlier_loss
# ---------------------------------------------------------------------------

async def test_outlier_loss_escalate_only_never_held():
    """outlier_loss → ESCALATED only; should_fire is NOT 'supervisor hold'."""
    store = _Store()
    pool = _Pool(store)

    await _stage_canary_inject_trigger(pool, {"kind": "outlier_loss"})

    with patch.object(at, "current_hold", new=_make_current_hold_fn(store)):
        await at.autotune(pool, _CANARY, NOW)

    events = store.app_log_events_for(_CANARY)
    assert "ENGINE_ESCALATED" in events
    assert "ENGINE_HELD" not in events

    with _patch_should_fire_gates(store):
        d = await should_fire(_CANARY, NOW, pool)
    assert d.reason != "supervisor hold"


# ---------------------------------------------------------------------------
# ESCALATE-only tests — loss_cluster streak<5
# ---------------------------------------------------------------------------

async def test_loss_cluster_short_escalate_only_never_held():
    """loss_cluster streak=3 (< LOSS_CLUSTER_HOLD_LEN=5) → ESCALATED only;
    should_fire is NOT 'supervisor hold'."""
    store = _Store()
    pool = _Pool(store)

    await _stage_canary_inject_trigger(pool, {"kind": "loss_cluster", "streak": 3})

    with patch.object(at, "current_hold", new=_make_current_hold_fn(store)):
        await at.autotune(pool, _CANARY, NOW)

    events = store.app_log_events_for(_CANARY)
    assert "ENGINE_ESCALATED" in events
    assert "ENGINE_HELD" not in events

    with _patch_should_fire_gates(store):
        d = await should_fire(_CANARY, NOW, pool)
    assert d.reason != "supervisor hold"


# ---------------------------------------------------------------------------
# Teardown — leaves non-canary_injection rows untouched
# ---------------------------------------------------------------------------

async def test_teardown_removes_only_injected_rows():
    """Teardown deletes canary_injection forensics rows and nothing else."""
    store = _Store()
    pool = _Pool(store)

    # Seed a non-injection row that must survive
    store.seed_non_canary_trigger()
    non_injection_count_before = len(store._triggers)  # noqa: SLF001
    assert non_injection_count_before == 1

    # Inject some canary rows
    await _stage_canary_inject_trigger(pool, {"kind": "loss_cluster", "streak": 5})
    await _stage_canary_inject_trigger(pool, {"kind": "drawdown_period"})
    assert len(store._triggers) == 3  # noqa: SLF001

    # Teardown
    result = await _stage_canary_inject_trigger(pool, {"teardown": True})
    assert result["teardown"] is True

    # Only the non-injection row remains
    remaining = store._triggers  # noqa: SLF001
    assert len(remaining) == 1
    assert remaining[0]["payload"]["source"] != _INJ_SOURCE
    assert remaining[0]["payload"]["engine"] == "reversion"
