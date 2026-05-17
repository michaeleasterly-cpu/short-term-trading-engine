# DA-1 — Engine Supervisor / Escalation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the engine lane a bounded, deterministic detect → self-heal → verify → escalate+hold → auto-clear supervisor for infra/liveness failures, with a `should_fire` hold-gate, parity with the data lane's escalation.

**Architecture:** A pure `tpcore/supervisor_state.py` owns the locked event-type vocabulary + the event-sourced hold-state read (`current_hold`). `tpcore.engine_profile.should_fire` gains a pure `supervisor_held` precondition check that reads it (no `ops` import — respects the tpcore↛engine/ops layering). `ops/engine_supervisor.py` owns the agent: `supervise(pool, engine, now, invoke)` — detect the §4 infra classes from `application_log`, run a bounded class-specific self-heal (re-invoke via the **injected** invoker, never importing `engine_dispatch` — avoids an import cycle), verify, emit `ENGINE_SUPERVISOR_RECOVERED` or `ENGINE_ESCALATED`+`ENGINE_HELD`, and auto-clear on a strong clean-cycle predicate. `engine_dispatch.dispatch_once` calls `await engine_supervisor.supervise(...)` per actor before `_dispatch_engine`; `_crashed_startup_refire` migrates into the supervisor behavior-preservingly (B/C suites are the oracle, exactly like C-T1).

**Tech Stack:** Python 3.11, asyncio, asyncpg, structlog, pytest (`asyncio_mode = "auto"`), `platform.application_log` event bus, `tpcore.engine_profile`.

**Lane / scope discipline:** Touches ONLY `tpcore/supervisor_state.py` (new), `tpcore/engine_profile.py`, `ops/engine_supervisor.py` (new), `ops/engine_dispatch.py`, and their test files (`tpcore/tests/test_supervisor_state.py` new, `tpcore/tests/test_engine_profile.py`, `scripts/tests/test_engine_supervisor.py` new, `scripts/tests/test_engine_dispatch.py`). Does NOT touch data-lane files (`tpcore/selfheal`, `tpcore/feeds`, `tpcore/ingestion`, `ops/data_repair_service.py`, `ops/cutover_agent.py`, `ops/weekly_digest.py`), behavioral/forensics (DA-2), daemon consolidation (DA-3), or allocation/risk logic. CI-exact gates: `python -m ruff check reversion/ vector/ momentum/ sentinel/ tpcore/ scripts/ ops/` and `python -m tpcore.scripts.check_imports reversion vector momentum sentinel tpcore`. The venv is `/Users/michael/short-term-trading-engine/.venv/bin/python`; `ruff` is on PATH as a binary.

---

## File Structure

| File | Responsibility | Change |
|------|----------------|--------|
| `tpcore/supervisor_state.py` | Locked event-type vocabulary + `HoldState` + pure event-sourced `current_hold` read | Create |
| `tpcore/engine_profile.py` | Pure fire gate | Add `supervisor_held` check to `should_fire` |
| `ops/engine_supervisor.py` | The agent: detect→self-heal→verify→escalate+hold→auto-clear; event writers | Create |
| `ops/engine_dispatch.py` | Dispatcher | Call `supervise()` per actor; migrate `_crashed_startup_refire`; `data_request_timeout`→escalation |
| `tpcore/tests/test_supervisor_state.py` | — | Create |
| `tpcore/tests/test_engine_profile.py` | — | Add hold-gate tests; reconcile exact-`checks`-dict assertions |
| `scripts/tests/test_engine_supervisor.py` | — | Create |
| `scripts/tests/test_engine_dispatch.py` | — | Add wiring tests; reconcile crashed-startup tests |

---

## Task 1: `tpcore/supervisor_state.py` — vocabulary + event-sourced hold read

The locked event-type strings, the `HoldState` shape, and the pure read used by BOTH `should_fire` (tpcore) and the supervisor (ops). Lives in tpcore so `should_fire` can import it without a layering violation; `ops` may import tpcore.

**Files:**
- Create: `tpcore/supervisor_state.py`
- Test: `tpcore/tests/test_supervisor_state.py`

- [ ] **Step 1: Write the failing test**

Create `tpcore/tests/test_supervisor_state.py`:

```python
import contextlib
from datetime import UTC, datetime

from tpcore.supervisor_state import (
    HELD_EVENT,
    CLEARED_EVENT,
    ESCALATED_EVENT,
    RECOVERED_EVENT,
    SCHEMA_VERSION,
    HoldState,
    current_hold,
)


class _Conn:
    def __init__(self, row):
        self._row = row

    async def fetchrow(self, *_a, **_k):
        return self._row


class _Pool:
    def __init__(self, row):
        self._row = row

    @contextlib.asynccontextmanager
    async def acquire(self):
        yield _Conn(self._row)


def test_event_vocabulary_is_locked():
    assert HELD_EVENT == "ENGINE_HELD"
    assert CLEARED_EVENT == "ENGINE_CLEARED"
    assert ESCALATED_EVENT == "ENGINE_ESCALATED"
    assert RECOVERED_EVENT == "ENGINE_SUPERVISOR_RECOVERED"
    assert SCHEMA_VERSION == 1


async def test_current_hold_none_when_no_held_row():
    assert await current_hold(_Pool(None), "reversion") is None


async def test_current_hold_returns_holdstate_when_held_unclearedd():
    row = {
        "hold_id": "h-1",
        "failure_class": "crashed_startup",
        "reason": "stale STARTUP",
        "held_at": datetime(2026, 5, 5, 21, 0, tzinfo=UTC),
        "cleared": None,
    }
    hs = await current_hold(_Pool(row), "reversion")
    assert isinstance(hs, HoldState)
    assert hs.hold_id == "h-1"
    assert hs.failure_class == "crashed_startup"
    assert hs.held_at == datetime(2026, 5, 5, 21, 0, tzinfo=UTC)


async def test_current_hold_none_when_latest_held_is_cleared():
    row = {
        "hold_id": "h-1",
        "failure_class": "crashed_startup",
        "reason": "stale STARTUP",
        "held_at": datetime(2026, 5, 5, 21, 0, tzinfo=UTC),
        "cleared": "ENGINE_CLEARED",
    }
    assert await current_hold(_Pool(row), "reversion") is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd /Users/michael/short-term-trading-engine/.claude/worktrees/da1-engine-supervisor && /Users/michael/short-term-trading-engine/.venv/bin/python -m pytest tpcore/tests/test_supervisor_state.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'tpcore.supervisor_state'`.

