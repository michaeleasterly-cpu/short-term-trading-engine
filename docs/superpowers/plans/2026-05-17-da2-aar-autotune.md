# DA-2 — AAR Auto-Tune Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A deterministic agent that reads `platform.forensics_triggers` and behaviorally stands an engine down (via DA-1's `ENGINE_HELD` primitive) on systemic decay signals, escalate-only on noise, cleared only by operator-resolving the trigger.

**Architecture:** New `ops/aar_autotune.py` reuses DA-1's `tpcore.supervisor_state` primitive (`current_hold`, the locked event vocabulary) with its OWN emitters mirroring the locked `application_log` INSERT (no `ops`→`ops` coupling). `should_fire`'s existing `supervisor_held` gate enforces the stand-down for free. One surgical guard added to `engine_supervisor._auto_clear` so DA-1 never auto-resumes a behavioral hold. Wired per-actor in `engine_dispatch.dispatch_once` between `_safe_supervise` and `_dispatch_engine`.

**Tech Stack:** Python 3.11, asyncio, asyncpg, structlog, pytest (`asyncio_mode = "auto"`), `platform.application_log` + read-only `platform.forensics_triggers`.

**Lane / scope discipline:** Touches ONLY `ops/aar_autotune.py` (new), `ops/engine_dispatch.py`, `ops/engine_supervisor.py` (the one `_auto_clear` guard), and test files (`scripts/tests/test_aar_autotune.py` new, `scripts/tests/test_engine_supervisor.py`, `scripts/tests/test_engine_dispatch.py`). Does NOT touch `tpcore/forensics/*` (producer — DA-2 only reads its output table), `tpcore/supervisor_state.py`/`tpcore/engine_profile.py` (the gate already enforces any `ENGINE_HELD`), the data lane (`tpcore/selfheal`, `tpcore/feeds`, `tpcore/ingestion`, `ops/data_repair_service.py`, `ops/cutover_agent.py`, `ops/weekly_digest.py`), or allocation/risk logic. CI-exact gates: `ruff check reversion/ vector/ momentum/ sentinel/ tpcore/ scripts/ ops/` and `python -m tpcore.scripts.check_imports reversion vector momentum sentinel tpcore`. venv: `/Users/michael/short-term-trading-engine/.venv/bin/python`; `ruff` on PATH as a binary. **`ops/aar_autotune.py` MUST NOT import `ops.engine_supervisor`** (own emitters; ops→ops coupling forbidden per spec §2).

---

## File Structure

| File | Responsibility | Change |
|------|----------------|--------|
| `ops/engine_supervisor.py` | DA-1 infra supervisor | Add infra-class guard to `_auto_clear` (1 early-return) |
| `ops/aar_autotune.py` | DA-2 agent: forensics → behavioral hold/escalate/clear | Create |
| `ops/engine_dispatch.py` | Dispatcher | Add `_safe_autotune` + wire per actor |
| `scripts/tests/test_engine_supervisor.py` | — | Add seam-guard tests |
| `scripts/tests/test_aar_autotune.py` | — | Create |
| `scripts/tests/test_engine_dispatch.py` | — | Add wiring test; reconcile |

---

## Task 1: `engine_supervisor._auto_clear` infra-class guard (the only DA-1 touch)

`current_hold` returns the latest uncleared `ENGINE_HELD` regardless of `failure_class`. DA-1's `_detect_and_act` does `if hold is not None: await _auto_clear(...); return`. For a future `behavioral` hold, DA-1's clean-cycle `_auto_clear` would wrongly emit `ENGINE_CLEARED`. Guard `_auto_clear` to only clear DA-1's own infra classes. Behavior-preserving for all infra classes — DA-1's suite is the oracle. Do this FIRST so DA-2's behavioral holds are safe by construction.

**Files:**
- Modify: `ops/engine_supervisor.py` (`_auto_clear`, lines ~146–157)
- Test: `scripts/tests/test_engine_supervisor.py`

