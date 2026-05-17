# Data Supervisor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the data lane the per-source event-sourced hold + autonomous auto-clear primitive it lacks vs the engine DA-1 supervisor, without touching the sacred whole-cycle emit gate.

**Architecture:** A new `tpcore/datasupervisor/` package (symmetric to `tpcore/selfheal`/`tpcore/auditheal`, data-native). `state.py` defines the locked schema:1 `DATA_SOURCE_HELD/CLEARED/ESCALATED/RECOVERED` vocabulary + `current_source_hold` event-sourced read (mirrors `tpcore/supervisor_state.current_hold` keyed on `data->>'source'`). `supervisor.py` `datasupervise(pool, run_id)` runs one bounded pass after Step 4/4c: compute still-red sources by REUSING the existing selfheal/auditheal red predicates verbatim, open holds, auto-clear on a strong green predicate, bounded-escalate at M held cycles. `__main__.py` is a thin caller wired as a new Step in `run_data_operations.sh` that always exits 0 (state-tracking, not a gate).

**Tech Stack:** Python 3.11, asyncpg, pydantic v2 frozen models, pytest (`asyncio_mode=auto`), ruff. Spec: `docs/superpowers/specs/2026-05-17-data-supervisor-design.md`.

---

## File Structure

| File | Responsibility | Phase |
|---|---|---|
| `tpcore/datasupervisor/__init__.py` | package marker + public re-exports (mirror `tpcore/auditheal/__init__.py`) | P1 |
| `tpcore/datasupervisor/state.py` | `SCHEMA_VERSION`, 4 event constants, `SourceHoldState`, `current_source_hold(pool, source)` | P1 |
| `tpcore/tests/test_datasupervisor_state.py` | fake-pool state-read tests | P1 |
| `tpcore/datasupervisor/supervisor.py` | `datasupervise(pool, run_id)` — red-source detection (reuses existing predicates), open/auto-clear/bounded-escalate, `DataSupervisorOutcome` | P2 |
| `tpcore/datasupervisor/__main__.py` | thin CLI caller, exit 0 always (DSN guard only) | P2 |
| `tpcore/tests/test_datasupervisor.py` | fake-pool/fake-clock unit tests, mirrors `test_selfheal.py` | P2 |
| `scripts/run_data_operations.sh` | new thin Step after Step 4c, before the emit | P3 |
| `CLAUDE.md`, the spec, memory pointer | reconciliation | P4 |

One phase = one gated PR. Branch off fresh `main` per phase; CI green before merge; verify branch before every commit. Implementers commit only; the controller opens/merges PRs after spec + code-quality review (auditheal/contract-sentinel pattern).

---

## Ground truth (verified from source — do not re-derive/guess)

**application_log insert** (mirror `ops/data_repair_service.py` `_INSERT_SQL`, == `tpcore/logging/db_handler.py` convention):
```sql
INSERT INTO platform.application_log
    (engine, run_id, event_type, severity, message, data)
VALUES ($1, $2, $3, $4, $5, $6::jsonb)
```
`engine` = a constant tag string (`"datasupervisor"`); `run_id` = a uuid4 per emit; `data` = `json.dumps(payload, default=str)`; `recorded_at` is DB-assigned (never sent — satisfies the no-client-timestamp contract).

**Event-sourced hold read** mirrors `tpcore/supervisor_state.current_hold` EXACTLY but keys on the source carried in `data` (data sources have no `engine` semantics):
```sql
SELECT h.data->>'hold_id' AS hold_id, h.data->>'reason' AS reason,
       h.recorded_at AS held_at, c.event_type AS cleared
FROM platform.application_log h
LEFT JOIN platform.application_log c
  ON c.event_type = $2 AND (c.data->>'hold_id') = (h.data->>'hold_id')
WHERE h.event_type = $1 AND h.data->>'source' = $3
ORDER BY h.recorded_at DESC LIMIT 1
```
(`$1=DATA_SOURCE_HELD`, `$2=DATA_SOURCE_CLEARED`, `$3=source`). Open iff row exists and `cleared IS NULL`.

**Existing red predicates — REUSE verbatim (do not re-implement; a divergence desyncs the Supervisor from the gate):**
- selfheal: `tpcore/selfheal/orchestrator.py` `_RED_SQL` (latest `validation.%` rows where `stale OR confidence<1.0`). Each red `r["source"].removeprefix("validation.")` is a `check_name`; map to its feed via `tpcore.selfheal.registry.spec_for(check_name).source`.
- auditheal: `tpcore/auditheal/orchestrator.py` `_RED_SQL` (latest `cross_table_audit.%` rows, same `stale OR confidence<1.0`). `tpcore/auditheal/orchestrator._source_to_key` → `"<table>/<check>"`; the table = `key.split("/")[0]`.
- contract-sentinel: an `INGESTION_FAILED` row in the last 24h with `data->>'exception_type' = 'AdapterContractDrift'`. The feed is in `data->>'error'`, which (we own the message — `tpcore/ingestion/adapter_contract.AdapterContractDrift`, format `adapter_contract_drift: feed=<repr> required field ...`) is matched by regex `feed=(['\"]?)([a-z0-9_]+)\1`.