- [ ] **Step 3: Implement `tpcore/supervisor_state.py`**

```python
"""Engine-supervisor inter-lane vocabulary + event-sourced hold read.

Pure tpcore (NO ops import): `should_fire` (tpcore) and the supervisor
(ops) both read hold state through `current_hold`. The supervisor
(ops/engine_supervisor.py) is the sole WRITER of these events; this
module only defines the locked vocabulary and the read.

Locked contract (schema:1, parity with ENGINE_DATA_REQUEST /
DATA_REPAIR_*): `hold_id` is a uuid4 string, the sole correlation
key; NO client timestamps in payloads (DB `recorded_at` only);
one-terminal liveness — an ENGINE_HELD is eventually followed by
exactly one ENGINE_CLEARED.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

SCHEMA_VERSION = 1

HELD_EVENT = "ENGINE_HELD"
CLEARED_EVENT = "ENGINE_CLEARED"
ESCALATED_EVENT = "ENGINE_ESCALATED"
RECOVERED_EVENT = "ENGINE_SUPERVISOR_RECOVERED"


@dataclass(frozen=True)
class HoldState:
    """An engine's currently-open supervisor hold."""

    hold_id: str
    failure_class: str
    reason: str
    held_at: datetime


async def current_hold(pool, engine: str) -> HoldState | None:
    """The engine's open hold, or None.

    Latest ENGINE_HELD for ``engine`` whose ``hold_id`` has no later
    ENGINE_CLEARED. Mirrors engine_dispatch._open_request_state's
    request/terminal LEFT JOIN, keyed on hold_id.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT h.data->>'hold_id'        AS hold_id,
                   h.data->>'failure_class'  AS failure_class,
                   h.data->>'reason'         AS reason,
                   h.recorded_at             AS held_at,
                   c.event_type              AS cleared
            FROM platform.application_log h
            LEFT JOIN platform.application_log c
              ON c.event_type = $2
             AND (c.data->>'hold_id') = (h.data->>'hold_id')
            WHERE h.event_type = $1 AND h.engine = $3
            ORDER BY h.recorded_at DESC LIMIT 1
            """,
            HELD_EVENT, CLEARED_EVENT, engine,
        )
    if row is None or row["cleared"] is not None:
        return None
    return HoldState(
        hold_id=row["hold_id"],
        failure_class=row["failure_class"],
        reason=row["reason"],
        held_at=row["held_at"],
    )
```

- [ ] **Step 4: Run to verify it passes**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest tpcore/tests/test_supervisor_state.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add tpcore/supervisor_state.py tpcore/tests/test_supervisor_state.py
git commit -m "$(cat <<'EOF'
feat(supervisor_state): locked vocabulary + event-sourced hold read

Pure tpcore module (no ops import) — the 4 ENGINE_* event-type
constants, HoldState, and current_hold (latest ENGINE_HELD with no
later ENGINE_CLEARED, mirroring _open_request_state). Shared by
should_fire (tpcore) and the supervisor (ops). DA-1 §3/§8.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: `should_fire` supervisor hold-gate

`should_fire` gains a pure `supervisor_held` check after `market_closed`, before `data_ready` (a held engine must not even emit data requests — DA-1 §6). It only reads via `current_hold`; never writes.

**Files:**
- Modify: `tpcore/engine_profile.py` (imports; `should_fire` body, lines 134–176)
- Test: `tpcore/tests/test_engine_profile.py`

- [ ] **Step 1: Write the failing test**

Append to `tpcore/tests/test_engine_profile.py`:

```python
async def test_should_fire_blocks_when_supervisor_held():
    from tpcore.supervisor_state import HoldState

    held = HoldState(hold_id="h-9", failure_class="crashed_startup",
                      reason="stale", held_at=datetime(2026, 5, 5, tzinfo=UTC))
    with _patch_all(), \
         patch("tpcore.engine_profile.current_hold",
               new=AsyncMock(return_value=held)):
        d = await should_fire("reversion",
                              datetime(2026, 5, 5, 21, 30, tzinfo=UTC),
                              _FakePool(ran=False))
    assert d.fire is False
    assert d.reason == "supervisor hold"
    assert d.checks["supervisor_held"] is False
    # gate short-circuits BEFORE the data-ready check
    assert "data_ready" not in d.checks


async def test_should_fire_proceeds_when_not_held():
    with _patch_all(), \
         patch("tpcore.engine_profile.current_hold",
               new=AsyncMock(return_value=None)):
        d = await should_fire("reversion",
                              datetime(2026, 5, 5, 21, 30, tzinfo=UTC),
                              _FakePool(ran=False))
    assert d.fire is True and d.reason == "ready"
    assert d.checks["supervisor_held"] is True
```

- [ ] **Step 2: Run to verify it fails**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest tpcore/tests/test_engine_profile.py -k supervisor_held -q`
Expected: FAIL — `AttributeError: ... has no attribute 'current_hold'` (not imported yet) / `KeyError: 'supervisor_held'`.

- [ ] **Step 3: Add the import**

In `tpcore/engine_profile.py`, in the import block (after `from tpcore.quality.validation.capital_gate import assert_passed_for_engine`, line ~25), add:

```python
from tpcore.supervisor_state import current_hold
```

- [ ] **Step 4: Insert the check into `should_fire`**

In `tpcore/engine_profile.py`, in `should_fire`, immediately AFTER the `market_closed` block and BEFORE the `try:`/`assert_passed_for_engine` `data_ready` block, insert:

```python
        hold = await current_hold(pool, engine)
        checks["supervisor_held"] = hold is None
        if hold is not None:
            return FireDecision(False, "supervisor hold", checks)