- [ ] **Step 1: Write the failing tests**

Append to `scripts/tests/test_engine_supervisor.py`:

```python
async def test_auto_clear_ignores_behavioral_holds():
    """DA-2 seam guard: DA-1's _auto_clear must NOT clear a hold whose
    failure_class is not one of DA-1's infra classes (behavioral holds
    are DA-2-owned, operator-cleared)."""
    from tpcore.supervisor_state import HoldState
    now = datetime(2026, 5, 6, 21, 30, tzinfo=UTC)
    held = HoldState("h-b", "behavioral", "drawdown_period: fp-1",
                     datetime(2026, 5, 5, 21, 0, tzinfo=UTC))
    # A clean cycle exists post-hold — DA-1 WOULD clear an infra hold here.
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
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd /Users/michael/short-term-trading-engine/.claude/worktrees/da2-aar-autotune && /Users/michael/short-term-trading-engine/.venv/bin/python -m pytest scripts/tests/test_engine_supervisor.py -k "ignores_behavioral or clears_each_infra" -q`
Expected: `test_auto_clear_ignores_behavioral_holds` FAILS (DA-1 currently clears it on the clean cycle); the infra test passes (no guard yet).

- [ ] **Step 3: Add the guard**

In `ops/engine_supervisor.py`, in `_auto_clear`, add the early-return as the FIRST statement of the function body (before `async with pool.acquire() as conn:`). The function becomes:

```python
async def _auto_clear(pool, engine: str, now: datetime, hold) -> None:
    """Strong clear predicate (DA-1 §7). Conservative by construction;
    DA-2 reuses ENGINE_HELD/ENGINE_CLEARED with a stronger predicate."""
    # DA-2 seam guard: DA-1 only clears the infra classes it created.
    # Behavioral holds (failure_class="behavioral") are DA-2-owned and
    # operator-cleared — DA-1 must never auto-resume them.
    if hold.failure_class not in (
            "crashed_startup", "scheduler_crash", "data_request_timeout",
            "data_repair_escalated", "missed_cycle"):
        return
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

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest scripts/tests/test_engine_supervisor.py -k "ignores_behavioral or clears_each_infra" -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Run the full supervisor suite — behavior preserved (DA-1 oracle)**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest scripts/tests/test_engine_supervisor.py -q`
Expected: PASS (all pre-existing DA-1 supervisor tests still green — the guard is a no-op for every infra class; only behavioral/unknown classes are newly skipped). If a pre-existing test fails, the guard changed infra behavior — fix the guard, do not edit the assertion.

- [ ] **Step 6: Commit**