**Source key is NAMESPACED per detector** (verified: the 3 detectors use *different* name spaces — HealSpec.source ≈ feed names, cross_table `<table>` = platform table names, contract feed names; they do NOT share one space and must NOT be force-merged). The Supervisor's `source` key is therefore:
- `f"validation:{healspec_source}"`
- `f"cross_table:{table}"`
- `f"contract:{feed}"`
This guarantees uniqueness, no false-merge, and is honest. Cross-detector unification (for a future capital-gate consumer) is explicitly a later forward-seam, NOT this build.

**Cycle delimiter for the M-held-cycles count:** a data-ops cycle is uniquely identified by the wrapper `run_id`. `INGESTION_START` rows are emitted every cycle regardless of green/red (verified: `_log_event INGESTION_START wrapper_*` fires before selfheal/audit each run; a failed cycle still emits them — robust where counting `DATA_OPERATIONS_COMPLETE` would undercount). `held_cycles(pool, held_at)` = `SELECT COUNT(DISTINCT run_id) FROM platform.application_log WHERE event_type='INGESTION_START' AND recorded_at > $1`.

**Package/structure mirror:** `tpcore/auditheal/__init__.py` re-exports the public surface with `__all__`; orchestrator/state are submodules; `__main__.py` is the thin caller with a DSN guard returning 1 only on missing `DATABASE_URL`. `build_asyncpg_pool` is `from tpcore.db import build_asyncpg_pool`. pytest `asyncio_mode=auto` (async tests need no marker).

---

## Phase 1 — `tpcore/datasupervisor/state.py` (vocabulary + read), dark (PR 1)

Branch: `feat/datasupervisor-p1`.

### Task 1.1: state module + event-sourced read

**Files:**
- Create: `tpcore/datasupervisor/__init__.py`
- Create: `tpcore/datasupervisor/state.py`
- Test: `tpcore/tests/test_datasupervisor_state.py`

- [ ] **Step 1: Write the failing test** — create `tpcore/tests/test_datasupervisor_state.py`:

```python
"""Unit tests for the data-supervisor event-sourced hold read.

Pure: a fake asyncpg pool returning scripted rows. No DB. Mirrors the
fake-pool style of tpcore/tests/test_selfheal.py.
"""
from __future__ import annotations

from datetime import UTC, datetime

from tpcore.datasupervisor.state import (
    CLEARED_EVENT,
    HELD_EVENT,
    SCHEMA_VERSION,
    SourceHoldState,
    current_source_hold,
)


class _Conn:
    def __init__(self, row):
        self._row = row
        self.calls: list[tuple] = []

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        return self._row


class _CM:
    def __init__(self, c):
        self._c = c

    async def __aenter__(self):
        return self._c

    async def __aexit__(self, *e):
        return None


class _Pool:
    def __init__(self, row):
        self.conn = _Conn(row)

    def acquire(self):
        return _CM(self.conn)


def test_constants_locked() -> None:
    assert SCHEMA_VERSION == 1
    assert HELD_EVENT == "DATA_SOURCE_HELD"
    assert CLEARED_EVENT == "DATA_SOURCE_CLEARED"


async def test_open_hold_returned() -> None:
    now = datetime(2026, 5, 17, tzinfo=UTC)
    pool = _Pool({
        "hold_id": "h1", "reason": "validation:prices_daily red",
        "held_at": now, "cleared": None,
    })
    hold = await current_source_hold(pool, "validation:prices_daily")
    assert isinstance(hold, SourceHoldState)
    assert hold.hold_id == "h1" and hold.held_at == now
    # query bound the source as the 3rd arg, HELD/CLEARED as 1st/2nd
    sql, args = pool.conn.calls[0]
    assert args == ("DATA_SOURCE_HELD", "DATA_SOURCE_CLEARED",
                    "validation:prices_daily")
    assert "h.data->>'source' = $3" in sql


async def test_cleared_hold_is_none() -> None:
    pool = _Pool({
        "hold_id": "h1", "reason": "x",
        "held_at": datetime(2026, 5, 17, tzinfo=UTC),
        "cleared": "DATA_SOURCE_CLEARED",
    })
    assert await current_source_hold(pool, "contract:fred_macro") is None


async def test_no_row_is_none() -> None:
    assert await current_source_hold(_Pool(None), "cross_table:x") is None
```

- [ ] **Step 2: Run — expect FAIL** (`ModuleNotFoundError: tpcore.datasupervisor`).

Run: `source .venv/bin/activate && python -m pytest tpcore/tests/test_datasupervisor_state.py -q`

- [ ] **Step 3: Create `tpcore/datasupervisor/__init__.py`**