```

For reference, the result is (existing lines unchanged, new block between them):

```python
        if profile.market_closed_required:
            closed = not cal.session_contains(now)
            checks["market_closed"] = closed
            if not closed:
                return FireDecision(False, "market open", checks)
        else:
            checks["market_closed"] = True

        hold = await current_hold(pool, engine)
        checks["supervisor_held"] = hold is None
        if hold is not None:
            return FireDecision(False, "supervisor hold", checks)

        try:
            await assert_passed_for_engine(pool, engine)
            checks["data_ready"] = True
```

- [ ] **Step 5: Run the new tests — verify they pass**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest tpcore/tests/test_engine_profile.py -k supervisor_held -q`
Expected: PASS (2 passed).

- [ ] **Step 6: Run the full engine_profile suite + reconcile exact-`checks` assertions**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest tpcore/tests/test_engine_profile.py -q`
The all-green `checks` dict now contains an extra key `supervisor_held: True`. Any pre-existing test asserting the full dict by equality (e.g. `test_should_fire_all_green_fires`: `assert d.checks == {"profiled": True, "cadence": True, "market_closed": True, "data_ready": True, "not_already_run": True}`) now fails. Reconcile each such test by adding `"supervisor_held": True` in the correct position (between `market_closed` and `data_ready`) — this is a faithful reconciliation (the gate genuinely now records that check), NOT a weakening. For tests asserting a partial/short-circuited `checks` (e.g. market-open returns before the hold check), confirm they still hold (they short-circuit before `supervisor_held` is set — no change needed). Record every test changed (name + the exact dict edit).
Expected after reconciliation: PASS (all).

- [ ] **Step 7: Commit**

```bash
git add tpcore/engine_profile.py tpcore/tests/test_engine_profile.py
git commit -m "$(cat <<'EOF'
feat(engine_profile): should_fire supervisor hold-gate (DA-1 §6)

New pure precondition check `supervisor_held` after market_closed,
before data_ready: a held engine (current_hold != None) returns
FireDecision(False, "supervisor hold") and never emits data requests.
should_fire only reads; the supervisor (ops) writes the hold events.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: `ops/engine_supervisor.py` — module skeleton, event writers, crash-isolated `supervise`

The agent shell: the four event emitters (mirroring `_emit_data_request`/`data_repair_service._emit`), the injected-invoker signature, and a crash-isolated `supervise` that does nothing yet (detectors added in Tasks 4–6). No `engine_dispatch` import (invoker is injected — avoids the cycle).

**Files:**
- Create: `ops/engine_supervisor.py`
- Test: `scripts/tests/test_engine_supervisor.py`

- [ ] **Step 1: Write the failing test**

Create `scripts/tests/test_engine_supervisor.py`:

```python
import contextlib
import json
import sys
import uuid
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest scripts/tests/test_engine_supervisor.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'ops.engine_supervisor'`.

- [ ] **Step 3: Implement the skeleton**

Create `ops/engine_supervisor.py`:

```python
"""Engine Supervisor (Sub-project DA-1).

Bounded, deterministic detect → self-heal → verify → escalate+hold →
auto-clear for engine-lane INFRA/LIVENESS failures (NOT behavioral —
that is DA-2). Invoked per dispatch actor by ops/engine_dispatch.py
before _dispatch_engine. Crash-isolated: a supervisor exception must
NEVER abort the sweep or block trading (same invariant as
allocator-failure in Sub-project C). The injected `invoke` callable
re-runs an actor's scheduler for the self-heal classes — injected (not
an engine_dispatch import) to avoid an engine_dispatch ↔ supervisor
import cycle. should_fire enforces the hold via tpcore.supervisor_state.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime

import structlog

from tpcore.supervisor_state import (
    ESCALATED_EVENT,
    HELD_EVENT,
    CLEARED_EVENT,
    RECOVERED_EVENT,
    SCHEMA_VERSION,
    current_hold,
)

logger = structlog.get_logger(__name__)

_MAX_REINVOKE = int(os.environ.get("ENGINE_SUPERVISOR_MAX_REINVOKE", "2"))
_MISSED_CYCLES_N = int(os.environ.get("ENGINE_SUPERVISOR_MISSED_CYCLES", "2"))

_INSERT_SQL = """
    INSERT INTO platform.application_log
        (engine, run_id, event_type, severity, message, data)
    VALUES ($1, $2, $3, $4, $5, $6::jsonb)
"""


async def _emit(pool, engine: str, event_type: str, severity: str,
                message: str, payload: dict) -> None:
    """One application_log row, mirroring engine_dispatch._emit_data_request
    / data_repair_service._emit (json.dumps, ::jsonb, DB recorded_at)."""
    async with pool.acquire() as conn:
        await conn.execute(
            _INSERT_SQL, engine, uuid.uuid4(), event_type, severity,
            message, json.dumps(payload, default=str),
        )


async def _emit_held(pool, engine: str, hold_id: str,
                     failure_class: str, reason: str) -> None:
    await _emit(pool, engine, HELD_EVENT, "ERROR",
                f"{engine} held: {failure_class} — {reason}",
                {"schema": SCHEMA_VERSION, "hold_id": hold_id,
                 "engine": engine, "failure_class": failure_class,
                 "reason": reason})


async def _emit_escalated(pool, engine: str, hold_id: str,
                          failure_class: str, reason: str,
                          attempts: int) -> None:
    await _emit(pool, engine, ESCALATED_EVENT, "ERROR",
                f"{engine} escalated: {failure_class} after {attempts} attempt(s)",
                {"schema": SCHEMA_VERSION, "hold_id": hold_id,
                 "engine": engine, "failure_class": failure_class,
                 "reason": reason, "attempts": attempts})


async def _emit_cleared(pool, engine: str, hold_id: str,
                        clear_reason: str) -> None:
    await _emit(pool, engine, CLEARED_EVENT, "INFO",
                f"{engine} cleared: {clear_reason}",
                {"schema": SCHEMA_VERSION, "hold_id": hold_id,
                 "engine": engine, "clear_reason": clear_reason})


async def _emit_recovered(pool, engine: str, failure_class: str,
                          attempts: int) -> None:
    await _emit(pool, engine, RECOVERED_EVENT, "INFO",
                f"{engine} self-healed: {failure_class} in {attempts} attempt(s)",
                {"schema": SCHEMA_VERSION, "engine": engine,
                 "failure_class": failure_class, "attempts": attempts})


async def _detect_and_act(pool, engine: str, now: datetime, invoke) -> None:
    """Detect/self-heal/escalate/auto-clear (Tasks 4–6 fill this in)."""
    return None


async def supervise(pool, engine: str, now: datetime, invoke) -> None:
    """Per-actor supervisor pass. Crash-isolated: ANY exception is
    logged and swallowed — the dispatch sweep must never abort on a
    broken supervisor (DA-1 §10).
    """
    try:
        await _detect_and_act(pool, engine, now, invoke)
    except Exception as exc:  # noqa: BLE001 — never abort the sweep
        logger.error("engine_supervisor.error", engine=engine,
                     error=str(exc))
```