```bash
git add ops/engine_supervisor.py scripts/tests/test_engine_supervisor.py
git commit -m "$(cat <<'EOF'
feat(engine_supervisor): _auto_clear infra-class guard (DA-2 §6 seam)

DA-1's _auto_clear now early-returns unless failure_class is one of
its own infra classes — behavioral holds (DA-2, operator-cleared)
must never be auto-resumed by DA-1's clean-cycle predicate.
Behavior-preserving for all infra classes; DA-1 suite is the oracle.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: `ops/aar_autotune.py` skeleton — emitters, constant, crash-isolated shell, open-triggers reader

The agent shell: own emitters mirroring the locked INSERT (no `ops.engine_supervisor` import), the `LOSS_CLUSTER_HOLD_LEN` knob, the `_open_triggers` read, and a crash-isolated `autotune(pool, engine, now)` delegating to a `_decide_and_act` stub (policy in Task 3).

**Files:**
- Create: `ops/aar_autotune.py`
- Test: `scripts/tests/test_aar_autotune.py`

- [ ] **Step 1: Write the failing test**

Create `scripts/tests/test_aar_autotune.py`:

```python
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
    pool = _pool_for(_rows_conn([]))
    async with pool.acquire() as conn:
        pass
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest scripts/tests/test_aar_autotune.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'ops.aar_autotune'`.

- [ ] **Step 3: Implement the skeleton**

Create `ops/aar_autotune.py`:

```python
"""AAR Auto-Tune (Sub-project DA-2).

Deterministic behavioral control: reads platform.forensics_triggers
and, on SYSTEMIC decay signals (loss_cluster >= LOSS_CLUSTER_HOLD_LEN,
drawdown_period), stands the engine down by emitting ENGINE_HELD with
failure_class="behavioral" (reusing DA-1's tpcore.supervisor_state
primitive — the should_fire `supervisor_held` gate enforces it for
free). Noise signals (outlier_loss, short loss clusters) ESCALATE
only. Behavioral holds are OPERATOR-cleared: cleared only when the
HOLD-eligible triggers are operator-resolved (forensics_triggers.
resolved_at), re-evaluated against currently-open triggers.

Crash-isolated: a broken autotune must NEVER abort the dispatch sweep
or block trading (same invariant as DA-1/allocator). Has its OWN
emitters mirroring the locked application_log INSERT — does NOT import
ops.engine_supervisor (no ops->ops coupling; spec §2).
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime

import structlog

from tpcore.supervisor_state import (
    CLEARED_EVENT,
    ESCALATED_EVENT,
    HELD_EVENT,
    SCHEMA_VERSION,
    current_hold,
)

logger = structlog.get_logger(__name__)

LOSS_CLUSTER_HOLD_LEN = int(
    os.environ.get("ENGINE_AUTOTUNE_LOSS_CLUSTER_HOLD_LEN", "5"))

_BEHAVIORAL = "behavioral"

_INSERT_SQL = """
    INSERT INTO platform.application_log
        (engine, run_id, event_type, severity, message, data)
    VALUES ($1, $2, $3, $4, $5, $6::jsonb)