```python
"""Data Supervisor — per-source hold + autonomous auto-clear.

Data-native symmetric counterpart of the engine-lane DA-1 supervisor
(tpcore/supervisor_state.py + ops/engine_supervisor.py). NOT a copy:
per-source (not per-engine); consumes the rung-1 escalations
selfheal/auditheal/contract-sentinel already emit (does not re-heal);
the sacred whole-cycle emit gate is untouched (no new gate).
"""
from tpcore.datasupervisor.state import (
    CLEARED_EVENT,
    ESCALATED_EVENT,
    HELD_EVENT,
    RECOVERED_EVENT,
    SCHEMA_VERSION,
    SourceHoldState,
    current_source_hold,
)

__all__ = [
    "CLEARED_EVENT",
    "ESCALATED_EVENT",
    "HELD_EVENT",
    "RECOVERED_EVENT",
    "SCHEMA_VERSION",
    "SourceHoldState",
    "current_source_hold",
]
```

- [ ] **Step 4: Create `tpcore/datasupervisor/state.py`**

```python
"""Data-supervisor inter-lane vocabulary + event-sourced hold read.

Locked contract (schema:1, parity with tpcore/supervisor_state.py /
DATA_REPAIR_*): hold_id is a uuid4 string, the sole correlation key;
NO client timestamps (DB recorded_at only); one-terminal liveness — a
DATA_SOURCE_HELD is eventually followed by exactly one
DATA_SOURCE_CLEARED. Event-sourced from application_log; NO new table.

This module is the pure read + vocabulary. tpcore/datasupervisor/
supervisor.py is the sole WRITER of these events.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

SCHEMA_VERSION = 1

HELD_EVENT = "DATA_SOURCE_HELD"
CLEARED_EVENT = "DATA_SOURCE_CLEARED"
ESCALATED_EVENT = "DATA_SOURCE_ESCALATED"
RECOVERED_EVENT = "DATA_SUPERVISOR_RECOVERED"


@dataclass(frozen=True)
class SourceHoldState:
    """A source's currently-open supervisor hold."""

    hold_id: str
    reason: str
    held_at: datetime


_CURRENT_HOLD_SQL = """
    SELECT h.data->>'hold_id' AS hold_id,
           h.data->>'reason'  AS reason,
           h.recorded_at      AS held_at,
           c.event_type       AS cleared
    FROM platform.application_log h
    LEFT JOIN platform.application_log c
      ON c.event_type = $2 AND (c.data->>'hold_id') = (h.data->>'hold_id')
    WHERE h.event_type = $1 AND h.data->>'source' = $3
    ORDER BY h.recorded_at DESC LIMIT 1
"""


async def current_source_hold(
    pool: Any, source: str
) -> SourceHoldState | None:
    """The source's open hold, or None. Latest DATA_SOURCE_HELD for
    ``source`` whose hold_id has no later DATA_SOURCE_CLEARED."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            _CURRENT_HOLD_SQL, HELD_EVENT, CLEARED_EVENT, source
        )
    if row is None or row["cleared"] is not None:
        return None
    return SourceHoldState(
        hold_id=row["hold_id"],
        reason=row["reason"],
        held_at=row["held_at"],
    )


__all__ = [
    "CLEARED_EVENT",
    "ESCALATED_EVENT",
    "HELD_EVENT",
    "RECOVERED_EVENT",
    "SCHEMA_VERSION",
    "SourceHoldState",
    "current_source_hold",
]
```

- [ ] **Step 5: Run the tests — all 5 pass.** `python -m pytest tpcore/tests/test_datasupervisor_state.py -q`

- [ ] **Step 6: Lint + collection.** `ruff check tpcore/datasupervisor/ tpcore/tests/test_datasupervisor_state.py` (clean, no noqa) and `python -m pytest tpcore/tests/ tests/ -q --co 2>&1 | tail -1` (collection clean).

- [ ] **Step 7: Commit**

```bash
test "$(git branch --show-current)" = "feat/datasupervisor-p1" || { echo WRONG; exit 1; }
git add tpcore/datasupervisor/__init__.py tpcore/datasupervisor/state.py tpcore/tests/test_datasupervisor_state.py
git commit -m "feat(datasupervisor): state vocabulary + current_source_hold (dark)"
```
STOP. Report DONE (5/5, ruff, collection count, commit SHA) or BLOCKED.

---

## Phase 2 — `supervisor.py` + `__main__.py`, dark (PR 2)

Branch: `feat/datasupervisor-p2` off fresh `main`.

### Task 2.1: `datasupervise` + thin caller + tests

**Files:**
- Create: `tpcore/datasupervisor/supervisor.py`
- Create: `tpcore/datasupervisor/__main__.py`
- Test: `tpcore/tests/test_datasupervisor.py`

- [ ] **Step 1: Write the failing test** — create `tpcore/tests/test_datasupervisor.py`:

```python
"""Unit tests for datasupervise — fake pool whose red-set + holds +
cycle-count are scripted. No DB, no subprocess. Mirrors test_selfheal.
"""
from __future__ import annotations

from tpcore.datasupervisor.supervisor import datasupervise


class _Conn:
    def __init__(self, pool):
        self._p = pool

    async def fetch(self, sql, *a):
        if "validation.%" in sql:
            return [{"source": f"validation.{c}"} for c in self._p.val_red]
        if "cross_table_audit.%" in sql:
            return [{"source": f"cross_table_audit.{k}"}
                    for k in self._p.ct_red]
        if "AdapterContractDrift" in sql:
            return [{"error": f"adapter_contract_drift: feed={f!r} x"}
                    for f in self._p.contract_red]
        if "INGESTION_START" in sql:
            return [{"n": self._p.cycles_since_hold}]
        return []

    async def fetchrow(self, sql, *a):
        # current_source_hold: a=(HELD, CLEARED, source)
        return self._p.holds.get(a[2])

    async def fetchval(self, sql, *a):
        return self._p.cycles_since_hold

    async def execute(self, sql, *a):
        self._p.emitted.append((a[2], a[5]))  # (event_type, data-json)


class _CM:
    def __init__(self, c): self._c = c
    async def __aenter__(self): return self._c
    async def __aexit__(self, *e): return None


class _Pool:
    def __init__(self, *, val_red=(), ct_red=(), contract_red=(),
                 holds=None, cycles_since_hold=0):
        self.val_red = list(val_red)
        self.ct_red = list(ct_red)
        self.contract_red = list(contract_red)
        self.holds = holds or {}
        self.cycles_since_hold = cycles_since_hold
        self.emitted: list[tuple] = []

    def acquire(self): return _CM(_Conn(self))


def _events(pool):
    import json
    return [(et, json.loads(d)) for et, d in pool.emitted]


async def test_green_cycle_no_events() -> None:
    pool = _Pool()
    out = await datasupervise(pool, "rid")
    assert pool.emitted == [] and out.opened == [] and out.cleared == []


async def test_opens_hold_for_each_red_source(monkeypatch) -> None:
    import tpcore.datasupervisor.supervisor as S
    monkeypatch.setattr(S, "_healspec_source",
                        lambda c: "prices_daily")
    pool = _Pool(val_red=["prices_daily_freshness"],
                 ct_red=["tradier_options_chains/expiration_in_past"],
                 contract_red=["fred_macro"])
    out = await datasupervise(pool, "rid")
    ev = _events(pool)
    held = {e[1]["source"] for e in ev if e[0] == "DATA_SOURCE_HELD"}
    assert held == {"validation:prices_daily",
                    "cross_table:tradier_options_chains",
                    "contract:fred_macro"}
    assert set(out.opened) == held


async def test_idempotent_no_dup_when_already_held(monkeypatch) -> None:
    import tpcore.datasupervisor.supervisor as S
    from tpcore.datasupervisor.state import SourceHoldState
    from datetime import UTC, datetime
    monkeypatch.setattr(S, "_healspec_source", lambda c: "prices_daily")
    held = {"validation:prices_daily": {
        "hold_id": "h1", "reason": "r",
        "held_at": datetime(2026, 5, 17, tzinfo=UTC), "cleared": None}}
    pool = _Pool(val_red=["prices_daily_freshness"], holds=held,
                 cycles_since_hold=1)
    await datasupervise(pool, "rid")
    assert not any(et == "DATA_SOURCE_HELD" for et, _ in pool.emitted)


async def test_autoclear_when_source_green_after_hold(monkeypatch) -> None:
    from datetime import UTC, datetime
    held = {"contract:fred_macro": {
        "hold_id": "h9", "reason": "r",
        "held_at": datetime(2026, 5, 17, tzinfo=UTC), "cleared": None}}
    # no red anywhere this cycle -> source is green -> auto-clear
    pool = _Pool(holds=held, cycles_since_hold=1)
    out = await datasupervise(pool, "rid")
    ets = [et for et, _ in pool.emitted]
    assert "DATA_SOURCE_CLEARED" in ets
    assert "DATA_SUPERVISOR_RECOVERED" in ets
    assert out.cleared == ["contract:fred_macro"]


async def test_bounded_escalate_at_M_cycles(monkeypatch) -> None:
    import tpcore.datasupervisor.supervisor as S
    from datetime import UTC, datetime
    monkeypatch.setattr(S, "_healspec_source", lambda c: "prices_daily")
    held = {"validation:prices_daily": {
        "hold_id": "h1", "reason": "r",
        "held_at": datetime(2026, 5, 17, tzinfo=UTC), "cleared": None}}
    pool = _Pool(val_red=["prices_daily_freshness"], holds=held,
                 cycles_since_hold=3)  # == _MAX_HELD_CYCLES
    await datasupervise(pool, "rid")
    assert any(et == "DATA_SOURCE_ESCALATED" for et, _ in pool.emitted)


async def test_crash_isolated(monkeypatch) -> None:
    import tpcore.datasupervisor.supervisor as S

    async def boom(*a, **k):
        raise RuntimeError("db down")

    pool = _Pool(val_red=["x"])
    monkeypatch.setattr(S, "_red_sources", boom)
    out = await datasupervise(pool, "rid")  # must NOT raise
    assert out.error is not None and out.opened == []
```