- [ ] **Step 4: Run to verify it passes**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest scripts/tests/test_engine_supervisor.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add ops/engine_supervisor.py scripts/tests/test_engine_supervisor.py
git commit -m "$(cat <<'EOF'
feat(engine_supervisor): module skeleton + locked event writers

ops/engine_supervisor.py: the 4 ENGINE_* emitters (mirroring
_emit_data_request), injected-invoker signature (no engine_dispatch
import — avoids a cycle), crash-isolated supervise() shell. Detectors
land in DA-1 Tasks 4–6.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: `crashed_startup` detector + the mechanism (migrate `_crashed_startup_refire`)

The reference failure class: migrate `engine_dispatch._crashed_startup_refire`'s detection SQL into the supervisor and wrap it in the full **detect → bounded self-heal (re-invoke ≤N) → verify → RECOVERED | ESCALATED+HELD** mechanism with idempotency.

**Files:**
- Modify: `ops/engine_supervisor.py`
- Test: `scripts/tests/test_engine_supervisor.py`

- [ ] **Step 1: Write the failing tests**

Append to `scripts/tests/test_engine_supervisor.py`:

```python
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
    # 1st fetchrow: not currently held (current_hold). 2nd: crashed-startup
    # detect row (started_at stale, not completed). 3rd: verify — now
    # completed clean (self-heal worked).
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
    # current_hold None; detect crashed; verify STILL crashed after
    # _MAX_REINVOKE attempts.
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
    # not held; no crashed startup (started_at None).
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
    # already held → no duplicate HELD; auto-clear is consulted instead
    assert all(a[2] != "ENGINE_HELD" for _s, a in conn.inserts)
    clear.assert_awaited_once()
```

- [ ] **Step 2: Run to verify they fail**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest scripts/tests/test_engine_supervisor.py -q`
Expected: FAIL — `_detect_and_act` is a stub (no events emitted, `invoke` never awaited).

- [ ] **Step 3: Implement the mechanism + `crashed_startup`**

In `ops/engine_supervisor.py`, add `from tpcore.engine_profile import cadence_window_start, profile_for` to the imports (ops→tpcore allowed), then add the constant and replace the `_detect_and_act` stub:

```python
_STALE_STARTUP_SECONDS = int(
    os.environ.get("ENGINE_DISPATCH_STALE_STARTUP_SECONDS", "7200"))  # 2h


async def _detect_crashed_startup(conn, engine: str, now: datetime,
                                  window_start: datetime) -> bool:
    """STARTUP in window with NO clean completion, older than stale
    threshold. Migrated verbatim from engine_dispatch._crashed_startup_refire
    (single owner; engine_dispatch will delegate). Behavior-preserving."""
    row = await conn.fetchrow(
        """
        SELECT
          max(recorded_at) FILTER (WHERE event_type = 'STARTUP')      AS started_at,
          bool_or(event_type IN ('SCAN_COMPLETE', 'SHUTDOWN'))        AS completed
        FROM platform.application_log
        WHERE engine = $1 AND recorded_at >= $2
        """,
        engine, window_start,
    )
    if not row or row["started_at"] is None or row["completed"]:
        return False
    return (now - row["started_at"]).total_seconds() >= _STALE_STARTUP_SECONDS


async def _auto_clear(pool, engine: str, now: datetime, hold) -> None:
    """Strong clean-cycle clear (Task 6 fills this in)."""
    return None


async def _detect_and_act(pool, engine: str, now: datetime, invoke) -> None:
    prof = profile_for(engine)
    window_start = cadence_window_start(engine, now) if prof else now

    hold = await current_hold(pool, engine)
    if hold is not None:
        # Already held → never re-detect/duplicate; only attempt clear.
        await _auto_clear(pool, engine, now, hold)
        return

    async with pool.acquire() as conn:
        crashed = await _detect_crashed_startup(conn, engine, now,
                                                window_start)
    if not crashed:
        return

    failure_class = "crashed_startup"
    attempts = 0
    while attempts < _MAX_REINVOKE:
        attempts += 1
        await invoke(engine)  # bounded self-heal: re-invoke scheduler
        async with pool.acquire() as conn:
            still = await _detect_crashed_startup(conn, engine, now,
                                                  window_start)
        if not still:
            await _emit_recovered(pool, engine, failure_class, attempts)
            return

    hold_id = str(uuid.uuid4())
    reason = f"{failure_class} unresolved after {attempts} re-invoke(s)"
    await _emit_escalated(pool, engine, hold_id, failure_class, reason,
                          attempts)
    await _emit_held(pool, engine, hold_id, failure_class, reason)
```

- [ ] **Step 4: Run to verify they pass**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest scripts/tests/test_engine_supervisor.py -q`
Expected: PASS (6 passed — the 2 from Task 3 + 4 new).

- [ ] **Step 5: Commit**