"""


async def _emit(pool, engine: str, event_type: str, severity: str,
                message: str, payload: dict) -> None:
    """One application_log row, mirroring the locked INSERT
    (engine_dispatch._emit_data_request / engine_supervisor._emit)."""
    async with pool.acquire() as conn:
        await conn.execute(
            _INSERT_SQL, engine, uuid.uuid4(), event_type, severity,
            message, json.dumps(payload, default=str),
        )


async def _emit_held(pool, engine: str, hold_id: str, reason: str,
                     triggers: list[str]) -> None:
    await _emit(pool, engine, HELD_EVENT, "ERROR",
                f"{engine} held: behavioral — {reason}",
                {"schema": SCHEMA_VERSION, "hold_id": hold_id,
                 "engine": engine, "failure_class": _BEHAVIORAL,
                 "reason": reason, "triggers": triggers})


async def _emit_escalated(pool, engine: str, hold_id: str, reason: str,
                          triggers: list[str]) -> None:
    await _emit(pool, engine, ESCALATED_EVENT, "ERROR",
                f"{engine} escalated: behavioral — {reason}",
                {"schema": SCHEMA_VERSION, "hold_id": hold_id,
                 "engine": engine, "failure_class": _BEHAVIORAL,
                 "reason": reason, "triggers": triggers})


async def _emit_cleared(pool, engine: str, hold_id: str,
                        clear_reason: str) -> None:
    await _emit(pool, engine, CLEARED_EVENT, "INFO",
                f"{engine} cleared: {clear_reason}",
                {"schema": SCHEMA_VERSION, "hold_id": hold_id,
                 "engine": engine, "clear_reason": clear_reason})


async def _open_triggers(pool, engine: str) -> list[dict]:
    """Unresolved forensics_triggers for ``engine`` (resolved_at NULL),
    newest first. Read-only; DA-2 never writes forensics_triggers."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, trigger_kind, payload
            FROM platform.forensics_triggers
            WHERE resolved_at IS NULL
              AND payload->>'engine' = $1
            ORDER BY fired_at DESC
            """,
            engine,
        )
    out: list[dict] = []
    for r in rows:
        payload = r["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        out.append({"id": r["id"], "trigger_kind": r["trigger_kind"],
                    "payload": payload})
    return out


async def _decide_and_act(pool, engine: str, now: datetime) -> None:
    """Policy + emit (Task 3 fills this in)."""
    return None


async def autotune(pool, engine: str, now: datetime) -> None:
    """Per-actor behavioral pass. Crash-isolated: ANY exception is
    logged and swallowed — the dispatch sweep must never abort on a
    broken autotune (spec §9)."""
    try:
        await _decide_and_act(pool, engine, now)
    except Exception as exc:  # noqa: BLE001 — never abort the sweep
        logger.error("aar_autotune.error", engine=engine, error=str(exc))
```

- [ ] **Step 4: Run to verify it passes**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest scripts/tests/test_aar_autotune.py -q`
Expected: PASS (3 passed). (The first test's stray `async with` warm-up lines are harmless; if ruff/pytest flags an unused `conn`, simplify the test to only the `rec` path — keep the payload assertion identical.)

- [ ] **Step 5: ruff**

Run: `ruff check ops/aar_autotune.py scripts/tests/test_aar_autotune.py`
Expected: `All checks passed!` (run `ruff check --fix` for import-sort only if flagged, then re-verify).

- [ ] **Step 6: Commit**

```bash
git add ops/aar_autotune.py scripts/tests/test_aar_autotune.py
git commit -m "$(cat <<'EOF'
feat(aar_autotune): module skeleton + locked emitters + triggers read

ops/aar_autotune.py: own ENGINE_HELD/ESCALATED/CLEARED emitters
(mirroring the locked INSERT — no ops.engine_supervisor import),
LOSS_CLUSTER_HOLD_LEN=5 knob, _open_triggers reader (resolved_at IS
NULL, read-only), crash-isolated autotune() shell. Policy in DA2-T3.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Decision policy + hold/escalate emission (expert Option-A-refined)

Replace `_decide_and_act`: read open triggers, apply §3, emit. One-hold rule: if the engine already has ANY uncleared hold (`current_hold` not None), DA-2 emits NOTHING (Task 4 handles the behavioral-clear case). Otherwise: if any HOLD-eligible trigger (`loss_cluster` `streak_length >= LOSS_CLUSTER_HOLD_LEN` OR `drawdown_period`) → emit `ENGINE_HELD` + `ENGINE_ESCALATED`; else if any ESCALATE-only trigger (`outlier_loss`, `loss_cluster` 3–4) → emit `ENGINE_ESCALATED` only; else nothing.

**Files:**
- Modify: `ops/aar_autotune.py`
- Test: `scripts/tests/test_aar_autotune.py`

- [ ] **Step 1: Write the failing tests**

Append to `scripts/tests/test_aar_autotune.py`:

```python
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
    assert events == []  # an existing (infra) hold → DA-2 emits nothing


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
```

- [ ] **Step 2: Run to verify they fail**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest scripts/tests/test_aar_autotune.py -k "outlier or loss_cluster or drawdown or no_open_triggers or one_hold_rule or hold_payload" -q`
Expected: FAIL — `_decide_and_act` is a stub (no events emitted).

- [ ] **Step 3: Implement the policy**

In `ops/aar_autotune.py`, replace the `_decide_and_act` stub with:

```python
def _streak_len(payload: dict) -> int:
    """loss_cluster payload streak_length (int in the producer's JSON;
    tolerate str defensively)."""
    v = payload.get("streak_length", 0)
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _is_hold_eligible(trig: dict) -> bool:
    """Spec §3 HOLD set: drawdown_period, or loss_cluster with
    streak_length >= LOSS_CLUSTER_HOLD_LEN."""
    kind = trig["trigger_kind"]
    if kind == "drawdown_period":
        return True
    if kind == "loss_cluster":
        return _streak_len(trig["payload"]) >= LOSS_CLUSTER_HOLD_LEN
    return False


async def _decide_and_act(pool, engine: str, now: datetime) -> None:
    # One-hold rule (spec §6): if ANY uncleared hold exists, DA-2 never
    # emits a (second) hold/escalation here. Clearing a behavioral hold
    # is handled separately in autotune() (DA2-T4).
    if await current_hold(pool, engine) is not None:
        return

    triggers = await _open_triggers(pool, engine)
    if not triggers:
        return

    hold_eligible = [t for t in triggers if _is_hold_eligible(t)]
    if hold_eligible:
        fps = [t["payload"].get("fingerprint", "") for t in hold_eligible]
        kinds = sorted({t["trigger_kind"] for t in hold_eligible})
        hold_id = str(uuid.uuid4())
        reason = (f"{','.join(kinds)}: "
                  f"{len(hold_eligible)} open hold-eligible trigger(s)")
        await _emit_escalated(pool, engine, hold_id, reason, fps)
        await _emit_held(pool, engine, hold_id, reason, fps)
        return

    # Only ESCALATE-only triggers open (outlier_loss / short clusters).
    fps = [t["payload"].get("fingerprint", "") for t in triggers]
    kinds = sorted({t["trigger_kind"] for t in triggers})
    await _emit_escalated(pool, engine, str(uuid.uuid4()),
                          f"{','.join(kinds)}: escalate-only", fps)
```

- [ ] **Step 4: Run to verify they pass**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest scripts/tests/test_aar_autotune.py -q`
Expected: PASS (all — the 3 skeleton tests + 7 policy tests).

- [ ] **Step 5: ruff**

Run: `ruff check ops/aar_autotune.py scripts/tests/test_aar_autotune.py`
Expected: `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add ops/aar_autotune.py scripts/tests/test_aar_autotune.py
git commit -m "$(cat <<'EOF'
feat(aar_autotune): expert Option-A-refined policy + hold/escalate

_decide_and_act: one-hold rule (skip if any uncleared hold) →
drawdown_period | loss_cluster>=LOSS_CLUSTER_HOLD_LEN HOLD+ESCALATE;
outlier_loss / short clusters ESCALATE-only; behavioral payload
carries trigger kinds + fingerprints. DA-2 §3/§4/§6.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Operator-only clear (re-evaluate §3 vs currently-open triggers)

When the engine has an uncleared `failure_class="behavioral"` hold, DA-2 re-evaluates the §3 HOLD condition against currently-open triggers: if NO hold-eligible trigger remains open → emit `ENGINE_CLEARED`. This is the only place a behavioral hold clears (operator resolves the trigger → it leaves `_open_triggers` → re-eval is empty → clear). A newer hold-eligible trigger keeps it held (re-eval, not stale fingerprints).

**Files:**
- Modify: `ops/aar_autotune.py` (`autotune`)
- Test: `scripts/tests/test_aar_autotune.py`

- [ ] **Step 1: Write the failing tests**

Append to `scripts/tests/test_aar_autotune.py`:

```python
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
    # all hold-eligible triggers resolved → _open_triggers returns [] →
    # re-eval empty → clear.
    rec = _rows_conn([[]])
    with patch.object(at, "current_hold",
                      new=AsyncMock(return_value=_beh_hold("h-z"))):
        await at.autotune(_pool_for(rec), "reversion",
                          datetime(2026, 5, 5, 21, 30, tzinfo=UTC))
    cleared = [a for _s, a in rec.inserts if a[2] == "ENGINE_CLEARED"]
    assert len(cleared) == 1
    p = json.loads(cleared[0][-1])
    assert p["hold_id"] == "h-z" and p["schema"] == 1


async def test_behavioral_held_stays_when_only_escalate_triggers_open():
    # original drawdown resolved, but an outlier_loss is open: NOT
    # hold-eligible → engine clears (re-eval has no hold-eligible).
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
```

- [ ] **Step 2: Run to verify they fail**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest scripts/tests/test_aar_autotune.py -k "behavioral_held" -q`
Expected: FAIL — `autotune` never emits `ENGINE_CLEARED` (clear path not implemented).

- [ ] **Step 3: Implement the operator-clear path**

In `ops/aar_autotune.py`, replace `autotune` (keep `_decide_and_act` unchanged) so the behavioral-clear is checked when a behavioral hold exists, else the decision path runs:

```python
async def _maybe_clear_behavioral(pool, engine: str) -> None:
    """If the engine has an uncleared behavioral hold, clear it iff NO
    HOLD-eligible trigger remains open (spec §5 — re-evaluate §3
    against currently-open triggers, NOT a stale fingerprint match;
    operator resolves triggers via forensics_triggers.resolved_at)."""
    hold = await current_hold(pool, engine)
    if hold is None or hold.failure_class != _BEHAVIORAL:
        return
    triggers = await _open_triggers(pool, engine)
    if any(_is_hold_eligible(t) for t in triggers):
        return  # still systemically decayed (incl. a newer trigger)
    await _emit_cleared(pool, engine, hold.hold_id,
                        "no open hold-eligible forensics trigger")


async def autotune(pool, engine: str, now: datetime) -> None:
    """Per-actor behavioral pass. Crash-isolated: ANY exception is
    logged and swallowed — the dispatch sweep must never abort on a
    broken autotune (spec §9)."""
    try:
        await _maybe_clear_behavioral(pool, engine)
        await _decide_and_act(pool, engine, now)
    except Exception as exc:  # noqa: BLE001 — never abort the sweep
        logger.error("aar_autotune.error", engine=engine, error=str(exc))
```

Note: `_decide_and_act` already short-circuits via the one-hold rule (`current_hold is not None → return`), so when a hold is still active it does nothing after `_maybe_clear_behavioral`; when `_maybe_clear_behavioral` just emitted `ENGINE_CLEARED`, `current_hold` (read fresh inside `_decide_and_act`) still sees the now-cleared hold's row joined to the new CLEARED → returns None → `_decide_and_act` proceeds, but `_open_triggers` is empty (that's why we cleared) → no re-hold. Behavior is correct: clear this cycle, fresh decisions next cycle.

- [ ] **Step 4: Run to verify they pass**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest scripts/tests/test_aar_autotune.py -q`
Expected: PASS (all — skeleton + policy + 4 clear tests).

- [ ] **Step 5: ruff + commit**

```bash
ruff check ops/aar_autotune.py scripts/tests/test_aar_autotune.py
git add ops/aar_autotune.py scripts/tests/test_aar_autotune.py
git commit -m "$(cat <<'EOF'
feat(aar_autotune): operator-only behavioral clear (DA-2 §5)

_maybe_clear_behavioral: a behavioral hold clears ONLY when no
HOLD-eligible trigger remains open (re-evaluate §3 vs currently-open
triggers — operator resolves via forensics_triggers.resolved_at).
A newer hold-eligible trigger keeps it held (not fingerprint match).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Wire `_safe_autotune` into `engine_dispatch` (per actor, after supervise, before dispatch)

**Files:**
- Modify: `ops/engine_dispatch.py`
- Test: `scripts/tests/test_engine_dispatch.py`

- [ ] **Step 1: Write the failing tests**

Append to `scripts/tests/test_engine_dispatch.py`:

```python
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
    # every actor: supervise → autotune → dispatch, in that order
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

    assert ran == ["allocator", *ROSTER]  # sweep completed despite raise
```

- [ ] **Step 2: Run to verify they fail**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest scripts/tests/test_engine_dispatch.py -k "autotune_called_per_actor or autotune_failure" -q`
Expected: FAIL — `ed.aar_autotune` does not exist / `_safe_autotune` not wired.

- [ ] **Step 3: Wire it**

In `ops/engine_dispatch.py`:

(a) Add the import immediately after `from ops import engine_supervisor`:

```python
from ops import aar_autotune
```

(b) Add `_safe_autotune` immediately after `_safe_supervise` (mirrors it; NO `invoke` param — behavioral holds have no self-heal):

```python
async def _safe_autotune(pool, engine: str, now: datetime) -> None:
    """Call the behavioral auto-tune with call-site crash isolation
    (defense in depth — autotune() is already internally isolated; a
    broken autotune must NEVER abort the sweep, DA-2 §9)."""
    try:
        await aar_autotune.autotune(pool, engine, now)
    except Exception as exc:  # noqa: BLE001 — never abort the sweep
        logger.error("engine_dispatch.autotune_failed", engine=engine,
                     error=str(exc))
```

(c) In `_dispatch_allocator`, insert the autotune call between supervise and dispatch:

```python
async def _dispatch_allocator(pool, now: datetime) -> None:
    """Sub-project C (D-C1): the allocator is the FIRST gated step,
    before the engine ROSTER loop. Reuses B's exact ladder via
    `_dispatch_engine` with the canonical `_invoke_allocator`. DA-1:
    the supervisor runs first (crash-isolated within `supervise`),
    persisting any hold/clear so the same-cycle should_fire read sees
    it; on supervisor failure the dispatch still proceeds."""
    await _safe_supervise(pool, "allocator", now, _invoke_allocator)
    await _safe_autotune(pool, "allocator", now)
    await _dispatch_engine(pool, now, "allocator", _invoke_allocator)
```

(d) In `dispatch_once`, insert the autotune call between supervise and dispatch in the ROSTER loop:

```python
async def dispatch_once(pool, now: datetime) -> None:
    await _dispatch_allocator(pool, now)
    for engine in ROSTER:
        await _safe_supervise(pool, engine, now, _safe_invoke)
        await _safe_autotune(pool, engine, now)
        await _dispatch_engine(pool, now, engine, _safe_invoke)
```

- [ ] **Step 4: Run new tests + full dispatch suite (B/C/DA-1 oracle)**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest scripts/tests/test_engine_dispatch.py -q`
Expected: PASS — the 2 new tests AND every pre-existing B/C/DA-1 dispatch test. `dispatch_once` now also calls `aar_autotune.autotune` per actor. Reconcile ONLY a pre-existing test that now fails, minimal & faithful (never weaken): a test that patches `should_fire`/`supervise` and now also reaches the REAL `aar_autotune.autotune` (which would hit the fake pool) → add `patch.object(ed.aar_autotune, "autotune", AsyncMock())` to that test's patch stack (DA-2 has its own dedicated suite). Record each test changed (name + why + exact change).

- [ ] **Step 5: ruff + check_imports**

Run: `ruff check ops/engine_dispatch.py scripts/tests/test_engine_dispatch.py && /Users/michael/short-term-trading-engine/.venv/bin/python -m tpcore.scripts.check_imports reversion vector momentum sentinel tpcore`
Expected: `All checks passed!` and `ok: no forbidden imports found`. Confirm `grep -nE "engine_supervisor" ops/aar_autotune.py` is empty (no ops→ops coupling — DA-2 has its own emitters).

- [ ] **Step 6: Commit**

```bash
git add ops/engine_dispatch.py scripts/tests/test_engine_dispatch.py
git commit -m "$(cat <<'EOF'
feat(engine_dispatch): wire _safe_autotune per actor (DA-2 §7)

dispatch runs _safe_autotune(pool,engine,now) between _safe_supervise
and _dispatch_engine for every actor (allocator + ROSTER), call-site
crash-isolated (defense in depth). Behavior-preserving for B/C/DA-1
(autotune is a no-op with no forensics triggers); suites are the oracle.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Full-suite + CI gate + finish

**Files:** none (verification only)

- [ ] **Step 1: Full test suite**

Run: `cd /Users/michael/short-term-trading-engine/.claude/worktrees/da2-aar-autotune && /Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q -p no:cacheprovider 2>&1 | tail -6`
Expected: PASS (entire suite green; the ops-name-collision guard in all three `scripts/tests/test_*` files keeps full-suite collection clean).

- [ ] **Step 2: CI-exact lint + import-layering**

Run: `ruff check reversion/ vector/ momentum/ sentinel/ tpcore/ scripts/ ops/ && /Users/michael/short-term-trading-engine/.venv/bin/python -m tpcore.scripts.check_imports reversion vector momentum sentinel tpcore`
Expected: `All checks passed!` and `ok: no forbidden imports found`. Assert: `grep -rnE "^(import|from) ops.engine_supervisor" ops/aar_autotune.py` empty (DA-2 has its own emitters; no ops→ops coupling — spec §2); `tpcore/` unchanged (the `supervisor_held` gate already enforces any `ENGINE_HELD` — DA-2 added no tpcore change).

- [ ] **Step 3: Finish the branch**

Use the **superpowers:finishing-a-development-branch** skill. Per the DA-1/C pattern + standing operator instruction: push the worktree branch, open a PR, **fetch origin/main and resolve any conflicts to combine intents (the data session may have merged in parallel — do NOT clobber their work)**, ensure the integrated full suite is green, merge when CI is green, then clean the worktree. Do NOT local-merge into the shared checkout (the data session uses it).

---

## Self-Review

**1. Spec coverage:**
- §2 architecture (separate `ops/aar_autotune.py`, own emitters, no ops→ops, crash-isolated) → Task 2 + Task 5 (`_safe_autotune`).
- §3 policy (Option-A-refined, `LOSS_CLUSTER_HOLD_LEN=5`) → Task 2 (constant) + Task 3 (`_is_hold_eligible`/`_decide_and_act`; outlier & short-cluster escalate-only; long-cluster & drawdown hold+escalate; every hold also escalates).
- §4 hold payload (`failure_class="behavioral"`, reason+`triggers` list) → Task 2 (`_emit_held`) + Task 3 (payload assembly + test).
- §5 operator-only clear via re-evaluation (not fingerprint match; newer trigger keeps held) → Task 4 (`_maybe_clear_behavioral` + 4 tests incl. newer-trigger).
- §6 seam guard + one-hold rule → Task 1 (`_auto_clear` infra guard, DA-1 oracle) + Task 3 (one-hold-rule `current_hold` short-circuit + test).
- §7 wiring order (supervise → autotune → dispatch, allocator first) → Task 5 (order test + crash-isolation test).
- §8 idempotency/bounded → Task 3 (one-hold rule = dedup) + Task 2 (bounded reads, ≤ few emits).
- §9 crash-isolation (internal + call-site) → Task 2 (`autotune` try/except) + Task 5 (`_safe_autotune`).
- §10 testing list → every bullet maps to a test in Tasks 1–5; §11 scope/CI green → Task 6. No gaps.

**2. Placeholder scan:** No "TBD/TODO/handle edge cases/similar to Task N". Every code step is complete literal code; every command has an expected result. Task 4/5 reconciliation steps are explicit bounded contingencies with the exact patch named (matches the accepted DA-1/C style). One test in Task 2 has a benign warm-up snippet flagged with an inline simplification instruction — not a placeholder.

**3. Type/name consistency:** `LOSS_CLUSTER_HOLD_LEN` (Task 2) used in Task 3/4 `_is_hold_eligible`. `_emit`/`_emit_held(pool,engine,hold_id,reason,triggers)`/`_emit_escalated`/`_emit_cleared` consistent Tasks 2–4. `_open_triggers`→`list[dict]` with `{"id","trigger_kind","payload"}` consistent Tasks 2–4. `_decide_and_act`/`_maybe_clear_behavioral`/`autotune(pool,engine,now)` consistent Tasks 2–5. `_safe_autotune(pool,engine,now)` (no invoke) consistent Task 5 def ↔ wiring ↔ tests. `current_hold`/`HoldState.failure_class`/`_BEHAVIORAL="behavioral"` consistent with `tpcore.supervisor_state` and the Task 1 guard's infra-class set. Emitter payloads match `current_hold`'s `data->>` reads (`hold_id`,`failure_class`,`reason`). No mismatches.