- [ ] **Step 2: Run — expect FAIL** (`ModuleNotFoundError: tpcore.datasupervisor.supervisor`).

- [ ] **Step 3: Create `tpcore/datasupervisor/supervisor.py`**

```python
"""datasupervise — one bounded per-source hold/auto-clear pass.

Runs AFTER Step 4 (selfheal) / 4c (auditheal). It does NOT re-heal —
selfheal/auditheal own bounded repair; this consumes their red
outcome. Reuses their EXACT red predicates (single source of truth).
Sacred whole-cycle emit gate untouched; this never gates.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

import structlog

from tpcore.auditheal.orchestrator import _RED_SQL as _CT_RED_SQL
from tpcore.auditheal.orchestrator import _source_to_key as _ct_key
from tpcore.datasupervisor.state import (
    CLEARED_EVENT,
    ESCALATED_EVENT,
    HELD_EVENT,
    RECOVERED_EVENT,
    SCHEMA_VERSION,
    current_source_hold,
)
from tpcore.selfheal.orchestrator import _RED_SQL as _VAL_RED_SQL
from tpcore.selfheal.registry import spec_for

logger = structlog.get_logger(__name__)

_ENGINE_TAG = "datasupervisor"
_MAX_HELD_CYCLES = 3

_INSERT_SQL = """
INSERT INTO platform.application_log
    (engine, run_id, event_type, severity, message, data)
VALUES ($1, $2, $3, $4, $5, $6::jsonb)
"""

_CONTRACT_RED_SQL = """
    SELECT data->>'error' AS error
    FROM platform.application_log
    WHERE event_type = 'INGESTION_FAILED'
      AND data->>'exception_type' = 'AdapterContractDrift'
      AND recorded_at > NOW() - INTERVAL '24 hours'
"""

_CYCLES_SQL = """
    SELECT COUNT(DISTINCT run_id) AS n
    FROM platform.application_log
    WHERE event_type = 'INGESTION_START' AND recorded_at > $1
"""

# Our own controlled message: adapter_contract_drift: feed=<repr> ...
_FEED_RE = re.compile(r"feed=(['\"]?)([a-z0-9_]+)\1")


@dataclass
class DataSupervisorOutcome:
    opened: list[str] = field(default_factory=list)
    cleared: list[str] = field(default_factory=list)
    escalated: list[str] = field(default_factory=list)
    error: str | None = None


def _healspec_source(check_name: str) -> str | None:
    spec = spec_for(check_name)
    return spec.source if spec is not None else None


async def _red_sources(pool: Any) -> set[str]:
    """Namespaced still-red source keys, reusing the EXACT selfheal /
    auditheal red SQL + the contract-drift escalation surface."""
    out: set[str] = set()
    async with pool.acquire() as conn:
        for r in await conn.fetch(_VAL_RED_SQL):
            check = r["source"].removeprefix("validation.")
            src = _healspec_source(check)
            if src:
                out.add(f"validation:{src}")
        for r in await conn.fetch(_CT_RED_SQL):
            table = _ct_key(r["source"]).split("/")[0]
            out.add(f"cross_table:{table}")
        for r in await conn.fetch(_CONTRACT_RED_SQL):
            m = _FEED_RE.search(r["error"] or "")
            if m:
                out.add(f"contract:{m.group(2)}")
    return out


async def _emit(pool: Any, event_type: str, message: str,
                data: dict[str, Any], *, severity: str = "INFO") -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            _INSERT_SQL, _ENGINE_TAG, str(uuid.uuid4()), event_type,
            severity, message, json.dumps(data, default=str),
        )


async def _held_cycles(pool: Any, held_at: Any) -> int:
    async with pool.acquire() as conn:
        return int(await conn.fetchval(_CYCLES_SQL, held_at) or 0)


async def datasupervise(pool: Any, run_id: str) -> DataSupervisorOutcome:
    """One bounded pass. Crash-isolated: never raises into the caller
    (the Step always exits 0 — state-tracking, not a gate)."""
    out = DataSupervisorOutcome()
    try:
        red = await _red_sources(pool)

        # Open a hold for each newly-red source (idempotent).
        for source in sorted(red):
            if await current_source_hold(pool, source) is not None:
                continue
            hid = str(uuid.uuid4())
            await _emit(
                pool, HELD_EVENT, f"data source held: {source}",
                {"schema": SCHEMA_VERSION, "hold_id": hid,
                 "source": source, "reason": f"{source} red post Step-4/4c"},
                severity="WARNING")
            out.opened.append(source)

        # Inspect every currently-open hold for auto-clear / escalate.
        # The set of sources to examine = the holds we just opened plus
        # any pre-existing open hold whose source is NOT red now.
        candidates = set(red) | {s for s in red}
        # Pre-existing holds surface via the not-red path: a hold whose
        # source is absent from `red` is a recovery candidate. We probe
        # each (red ∪ previously-held). Previously-held discovery uses
        # the same per-source read (no table scan needed for the test
        # surface; production reads holds lazily per source touched).
        for source in sorted(set(red) | _recovery_probe_sources(red)):
            hold = await current_source_hold(pool, source)
            if hold is None:
                continue
            if source not in red:
                await _emit(
                    pool, CLEARED_EVENT, f"data source cleared: {source}",
                    {"schema": SCHEMA_VERSION, "hold_id": hold.hold_id,
                     "source": source, "clear_reason": "source green "
                     "after hold (autonomous auto-clear)"})
                await _emit(
                    pool, RECOVERED_EVENT,
                    f"data supervisor recovered: {source}",
                    {"schema": SCHEMA_VERSION, "source": source})
                out.cleared.append(source)
            else:
                n = await _held_cycles(pool, hold.held_at)
                if n >= _MAX_HELD_CYCLES:
                    await _emit(
                        pool, ESCALATED_EVENT,
                        f"data source escalated: {source}",
                        {"schema": SCHEMA_VERSION, "hold_id": hold.hold_id,
                         "source": source, "held_cycles": n,
                         "reason": "still red after "
                         f"{_MAX_HELD_CYCLES} held cycles"},
                        severity="ERROR")
                    out.escalated.append(source)
    except Exception as exc:  # noqa: BLE001 — crash-isolated by design
        logger.error("datasupervisor.error", error=str(exc),
                      exc_type=type(exc).__name__)
        out.error = str(exc)
    return out


def _recovery_probe_sources(red: set[str]) -> set[str]:
    """Sources to probe for auto-clear in addition to the red set.

    A hold clears when its source is NO LONGER red, so recovery
    candidates are by definition NOT in `red`. The production read of
    "which sources currently have an open hold" is the open-HELD set;
    `current_source_hold` is keyed per source, so the supervisor probes
    the union of (this cycle's red) ∪ (open-hold sources). Open-hold
    discovery is a single indexed query (below) — kept a function so
    the unit tests can drive it via the fake pool's scripted holds.
    """
    return set()
```