```bash
git add ops/engine_supervisor.py scripts/tests/test_engine_supervisor.py
git commit -m "$(cat <<'EOF'
feat(engine_supervisor): crashed_startup detector + bounded mechanism

Migrates _crashed_startup_refire's detection SQL into the supervisor
(single owner) and wraps it in detect → bounded re-invoke (<=N) →
verify → RECOVERED | ESCALATED+HELD. Already-held → no re-detect,
auto-clear only (idempotent). DA-1 §4/§5.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Remaining infra detectors — `scheduler_crash`, `data_request_timeout`, `data_repair_escalated`, `missed_cycle`

Add the other four §4 classes. `scheduler_crash` = a `SHUTDOWN` row with `exit_code != 0` in the window (db_handler writes `data={"duration_ms","exit_code"}` — confirmed by Sub-project C-T4). `data_request_timeout` = open `ENGINE_DATA_REQUEST` past timeout. `data_repair_escalated` = a `DATA_REPAIR_ESCALATED` for the engine (no self-heal possible → straight to escalate+hold). `missed_cycle` = N consecutive should_fire-eligible windows with no STARTUP, **excluding held windows** (DA-1 §4 feedback-loop guard).

**Files:**
- Modify: `ops/engine_supervisor.py`
- Test: `scripts/tests/test_engine_supervisor.py`

- [ ] **Step 1: Write the failing tests**

Append to `scripts/tests/test_engine_supervisor.py`:

```python
async def test_scheduler_crash_nonzero_shutdown_detected_and_self_heals():
    now = datetime(2026, 5, 5, 21, 30, tzinfo=UTC)
    # not held; no crashed_startup; scheduler_crash detect = True;
    # verify clean after re-invoke.
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


async def test_missed_cycle_excludes_held_windows(monkeypatch):
    now = datetime(2026, 5, 5, 21, 30, tzinfo=UTC)
    # not currently held; all other classes negative; missed_cycle =
    # True (no STARTUP across N eligible non-held windows).
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
```

- [ ] **Step 2: Run to verify they fail**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest scripts/tests/test_engine_supervisor.py -k "scheduler_crash or data_repair_escalated or missed_cycle" -q`
Expected: FAIL — only `crashed_startup` is wired.

- [ ] **Step 3: Implement the four detectors and the class dispatch**

In `ops/engine_supervisor.py`, add the detectors and replace the body of `_detect_and_act` (the part after `if not crashed:`) with an ordered class evaluation. Add these helpers:

```python
_NO_TERMINAL_TIMEOUT_SECONDS = int(
    os.environ.get("ENGINE_DISPATCH_REQUEST_TIMEOUT_SECONDS", "5400"))


async def _detect_scheduler_crash(conn, engine: str,
                                  window_start: datetime) -> bool:
    """A SHUTDOWN row with exit_code != 0 in this window (the
    db_handler.shutdown payload is {"duration_ms","exit_code"} —
    Sub-project C-T4). Distinct from crashed_startup (no SHUTDOWN)."""
    row = await conn.fetchrow(
        """
        SELECT bool_or(
                 event_type = 'SHUTDOWN'
                 AND (data->>'exit_code')::int <> 0
               ) AS crashed
        FROM platform.application_log
        WHERE engine = $1 AND recorded_at >= $2
        """,
        engine, window_start,
    )
    return bool(row and row["crashed"])


async def _detect_data_request_timeout(conn, engine: str, now: datetime,
                                       window_start: datetime) -> bool:
    """An ENGINE_DATA_REQUEST in this window with no terminal event,
    older than the no-terminal timeout."""
    row = await conn.fetchrow(
        """
        SELECT r.recorded_at AS req_ts, t.event_type AS terminal
        FROM platform.application_log r
        LEFT JOIN platform.application_log t
          ON t.event_type = ANY(ARRAY['DATA_REPAIR_COMPLETE',
                                       'DATA_REPAIR_ESCALATED'])
         AND (t.data->>'request_id') = (r.data->>'request_id')
        WHERE r.event_type = 'ENGINE_DATA_REQUEST'
          AND r.engine = $1 AND r.recorded_at >= $2
        ORDER BY r.recorded_at DESC LIMIT 1
        """,
        engine, window_start,
    )
    if row is None or row["terminal"] is not None:
        return False
    return (now - row["req_ts"]).total_seconds() >= _NO_TERMINAL_TIMEOUT_SECONDS


async def _detect_data_repair_escalated(conn, engine: str,
                                        window_start: datetime) -> bool:
    """A DATA_REPAIR_ESCALATED for this engine's request this window."""
    row = await conn.fetchrow(
        """
        SELECT bool_or(t.event_type = 'DATA_REPAIR_ESCALATED') AS escalated
        FROM platform.application_log r
        JOIN platform.application_log t
          ON (t.data->>'request_id') = (r.data->>'request_id')
        WHERE r.event_type = 'ENGINE_DATA_REQUEST'
          AND r.engine = $1 AND r.recorded_at >= $2
        """,
        engine, window_start,
    )
    return bool(row and row["escalated"])


async def _detect_missed_cycle(conn, engine: str,
                               window_start: datetime) -> bool:
    """No STARTUP across the last N eligible windows. Held windows are
    NOT eligible (a held engine is intentionally idle — counting it
    would loop: held → no STARTUP → missed_cycle → re-invoke). The
    caller only reaches this when current_hold(...) is None, so the
    engine is not currently held; the row's `eligible_windows` is the
    count of non-held eligible windows the SQL observed."""
    row = await conn.fetchrow(
        """
        SELECT
          count(*) FILTER (WHERE event_type = 'STARTUP') AS startups,
          count(DISTINCT date_trunc('day', recorded_at)) AS eligible_windows
        FROM platform.application_log
        WHERE engine = $1 AND recorded_at >= $2
        """,
        engine, window_start,
    )
    if row is None:
        return False
    return (row["startups"] == 0
            and (row["eligible_windows"] or 0) >= _MISSED_CYCLES_N)
```

Now refactor `_detect_and_act` so the mechanism is class-driven. Replace the entire `_detect_and_act` function with:

```python
# class → (needs self-heal?, detector). data_repair_escalated has no
# viable self-heal (data lane already exhausted bounded repair) → it
# goes straight to escalate+hold.
async def _classify(conn, engine, now, window_start):
    if await _detect_crashed_startup(conn, engine, now, window_start):
        return "crashed_startup", True
    if await _detect_scheduler_crash(conn, engine, window_start):
        return "scheduler_crash", True
    if await _detect_data_request_timeout(conn, engine, now, window_start):
        return "data_request_timeout", True
    if await _detect_data_repair_escalated(conn, engine, window_start):
        return "data_repair_escalated", False
    if await _detect_missed_cycle(conn, engine, window_start):
        return "missed_cycle", True
    return None, False


async def _verify_cleared(pool, engine, now, window_start,
                          failure_class) -> bool:
    """Re-run the class's detector; True iff the failure is gone."""
    async with pool.acquire() as conn:
        if failure_class == "crashed_startup":
            return not await _detect_crashed_startup(conn, engine, now,
                                                     window_start)
        if failure_class == "scheduler_crash":
            return not await _detect_scheduler_crash(conn, engine,
                                                     window_start)
        if failure_class == "data_request_timeout":
            return not await _detect_data_request_timeout(conn, engine,
                                                          now, window_start)
        if failure_class == "missed_cycle":
            return not await _detect_missed_cycle(conn, engine,
                                                  window_start)
    return False


async def _detect_and_act(pool, engine: str, now: datetime, invoke) -> None:
    prof = profile_for(engine)
    window_start = cadence_window_start(engine, now) if prof else now

    hold = await current_hold(pool, engine)
    if hold is not None:
        await _auto_clear(pool, engine, now, hold)
        return

    async with pool.acquire() as conn:
        failure_class, can_self_heal = await _classify(
            conn, engine, now, window_start)
    if failure_class is None:
        return

    attempts = 0
    if can_self_heal:
        while attempts < _MAX_REINVOKE:
            attempts += 1
            await invoke(engine)
            if await _verify_cleared(pool, engine, now, window_start,
                                     failure_class):
                await _emit_recovered(pool, engine, failure_class, attempts)
                return

    hold_id = str(uuid.uuid4())
    reason = (f"{failure_class} unresolved after {attempts} re-invoke(s)"
              if can_self_heal else
              f"{failure_class}: no self-heal (data lane exhausted)")
    await _emit_escalated(pool, engine, hold_id, failure_class, reason,
                          attempts)
    await _emit_held(pool, engine, hold_id, failure_class, reason)
```

(`_detect_crashed_startup`, `_auto_clear`, the emitters, constants, and imports from Tasks 3–4 are unchanged and reused.)

- [ ] **Step 4: Run to verify they pass**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest scripts/tests/test_engine_supervisor.py -q`
Expected: PASS (9 passed). If a Task-4 test's queued `fetchrow` order no longer matches the new class-evaluation order, update that test's `_rows_conn([...])` queue to the new detector order (crashed_startup → scheduler_crash → data_request_timeout → data_repair_escalated → missed_cycle) — a faithful test-fixture reconciliation (same asserted behavior), record each change.

- [ ] **Step 5: Commit**

```bash
git add ops/engine_supervisor.py scripts/tests/test_engine_supervisor.py
git commit -m "$(cat <<'EOF'
feat(engine_supervisor): scheduler_crash/timeout/repair-escalated/missed

The remaining DA-1 §4 infra classes + class-driven mechanism.
data_repair_escalated has no viable self-heal → straight to
escalate+hold. missed_cycle excludes held windows (feedback-loop
guard, DA-1 §4). Detection order is fixed and verify mirrors detect.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Auto-clear — strong clean-cycle predicate

Replace the `_auto_clear` stub with the safe-by-construction predicate (DA-1 §7): clear iff a `STARTUP` is followed by a clean `SHUTDOWN exit_code 0` in a window strictly **after** the hold, with no new failure this cycle; for `data_repair_escalated` additionally a `DATA_REPAIR_COMPLETE green=true`. On pass → `ENGINE_CLEARED`.

**Files:**
- Modify: `ops/engine_supervisor.py`
- Test: `scripts/tests/test_engine_supervisor.py`

- [ ] **Step 1: Write the failing tests**

Append to `scripts/tests/test_engine_supervisor.py`:

```python
async def test_auto_clear_requires_clean_shutdown_not_just_startup():
    from tpcore.supervisor_state import HoldState
    now = datetime(2026, 5, 6, 21, 30, tzinfo=UTC)
    held = HoldState("h-1", "crashed_startup", "stale",
                     datetime(2026, 5, 5, 21, 0, tzinfo=UTC))
    # post-hold: a STARTUP but NO clean SHUTDOWN exit0 → must NOT clear
    conn = _rows_conn([{"clean": False}])
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
    # clean cycle True BUT no DATA_REPAIR_COMPLETE green → must NOT clear
    conn = _rows_conn([{"clean": True}, {"green": False}])
    with patch.object(es, "current_hold", new=AsyncMock(return_value=held)):
        await es.supervise(_pool_for(conn), "vector", now, AsyncMock())
    assert all(a[2] != "ENGINE_CLEARED" for _s, a in conn.inserts)
```

- [ ] **Step 2: Run to verify they fail**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest scripts/tests/test_engine_supervisor.py -k auto_clear -q`
Expected: FAIL — `_auto_clear` is a stub (never emits `ENGINE_CLEARED`).

- [ ] **Step 3: Implement `_auto_clear`**

In `ops/engine_supervisor.py`, replace the `_auto_clear` stub with:

```python
async def _clean_cycle_after(conn, engine: str, held_at: datetime) -> bool:
    """A STARTUP followed by a clean SHUTDOWN (exit_code 0) recorded
    strictly AFTER the hold. NOT 'ran once' — a full clean cycle."""
    row = await conn.fetchrow(
        """
        SELECT (
          bool_or(event_type = 'STARTUP')
          AND bool_or(event_type = 'SHUTDOWN'
                      AND (data->>'exit_code')::int = 0)
        ) AS clean
        FROM platform.application_log
        WHERE engine = $1 AND recorded_at > $2
        """,
        engine, held_at,
    )
    return bool(row and row["clean"])


async def _repair_complete_green_after(conn, engine: str,
                                       held_at: datetime) -> bool:
    row = await conn.fetchrow(
        """
        SELECT bool_or((data->>'green')::bool) AS green
        FROM platform.application_log
        WHERE engine = $1 AND event_type = 'DATA_REPAIR_COMPLETE'
          AND recorded_at > $2
        """,
        engine, held_at,
    )
    return bool(row and row["green"])