NOTE for the implementer: the open-hold discovery (sources with an open `DATA_SOURCE_HELD` and no later `DATA_SOURCE_CLEARED`) MUST be a real query so a recovered source actually auto-clears in production. Replace the `_recovery_probe_sources` stub body by having `datasupervise` first fetch open-hold sources via:
```sql
SELECT h.data->>'source' AS source
FROM platform.application_log h
LEFT JOIN platform.application_log c
  ON c.event_type = 'DATA_SOURCE_CLEARED'
 AND (c.data->>'hold_id') = (h.data->>'hold_id')
WHERE h.event_type = 'DATA_SOURCE_HELD' AND c.event_type IS NULL
```
and probe `sorted(red | open_hold_sources)`. The fake `_Conn.fetch` in the test returns `[]` for this SQL by default; add a branch so `test_autoclear_when_source_green_after_hold` (which seeds `holds`) drives auto-clear: have the fake return the seeded held source for that query. Keep `current_source_hold` as the authoritative per-source open/closed check. Implement this in Step 3 (do not ship the stub) — the tests above already assert auto-clear fires, so a stub fails them.

- [ ] **Step 4: Create `tpcore/datasupervisor/__main__.py`**

```python
"""Thin CLI — ``python -m tpcore.datasupervisor``. Wired as a Step in
run_data_operations.sh AFTER Step 4/4c. Exit code: 0 ALWAYS (this is
state-tracking, NOT a gate — it never decides DATA_OPERATIONS_COMPLETE).
Only a missing DSN returns 1 (parity with selfheal __main__)."""
from __future__ import annotations

import asyncio
import os
import sys
import uuid

import structlog

from tpcore.datasupervisor.supervisor import datasupervise
from tpcore.db import build_asyncpg_pool

logger = structlog.get_logger(__name__)


async def _amain() -> int:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("datasupervisor: DATABASE_URL not set", file=sys.stderr)
        return 1
    pool = await build_asyncpg_pool(db_url)
    try:
        out = await datasupervise(pool, str(uuid.uuid4()))
    finally:
        await pool.close()
    print("=" * 64)
    print(f"DATA-SUPERVISOR opened={out.opened} cleared={out.cleared} "
          f"escalated={out.escalated} error={out.error}")
    print("=" * 64)
    return 0  # NEVER gates — always 0 (a broken supervisor is inert)


def main() -> None:  # pragma: no cover — CLI shim
    raise SystemExit(asyncio.run(_amain()))


if __name__ == "__main__":  # pragma: no cover
    main()
```

- [ ] **Step 5:** Implement the real open-hold discovery query per the NOTE (not the stub), wire it into `datasupervise`, and add the matching branch to the test's `_Conn.fetch` so `test_autoclear_when_source_green_after_hold` exercises the production path. Run `python -m pytest tpcore/tests/test_datasupervisor.py -q` — all 6 pass. Fix implementation (never tests) until green.

- [ ] **Step 6:** `ruff check tpcore/datasupervisor/ tpcore/tests/test_datasupervisor.py` (clean; the single `# noqa: BLE001` on the crash-isolation catch is the ONLY allowed noqa — it matches the established crash-isolation pattern in ops/engine_supervisor.py / _run_stage; confirm no other noqa). `python -c "import tpcore.datasupervisor.__main__ as m; print('OK', hasattr(m,'main'))"` → `OK True`. `python -m pytest tpcore/tests/ tests/ -q --co 2>&1 | tail -1` → collection clean.

- [ ] **Step 7: Commit**

```bash
test "$(git branch --show-current)" = "feat/datasupervisor-p2" || { echo WRONG; exit 1; }
git add tpcore/datasupervisor/supervisor.py tpcore/datasupervisor/__main__.py tpcore/tests/test_datasupervisor.py
git commit -m "feat(datasupervisor): datasupervise pass + thin __main__ (dark)"
```
STOP. Report DONE (6/6, ruff confirming only the one documented BLE001 noqa, __main__ import, collection, commit SHA) or BLOCKED.

---

## Phase 3 — wire the thin Step (PR 3)

Branch: `feat/datasupervisor-p3` off fresh `main`.

### Task 3.1: new Step in `run_data_operations.sh` after Step 4c

**Files:** Modify `scripts/run_data_operations.sh`

- [ ] **Step 1: Read the Step 4c → emit region.** `grep -n "STEP 4c\|auditheal\|STEP 5\|STEP 6\|DATA_OPERATIONS_COMPLETE\|_log_event INGESTION" scripts/run_data_operations.sh` and `sed -n` the block from the end of Step 4c to the `DATA_OPERATIONS_COMPLETE` emit. Identify the exact line AFTER Step 4c completes and BEFORE Step 5/6.

- [ ] **Step 2: Insert the new Step** immediately after Step 4c's completion line and before the next Step. Use the existing `_log_event` + `DATABASE_URL` idiom already used by Step 4/4c in this file (match it exactly):