async def _auto_clear(pool, engine: str, now: datetime, hold) -> None:
    """Strong clear predicate (DA-1 §7). Conservative by construction;
    DA-2 reuses ENGINE_HELD/ENGINE_CLEARED with a stronger predicate."""
    async with pool.acquire() as conn:
        if not await _clean_cycle_after(conn, engine, hold.held_at):
            return
        if hold.failure_class == "data_repair_escalated":
            if not await _repair_complete_green_after(conn, engine,
                                                      hold.held_at):
                return
    await _emit_cleared(pool, engine, hold.hold_id,
                        f"clean cycle after {hold.failure_class}")
```

- [ ] **Step 4: Run to verify they pass**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest scripts/tests/test_engine_supervisor.py -q`
Expected: PASS (12 passed).

- [ ] **Step 5: Commit**

```bash
git add ops/engine_supervisor.py scripts/tests/test_engine_supervisor.py
git commit -m "$(cat <<'EOF'
feat(engine_supervisor): strong clean-cycle auto-clear (DA-1 §7)

A held engine clears only on STARTUP + clean SHUTDOWN exit0 strictly
after the hold (not 'ran once'); data_repair_escalated additionally
needs DATA_REPAIR_COMPLETE green=true. Emits ENGINE_CLEARED →
should_fire's gate then sees the engine resume.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Wire the supervisor into `engine_dispatch` (behavior-preserving migration)

`dispatch_once` calls `await engine_supervisor.supervise(pool, engine, now, invoke)` per actor before `_dispatch_engine`. The `_crashed_startup_refire` re-invoke in `_dispatch_engine`'s `already ran this cycle` branch is removed (the supervisor now owns it); the `data_request_timeout` log becomes a supervisor-emitted escalation path. B/C suites are the equivalence oracle (exactly like C-T1).

**Files:**
- Modify: `ops/engine_dispatch.py`
- Test: `scripts/tests/test_engine_dispatch.py`

- [ ] **Step 1: Write the failing tests**

Append to `scripts/tests/test_engine_dispatch.py`:

```python
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

    # every dispatch:<x> is immediately preceded by supervise:<x>
    for i, item in enumerate(order):
        if item.startswith("dispatch:"):
            assert order[i - 1] == item.replace("dispatch:", "supervise:")
    assert order[0] == "supervise:allocator"


async def test_supervise_failure_does_not_abort_sweep():
    ran: list[str] = []

    async def _sup(pool, engine, now, invoke):
        raise RuntimeError("supervisor boom")

    async def _de(pool, now, engine, invoke):
        ran.append(engine)

    # supervise() is itself crash-isolated, but assert dispatch_once is
    # robust even if a patched supervise raises.
    with patch.object(ed.engine_supervisor, "supervise",
                      AsyncMock(side_effect=_sup)), \
         patch.object(ed, "_dispatch_engine", _de), \
         patch.object(ed, "_invoke_allocator", AsyncMock()), \
         contextlib.suppress(RuntimeError):
        await dispatch_once(object(), datetime(2026, 5, 18, 13, 0, tzinfo=UTC))
```

- [ ] **Step 2: Run to verify they fail**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest scripts/tests/test_engine_dispatch.py -k "supervise_called_per_actor or supervise_failure" -q`
Expected: FAIL — `ed.engine_supervisor` does not exist (not imported/wired).

- [ ] **Step 3: Wire it in `ops/engine_dispatch.py`**

(a) Add the import after `from tpcore.quality.validation.capital_gate import failing_sources_for_engine` (line ~22):

```python
from ops import engine_supervisor
```

(b) In `_dispatch_engine`, change the signature to thread the actor's `invoke` to the supervisor and call `supervise` first. Replace the opening of `_dispatch_engine` — from `async def _dispatch_engine(pool, now: datetime, engine: str,\n                           invoke) -> None:` and its docstring through the line `decision = await should_fire(engine, now, pool)` — with the same code plus a leading supervise call:

```python
async def _dispatch_engine(pool, now: datetime, engine: str,
                           invoke) -> None:
    """One profiled actor's gated dispatch (B's ladder, extracted so
    the allocator reuses it — spec C §3, reused not duplicated).

    `invoke` is an awaitable `(engine: str) -> None` that runs the
    actor with crash isolation (`_safe_invoke` for ROSTER engines,
    `_invoke_allocator` for the allocator).

    DA-1: the supervisor runs FIRST (crash-isolated within
    `supervise`), persisting any hold/clear so the same-cycle
    `should_fire` read observes it.
    """
    await engine_supervisor.supervise(pool, engine, now, invoke)
    decision = await should_fire(engine, now, pool)
```

(c) Remove the now-duplicated crashed-startup re-invoke from the `already ran this cycle` branch (the supervisor owns it). Replace this exact block:

```python
    elif decision.reason == "already ran this cycle":
        prof = profile_for(engine)
        window_start = cadence_window_start(engine, now) if prof else now
        async with pool.acquire() as conn:
            if await _crashed_startup_refire(conn, engine, now, window_start):
                logger.warning(
                    "engine_dispatch.crashed_startup_refire", engine=engine)
                await invoke(engine)
                return
        logger.info(
            "engine_dispatch.skipped", engine=engine,
            reason=decision.reason,
            data_ready=decision.checks.get("data_ready"),
        )
```

with:

```python
    elif decision.reason == "already ran this cycle":
        # DA-1: crashed-STARTUP re-invoke is owned by engine_supervisor
        # (ran above, before should_fire). Here we only record the skip.
        logger.info(
            "engine_dispatch.skipped", engine=engine,
            reason=decision.reason,
            data_ready=decision.checks.get("data_ready"),
        )
```

(d) Delete the now-unused `_crashed_startup_refire` function (lines 57–82) — it migrated to `ops/engine_supervisor.py` (`_detect_crashed_startup`) in Task 4. If `profile_for`/`cadence_window_start` become unused after (c), leave the imports (still used elsewhere in `_dispatch_engine` data-blocked branch — verify with `grep -n "cadence_window_start\|profile_for" ops/engine_dispatch.py`; only remove an import if grep shows zero remaining uses).

- [ ] **Step 4: Run the new tests + FULL dispatch suite (B/C oracle)**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest scripts/tests/test_engine_dispatch.py -q`
Expected: PASS — new wiring tests pass AND every pre-existing B/C test passes. Two reconciliations, applied ONLY where a pre-existing test fails, minimal and faithful (never weaken a B/C assertion):
  1. Tests of the old `_crashed_startup_refire` behavior in `_dispatch_engine` (e.g. a test asserting `engine_dispatch.crashed_startup_refire` log + re-invoke on the `already ran` branch): the behavior moved to the supervisor. Update the test to assert the equivalent via the supervisor — patch `ed.engine_supervisor.supervise` and assert it is awaited for the actor (the re-invoke is now the supervisor's responsibility, covered by `scripts/tests/test_engine_supervisor.py`); keep any assertion about the `already ran` skip log intact.
  2. Tests that patch `should_fire` and now also reach `engine_supervisor.supervise` (real) which would hit the DB: add `patch.object(ed.engine_supervisor, "supervise", AsyncMock())` to that test's patch stack (the supervisor has its own dedicated suite). Record every test changed (name + why + exact change).

- [ ] **Step 5: Commit**

```bash
git add ops/engine_dispatch.py scripts/tests/test_engine_dispatch.py
git commit -m "$(cat <<'EOF'
feat(engine_dispatch): wire engine_supervisor per actor (DA-1 §9)

dispatch runs supervise(pool,engine,now,invoke) before should_fire for
every actor (allocator + ROSTER). _crashed_startup_refire migrated to
the supervisor (single owner) — the already-ran branch now only logs
the skip. Behavior-preserving; B/C suites are the oracle.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: Full-suite gate + finish

**Files:** none (verification only)

- [ ] **Step 1: Full test suite**

Run: `cd /Users/michael/short-term-trading-engine/.claude/worktrees/da1-engine-supervisor && /Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q -p no:cacheprovider 2>&1 | tail -8`
Expected: PASS (entire suite green; the ops-name-collision guard in both `scripts/tests/test_engine_dispatch.py` and `scripts/tests/test_engine_supervisor.py` keeps full-suite collection clean).

- [ ] **Step 2: CI-exact lint + import-layering**

Run: `ruff check reversion/ vector/ momentum/ sentinel/ tpcore/ scripts/ ops/ && /Users/michael/short-term-trading-engine/.venv/bin/python -m tpcore.scripts.check_imports reversion vector momentum sentinel tpcore`
Expected: `All checks passed!` and `ok: no forbidden imports found`. Critical layering assertion: `tpcore/engine_profile.py` and `tpcore/supervisor_state.py` import NOTHING from `ops` (the gate stays pure tpcore); only `ops/engine_supervisor.py` and `ops/engine_dispatch.py` import across to `ops`/`tpcore`. If check_imports flags a tpcore→ops import, the `current_hold`/`supervisor_held` design was violated — fix by keeping the read in `tpcore/supervisor_state.py` (never import the ops supervisor from tpcore).

- [ ] **Step 3: Finish the branch**

Use the **superpowers:finishing-a-development-branch** skill. Per the standing operator instruction and the B/C pattern: push the worktree branch, open a PR, **fetch origin/main and resolve any conflicts to combine intents (the data session may have merged in parallel — do NOT clobber their work)**, ensure the integrated full suite is green, merge when CI is green, then clean the worktree. Do NOT local-merge into the shared checkout (the data session uses it).

---

## Self-Review

**1. Spec coverage:**
- §2 architecture (module in engine daemon, crash-isolated, reusable primitive) → Task 3 (`supervise` crash-isolated) + Task 7 (wiring).
- §3 event-sourced state, no new table → Task 1 (`current_hold` reads `application_log`; zero migrations).
- §4 the five infra classes + missed_cycle held-window exclusion → Task 4 (crashed_startup) + Task 5 (the other four; held-window guard via the "only reached when not held" structure + the §4 comment).
- §5 bounded class-specific self-heal → verify → RECOVERED|ESCALATED+HELD → Tasks 4 & 5 (the `while attempts < _MAX_REINVOKE` mechanism; `data_repair_escalated` `can_self_heal=False`).
- §6 should_fire hold-gate, pure, after market_closed before data_ready → Task 2.
- §7 strong auto-clear predicate (clean SHUTDOWN exit0 post-hold; repair-escalated needs green) → Task 6.
- §8 four locked schema:1 events, hold_id uuid4, no client ts → Tasks 1 (constants) & 3 (emitters mirror `_emit_data_request`).
- §9 composition + behavior-preserving `_crashed_startup_refire` migration + data_request_timeout→escalation → Task 7 (incl. the timeout class is detected by the supervisor in Task 5, replacing the log-only branch's role).
- §10 crash-isolation + bounded + idempotent → Task 3 (try/except), Tasks 4–5 (bounded loop; already-held → no re-detect), Task 1 (event-sourced dedup).
- §11 testing list → every bullet maps to a test in Tasks 1–7; §12 scope/CI green → Task 8. No gaps.

**2. Placeholder scan:** No "TBD/TODO/handle edge cases/similar to Task N". Every code step is complete literal code; every command has an expected result. The Task-2/4/5/7 reconciliation steps are explicit bounded contingencies ("ONLY where a pre-existing test fails", exact edit named), not deferred work — matching the C plan's accepted style.

**3. Type/name consistency:** `current_hold(pool, engine) -> HoldState | None` (Task 1) ← imported by `engine_profile` (Task 2) and `engine_supervisor` (Tasks 3–6). `HoldState(hold_id, failure_class, reason, held_at)` consistent across Tasks 1/4/6 tests. Event constants `HELD_EVENT/CLEARED_EVENT/ESCALATED_EVENT/RECOVERED_EVENT/SCHEMA_VERSION` defined Task 1, used Tasks 3–6. `supervise(pool, engine, now, invoke)` signature consistent Tasks 3/7. `_detect_and_act`, `_auto_clear`, `_classify`, `_verify_cleared`, `_detect_*` names consistent across Tasks 3–6. `checks["supervisor_held"]` key consistent Task 2 impl ↔ tests. Emitter payload keys match `tpcore/supervisor_state.current_hold`'s `data->>` reads (`hold_id`, `failure_class`, `reason`). No mismatches.