```bash
# Step 4d — DATA SUPERVISOR (per-source hold + autonomous auto-clear).
# Runs AFTER self-heal/audit so it sees the cycle's FINAL red set.
# State-tracking ONLY: it never gates — exit is always 0 and it does
# NOT affect whether DATA_OPERATIONS_COMPLETE is emitted (that remains
# exclusively the Step-4/4c 100%-green decision, unchanged). Opens a
# per-source DATA_SOURCE_HELD for still-red sources, autonomously
# auto-clears recovered ones, and escalates a chronically-stuck source.
echo ""
echo "▶ STEP 4d / 6  data supervisor (per-source hold + auto-clear)"
echo "────────────────────────────────────────────────────────────────────────"
_log_event INGESTION_START wrapper_datasupervisor
DATABASE_URL="${DATABASE_URL_IPV4:-$DATABASE_URL}" .venv/bin/python -m tpcore.datasupervisor || true
_log_event INGESTION_COMPLETE wrapper_datasupervisor
```
(The `|| true` is belt-and-braces — `__main__` already returns 0 except on a missing DSN; a supervisor hiccup must never break the cycle. Do NOT add an `exit` on its status. Renumber nothing else; "4d" keeps Step 5/6 labels intact. If the file's Step headers use a different "X / 6" pattern, match the surrounding convention.)

- [ ] **Step 3: Validate.** `bash -n scripts/run_data_operations.sh && echo "syntax OK"`. `grep -n "tpcore.datasupervisor\|STEP 4d\|wrapper_datasupervisor" scripts/run_data_operations.sh`. Confirm: the new Step is AFTER Step 4c and BEFORE the `DATA_OPERATIONS_COMPLETE` emit; the emit logic itself is UNCHANGED (`git diff` shows only the inserted block, no edit to the Step-4/4c gate or the Step-6 emit). `DATABASE_URL= .venv/bin/python -m tpcore.datasupervisor 2>&1 | head -1` → prints the `datasupervisor: DATABASE_URL not set` guard (proves the module is invocable, not an import traceback).

- [ ] **Step 4: Commit**

```bash
test "$(git branch --show-current)" = "feat/datasupervisor-p3" || { echo WRONG; exit 1; }
git add scripts/run_data_operations.sh
git commit -m "feat(datasupervisor): wire Step 4d after Step 4c (state-tracking, never gates)"
```
STOP. Report DONE (the inserted block + the surrounding lines proving placement after 4c / before emit, bash -n result, the DSN-guard output, commit SHA) or BLOCKED.

---

## Phase 4 — docs reconciliation (PR 4)

Branch: `docs/datasupervisor-p4` off fresh `main`.

### Task 4.1: reconcile docs

**Files:** `CLAUDE.md`, `docs/superpowers/specs/2026-05-17-data-supervisor-design.md`

- [ ] **Step 1: CLAUDE.md.** `grep -n "Step 4c\|self-heal\|DATA_OPERATIONS_COMPLETE\|Operator workflow\|Escalation" CLAUDE.md`. In the operator-workflow / data-ops description, add ONE factual sentence (match style, no emojis, surgical): a Step-4d data supervisor (`tpcore/datasupervisor`) runs after self-heal/audit each cycle and maintains per-source `DATA_SOURCE_HELD`/`DATA_SOURCE_CLEARED` with autonomous auto-clear + a bounded `DATA_SOURCE_ESCALATED` for a chronically-stuck source; it is **state-tracking only and does NOT affect the `DATA_OPERATIONS_COMPLETE` 100%-green gate** (the sacred invariant is unchanged); it is the data-native symmetric counterpart of the engine DA-1 supervisor.

- [ ] **Step 2: Spec status.** Set the spec `**Status:**` line to begin `**Status:** BUILT 2026-05-17` (keep lineage text); add a `**Build record:**` list mirroring `docs/superpowers/specs/2026-05-17-audit-driven-referential-remediation-design.md`: P1 state (PR #<p1>); P2 supervise+__main__ (PR #<p2>); P3 wire Step 4d (PR #<p3>); P4 docs (this). (The controller supplies the exact PR numbers at merge; if unknown when editing, write them as the branch names and the controller corrects on merge.)

- [ ] **Step 3: Verify scope + commit.**

```bash
source .venv/bin/activate && python -m pytest tpcore/tests/ tests/ -q --co 2>&1 | tail -1   # docs-only: collects clean
git diff --stat   # exactly CLAUDE.md + the spec
test "$(git branch --show-current)" = "docs/datasupervisor-p4" || { echo WRONG; exit 1; }
git add CLAUDE.md docs/superpowers/specs/2026-05-17-data-supervisor-design.md
git commit -m "docs: Data Supervisor — CLAUDE.md + spec BUILT reconciliation"
```
STOP. Report DONE.

---

## Self-Review

**1. Spec coverage:**
- Spec §2 architecture mapping → `state.py` (`current_source_hold` mirrors `supervisor_state.current_hold`, keyed `data->>'source'`); `supervisor.py` sole writer; `__main__` thin caller; no new gate. ✓
- Spec §3 mechanism: red detection reusing EXISTING predicates → `_red_sources` imports `tpcore.selfheal.orchestrator._RED_SQL`, `tpcore.auditheal.orchestrator._RED_SQL`+`_source_to_key`, contract via the controlled message regex; open/idempotent (via `current_source_hold`); strong auto-clear (source ∉ red AND open hold) ; bounded escalate at `_MAX_HELD_CYCLES`; crash-isolated try/except. ✓ (Auto-clear "strong predicate": the spec also requires green rows recorded after held_at — the implementer NOTE + Step 5 require the real open-hold query; the "source not red this cycle" is the reuse of the exact same red predicates so detector/gate cannot disagree. The post-held_at green-row refinement is satisfied because a source absent from the freshly-recomputed red set IS green now; the held_at ordering matters only to avoid clearing on a stale row — the red SQL already selects the LATEST row per source, so "not red now" == "latest row green", which is strictly after held_at by construction. Documented here so the reviewer checks this reasoning.)
- Spec §4 composition: Step 4d after 4c, exit 0 always, emit gate untouched → Phase 3. ✓
- Spec §5 non-goals: no re-heal (consumes red, never calls selfheal/auditheal/runner), no gate, no engine-lane file, no new table, no new daemon → enforced by file list (only `tpcore/datasupervisor/*` + one bash Step + docs). ✓
- Spec §6 phasing → 4 phases, 1 PR each. ✓
- Spec §7 resolved: namespaced source keys (`validation:`/`cross_table:`/`contract:`); `INGESTION_START` distinct-run_id cycle count; controlled-message feed regex; package mirrors auditheal. ✓

**2. Placeholder scan:** No TBD/TODO. The one risk: Phase-2 Step 3 ships a `_recovery_probe_sources` stub — Step 5 + the explicit NOTE require replacing it with the real open-hold query before the phase is done (the tests fail with the stub, so it cannot merge stubbed). Called out, not a hidden placeholder.

**3. Type consistency:** `current_source_hold(pool, source) -> SourceHoldState|None`; `SourceHoldState(hold_id, reason, held_at)`; event constants `DATA_SOURCE_HELD/CLEARED/ESCALATED`, `DATA_SUPERVISOR_RECOVERED`; `datasupervise(pool, run_id) -> DataSupervisorOutcome(opened, cleared, escalated, error)`; `_MAX_HELD_CYCLES=3`; `_ENGINE_TAG="datasupervisor"` — consistent across state.py, supervisor.py, __main__.py, and all tests. The `_INSERT_SQL` arg order `(engine, run_id, event_type, severity, message, data)` matches the `_emit` positional call and the verified `data_repair_service._INSERT_SQL`.

(Carried to execution: Phase-2 Step 3's open-hold discovery query is the load-bearing correctness piece — the spec-compliance reviewer MUST verify it is implemented (not the stub) and that `test_autoclear_when_source_green_after_hold` exercises the real query path, since a stubbed recovery probe would make auto-clear — the feature's whole point — silently dead.)
