# Sub-project B — Event-Driven Engine Dispatch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the unconditional bash engine loop with a Python dispatcher that gates every engine through `engine_profile.should_fire()`, emits `ENGINE_DATA_REQUEST` (async hand-off, never in-process self-heal) when an engine is data-blocked, and re-dispatches on `DATA_REPAIR_COMPLETE`.

**Architecture:** New `ops/engine_dispatch.py` (Python); `scripts/run_all_engines.sh` becomes a thin caller; `ops/engine_service.py` gains a second trigger event; per-scheduler cadence math deleted (engine_profile is the sole cadence authority); `capital_gate` gains a non-raising `failing_sources_for_engine`.

**Tech Stack:** Python 3.11, asyncpg, pytest (`asyncio_mode=auto`), `tpcore.engine_profile`, `tpcore.quality.validation.capital_gate`, `tpcore.selfheal.registry` (vocabulary only), `platform.application_log`.

---

## File Structure

- **Create:** `ops/engine_dispatch.py` — the dispatcher (roster loop, should_fire gate, request emit, terminal-event/timeout handling, crashed-STARTUP guard). One responsibility: decide+invoke per engine. ~220 lines.
- **Create:** `scripts/tests/test_engine_dispatch.py` — all dispatcher unit tests (fake pool, mocked should_fire / failing_sources_for_engine / subprocess).
- **Modify:** `tpcore/quality/validation/capital_gate.py` — add `failing_sources_for_engine` (non-raising) + factor the failing-source predicate out of `_evaluate`.
- **Modify:** `tpcore/quality/validation/tests/test_capital_gate.py` — tests for the new function.
- **Modify:** `ops/engine_service.py` — add `DATA_REPAIR_COMPLETE` as a second trigger event type.
- **Modify:** `scripts/tests/test_engine_service.py` (or create if absent) — multi-trigger test.
- **Modify:** `scripts/run_all_engines.sh` — strip the bash `for`-loop + the `LATEST_VALIDATION` global-gate preamble; call `python -m ops.engine_dispatch "$@"`.
- **Modify:** `momentum/scheduler.py` — delete `is_rebalance_day` cadence gate; keep `--force-rebalance`.
- **Modify:** `sentinel/scheduler.py` — delete `is_trading_day` cadence gate; add `--force`.
- **Modify:** `momentum/tests/...`, `sentinel/tests/...` — adjust tests for the removed cadence gate.

Verified real APIs (file:line, this worktree base = origin/main incl. PR #4):
- `tpcore/engine_profile.py:123` `async should_fire(engine, now, pool) -> FireDecision`; `FireDecision` (`:100`) = `frozen(fire:bool, reason:str, checks:dict[str,bool])`; `checks` keys: `profiled, cadence, market_closed, data_ready, not_already_run`.
- `tpcore/quality/validation/capital_gate.py`: `ENGINE_TABLES` (`:60-67`); `assert_passed_for_engine` (`:148-164`) → `_required_sources(engine)` (`:78-99`) + `_evaluate(rows, required, max_age_days)` (`:102-136`, raises `ValidationStaleError`/`ValidationFailedError`); `_fetch_validation_rows(pool)` (`:167-175`, SQL: `SELECT source,timestamp,stale FROM platform.data_quality_log WHERE source LIKE 'validation.%' ORDER BY timestamp DESC`). `ENGINE_TABLES` values are `HealSpec.source` names (`tpcore/selfheal/registry.py`).
- `tpcore/logging/db_handler.py:35` `_INSERT_SQL = "INSERT INTO platform.application_log (engine, run_id, event_type, severity, message, data) VALUES ($1,$2,$3,$4,$5,$6::jsonb)"`; `recorded_at` is DB-default `now()`.
- `ops/engine_service.py:43-46` constants; `_find_new_trigger` (`:49-67`) SQL `WHERE event_type=$1 AND recorded_at>$2 ORDER BY recorded_at DESC LIMIT 1`; `_run_engine_sweep` (`:70-77`); `_main_loop` (`:80-107`).
- `scripts/run_all_engines.sh:73-80` for-loop; `:36-66` LATEST_VALIDATION preamble.
- `momentum/scheduler.py:273-290` `is_rebalance_day` gate; `:549-556` `--force-rebalance`; `:560-583` amain. `sentinel/scheduler.py:144-150` `is_trading_day` gate; `:370-376` argparse (no `--force`); `:379-395` amain.
- Tests: `pyproject.toml:80` `asyncio_mode=auto`; ops daemons tested under `scripts/tests/`; fake-pool/`_FakeConn` pattern in `tpcore/tests/test_engine_profile.py`; `_FakeDBLog` pattern in `tpcore/tests/test_ingestion_engine.py`.

---

### Task 1: `capital_gate.failing_sources_for_engine` (non-raising)

**Files:**
- Modify: `tpcore/quality/validation/capital_gate.py`
- Test: `tpcore/quality/validation/tests/test_capital_gate.py`

- [ ] **Step 1: Read the existing internals**

Read `capital_gate.py:78-136` — `_required_sources(engine)` (returns the set of `validation.<check>` keys the engine needs) and `_evaluate(rows, required, max_age_days)` (the predicate that decides a required source is stale/failed/missing and raises). You will reuse `_fetch_validation_rows`, `_required_sources`, and the SAME staleness/failure predicate `_evaluate` applies — without raising.

- [ ] **Step 2: Write the failing test**

```python
# in tpcore/quality/validation/tests/test_capital_gate.py
import pytest
from tpcore.quality.validation.capital_gate import failing_sources_for_engine

class _Conn:
    def __init__(self, rows): self._rows = rows
    async def fetch(self, *_a, **_k): return self._rows
class _Pool:
    def __init__(self, rows): self._rows = rows
    def acquire(self):
        rows = self._rows
        class _Cm:
            async def __aenter__(self_): return _Conn(rows)
            async def __aexit__(self_, *a): return False
        return _Cm()

async def test_failing_sources_returns_healspec_source_names_for_red_required_check():
    # reversion needs prices_daily + fundamentals_quarterly. Make the
    # prices_daily validation check stale.
    import datetime as dt
    fresh = dt.datetime.now(dt.UTC)
    rows = [
        {"source": "validation.prices_daily_freshness", "timestamp": fresh, "stale": True},
        {"source": "validation.fundamentals_integrity", "timestamp": fresh, "stale": False},
    ]
    bad = await failing_sources_for_engine(_Pool(rows), "reversion")
    assert "prices_daily" in bad
    assert "fundamentals_quarterly" not in bad

async def test_failing_sources_empty_when_all_green():
    import datetime as dt
    fresh = dt.datetime.now(dt.UTC)
    rows = [
        {"source": "validation.prices_daily_freshness", "timestamp": fresh, "stale": False},
        {"source": "validation.fundamentals_integrity", "timestamp": fresh, "stale": False},
    ]
    assert await failing_sources_for_engine(_Pool(rows), "reversion") == []

async def test_failing_sources_unknown_engine_returns_empty():
    assert await failing_sources_for_engine(_Pool([]), "does_not_exist") == []
```
(The exact validation-check source strings — e.g. `validation.prices_daily_freshness` vs `validation.prices_daily_completeness` — must match what `_required_sources("reversion")` actually maps `prices_daily` to. In Step 1 read `_required_sources` and the `HEAL_SPECS` linkage and use the REAL check names in the test rows. Adjust the row `source` strings to the real ones; the assertion is on the returned **HealSpec.source** names (`prices_daily`, etc.), which is the locked vocabulary.)

- [ ] **Step 3: Run, verify fail**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q tpcore/quality/validation/tests/test_capital_gate.py -k failing_sources`
Expected: FAIL — `ImportError: cannot import name 'failing_sources_for_engine'`.

- [ ] **Step 4: Implement (reuse existing helpers; do NOT duplicate _evaluate)**

Add to `capital_gate.py`. Factor the per-row stale/failed predicate `_evaluate` uses into a module helper `_is_red(row, max_age_days) -> bool` (extract the exact condition from `_evaluate:102-136`; replace the inline condition in `_evaluate` with a call to `_is_red` so there is ONE predicate — DRY, no behavior change to `_evaluate`). Then:

```python
async def failing_sources_for_engine(
    pool: asyncpg.Pool, engine: str, *, max_age_days: int = 7,
) -> list[str]:
    """Non-raising companion to assert_passed_for_engine.

    Returns the HealSpec.source names of the engine's required data
    sources whose latest validation row is missing/stale/failed.
    [] when the engine is unprofiled or all its sources are green.
    Vocabulary = HealSpec.source (the ENGINE_TABLES / selfheal-registry
    namespace), the locked inter-lane contract vocabulary.
    """
    tables = ENGINE_TABLES.get(engine)
    if not tables:
        return []
    rows = await _fetch_validation_rows(pool)
    latest: dict[str, dict] = {}
    for r in rows:                       # rows are ORDER BY timestamp DESC
        latest.setdefault(r["source"], r)
    bad: set[str] = set()
    for check, spec in HEAL_SPECS.items():
        if spec.source not in tables:
            continue
        key = f"validation.{check}"
        row = latest.get(key)
        if row is None or _is_red(row, max_age_days):
            bad.add(spec.source)
    return sorted(bad)
```
(Confirm against Step 1: the `HEAL_SPECS`/`f"validation.{check}"` mapping mirrors exactly what `_required_sources` does — reuse the same iteration so the two can never diverge. If `_required_sources` already returns `{(source, validation.key)}` pairs, call it instead of re-deriving. Match the real internal shape you read in Step 1; the tests pin behavior.)

- [ ] **Step 5: Run, verify pass + no regression**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q tpcore/quality/validation/tests/test_capital_gate.py -v`
Expected: PASS (new + all existing capital_gate tests — `_evaluate` behavior unchanged via `_is_red` extraction).

- [ ] **Step 6: ruff + commit**

```bash
/Users/michael/short-term-trading-engine/.venv/bin/python -m ruff check tpcore/quality/validation/capital_gate.py tpcore/quality/validation/tests/test_capital_gate.py
git add tpcore/quality/validation/capital_gate.py tpcore/quality/validation/tests/test_capital_gate.py
git commit -m "feat(capital_gate): non-raising failing_sources_for_engine (HealSpec.source vocab)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: `ops/engine_dispatch.py` — roster loop, should_fire gate, fire→invoke

**Files:**
- Create: `ops/engine_dispatch.py`
- Test: `scripts/tests/test_engine_dispatch.py`

- [ ] **Step 1: Write the failing test**

```python
# scripts/tests/test_engine_dispatch.py
import contextlib
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

from tpcore.engine_profile import FireDecision
from ops.engine_dispatch import dispatch_once, ROSTER


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
```

- [ ] **Step 2: Run, verify fail**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q scripts/tests/test_engine_dispatch.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'ops.engine_dispatch'`.

- [ ] **Step 3: Implement the skeleton**

```python
# ops/engine_dispatch.py
"""Event-driven engine dispatcher (Sub-project B).

Replaces the unconditional bash engine loop. Per engine: consult
``tpcore.engine_profile.should_fire``. Fire → invoke that engine's
scheduler. Data-blocked → emit ENGINE_DATA_REQUEST and skip (async
hand-off to the data lane; NEVER self-heal in-process — that would
couple trade latency to data-repair and contend on the pooler).
See docs/superpowers/specs/2026-05-17-sub-project-b-event-driven-dispatch-design.md.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import UTC, datetime

import structlog

from tpcore.db import build_asyncpg_pool
from tpcore.engine_profile import should_fire

logger = structlog.get_logger(__name__)

ROSTER: tuple[str, ...] = ("reversion", "vector", "momentum", "sentinel")


async def _invoke_scheduler(engine: str) -> int:
    """Run one engine's scheduler as an isolated subprocess.

    Per-engine crash isolation: a non-zero exit is logged and the
    sweep continues to the next engine (mirrors the old bash loop's
    ``|| continue``). Args (e.g. --force) are NOT forwarded — the
    dispatcher is the gate; manual --force is a direct-invocation path.
    """
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", f"{engine}.scheduler", cwd=repo,
    )
    rc = await proc.wait()
    logger.info("engine_dispatch.scheduler_done", engine=engine, returncode=rc)
    return rc


async def dispatch_once(pool, now: datetime) -> None:
    for engine in ROSTER:
        decision = await should_fire(engine, now, pool)
        if decision.fire:
            logger.info("engine_dispatch.dispatched", engine=engine)
            await _invoke_scheduler(engine)
        else:
            logger.info(
                "engine_dispatch.skipped", engine=engine,
                reason=decision.reason,
                data_ready=decision.checks.get("data_ready"),
            )


async def _amain() -> int:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL not set", file=sys.stderr)
        return 1
    pool = await build_asyncpg_pool(db_url)
    try:
        await dispatch_once(pool, now=datetime.now(UTC))
        return 0
    finally:
        await pool.close()


def main() -> None:  # pragma: no cover — CLI shim
    raise SystemExit(asyncio.run(_amain()))


if __name__ == "__main__":  # pragma: no cover
    main()
```

- [ ] **Step 4: Run, verify pass**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q scripts/tests/test_engine_dispatch.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: ruff + check_imports + commit**

```bash
/Users/michael/short-term-trading-engine/.venv/bin/python -m ruff check ops/engine_dispatch.py scripts/tests/test_engine_dispatch.py
/Users/michael/short-term-trading-engine/.venv/bin/python tpcore/scripts/check_imports.py ops
git add ops/engine_dispatch.py scripts/tests/test_engine_dispatch.py
git commit -m "feat(engine_dispatch): roster loop gated by engine_profile.should_fire

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: data-not-ready → emit `ENGINE_DATA_REQUEST` (async hand-off, dedup, never heal)

**Files:**
- Modify: `ops/engine_dispatch.py`
- Test: `scripts/tests/test_engine_dispatch.py`

- [ ] **Step 1: Write the failing test**

```python
import json, uuid

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
         patch("ops.engine_dispatch._invoke_scheduler", new=AsyncMock()) as inv, \
         patch("ops.engine_dispatch.run_self_heal", create=True) as heal:
        await dispatch_once(_P(), now=datetime(2026,5,5,21,30,tzinfo=UTC))
    inv.assert_not_called()                       # never invoked
    heal.assert_not_called()                      # NEVER heals in-process
    reqs = [a for s, a in inserts if "application_log" in s
            and "ENGINE_DATA_REQUEST" in (a if isinstance(a, tuple) else ())]
    # exactly one ENGINE_DATA_REQUEST insert with schema:1 + uuid request_id
    payloads = [a for s, a in inserts if "INSERT INTO platform.application_log" in s]
    assert len(payloads) == 1
    data = json.loads(payloads[0][-1])
    assert data["schema"] == 1 and data["engine"] == "reversion"
    assert data["sources"] == ["prices_daily"]
    uuid.UUID(data["request_id"])                 # valid uuid


async def test_open_request_is_not_re_emitted():
    nofire = FireDecision(False, "data not ready", {"data_ready": False})
    class _C:
        async def fetchval(self, *_a, **_k): return 1   # an OPEN request exists
        async def fetch(self, *_a, **_k): return []
        async def execute(self, *_a, **_k): raise AssertionError("must not insert")
    class _P:
        @contextlib.asynccontextmanager
        async def acquire(self): yield _C()
    with patch("ops.engine_dispatch.should_fire", AsyncMock(return_value=nofire)), \
         patch("ops.engine_dispatch.failing_sources_for_engine", new=AsyncMock(return_value=["prices_daily"])), \
         patch("ops.engine_dispatch._invoke_scheduler", new=AsyncMock()):
        await dispatch_once(_P(), now=datetime(2026,5,5,21,30,tzinfo=UTC))
```

- [ ] **Step 2: Run, verify fail**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q scripts/tests/test_engine_dispatch.py -k data_blocked`
Expected: FAIL — `failing_sources_for_engine` not imported / no request emitted.

- [ ] **Step 3: Implement**

Add imports to `ops/engine_dispatch.py`:
```python
import json
import uuid
from tpcore.quality.validation.capital_gate import failing_sources_for_engine
```
Add the open-request probe + emit (note: `run_self_heal` is deliberately NOT imported — the dispatcher must never heal; the test patches it with `create=True` only to assert absence):
```python
_REQUEST_EVENT = "ENGINE_DATA_REQUEST"
_TERMINAL_EVENTS = ("DATA_REPAIR_COMPLETE", "DATA_REPAIR_ESCALATED")


async def _has_open_request(conn, engine: str, window_start: datetime) -> bool:
    """True if an ENGINE_DATA_REQUEST for this engine in the current
    cadence window has no terminal event yet."""
    row = await conn.fetchval(
        """
        SELECT 1 FROM platform.application_log r
        WHERE r.event_type = $1 AND r.engine = $2 AND r.recorded_at >= $3
          AND NOT EXISTS (
            SELECT 1 FROM platform.application_log t
            WHERE t.event_type = ANY($4::text[])
              AND (t.data->>'request_id') = (r.data->>'request_id'))
        LIMIT 1
        """,
        _REQUEST_EVENT, engine, window_start, list(_TERMINAL_EVENTS),
    )
    return row is not None


async def _emit_data_request(conn, engine: str, sources: list[str], reason: str) -> str:
    request_id = str(uuid.uuid4())
    payload = json.dumps({
        "schema": 1, "request_id": request_id,
        "engine": engine, "sources": sources, "reason": reason,
    })
    await conn.execute(
        """
        INSERT INTO platform.application_log
            (engine, run_id, event_type, severity, message, data)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb)
        """,
        engine, uuid.uuid4(), _REQUEST_EVENT, "WARNING",
        f"{engine} data-blocked: {reason}", payload,
    )
    logger.warning("engine_dispatch.data_request", engine=engine,
                    request_id=request_id, sources=sources)
    return request_id
```
In `dispatch_once`, replace the `else` branch with:
```python
        elif decision.checks.get("data_ready") is False:
            from tpcore.engine_profile import _cadence_window_start, profile_for
            prof = profile_for(engine)
            window_start = _cadence_window_start(prof, now) if prof else now
            async with pool.acquire() as conn:
                if await _has_open_request(conn, engine, window_start):
                    logger.info("engine_dispatch.request_open", engine=engine)
                    continue
                sources = await failing_sources_for_engine(pool, engine)
                await _emit_data_request(conn, engine, sources, decision.reason)
        else:
            logger.info("engine_dispatch.skipped", engine=engine,
                        reason=decision.reason)
```
(`_cadence_window_start`/`profile_for` are existing public-ish helpers in `tpcore.engine_profile` from Sub-project A; the window keys dedup to the cadence cycle. If `_cadence_window_start` is private and you prefer not to import it, add a thin public `cadence_window_start(engine, now)` to `tpcore/engine_profile.py` in this task and use that — keep one cadence-window authority.)

- [ ] **Step 4: Run, verify pass**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q scripts/tests/test_engine_dispatch.py -v`
Expected: PASS.

- [ ] **Step 5: ruff + commit**

```bash
/Users/michael/short-term-trading-engine/.venv/bin/python -m ruff check ops/engine_dispatch.py scripts/tests/test_engine_dispatch.py
git add ops/engine_dispatch.py scripts/tests/test_engine_dispatch.py tpcore/engine_profile.py
git commit -m "feat(engine_dispatch): emit ENGINE_DATA_REQUEST on data-block (async hand-off, dedup, never heal)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: crashed-STARTUP guard (re-fire when STARTUP-without-completion is stale)

**Files:**
- Modify: `ops/engine_dispatch.py`
- Test: `scripts/tests/test_engine_dispatch.py`

- [ ] **Step 1: Write the failing test**

```python
async def test_stale_startup_without_completion_is_refired():
    # should_fire says "already ran this cycle" (STARTUP exists) but
    # there is no completion and the STARTUP is > threshold old → the
    # dispatcher overrides and fires.
    already = FireDecision(False, "already ran this cycle", {"data_ready": True, "not_already_run": False})
    class _C:
        async def fetchrow(self, *_a, **_k):
            # newest STARTUP 3h ago, no SCAN_COMPLETE/SHUTDOWN since
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
    assert inv.await_args_list and inv.await_args_list[0].args[0] in ROSTER  # re-fired

async def test_recent_startup_without_completion_is_not_refired():
    already = FireDecision(False, "already ran this cycle", {"data_ready": True, "not_already_run": False})
    class _C:
        async def fetchrow(self,*_a,**_k):
            return {"started_at": datetime(2026,5,5,21,20,tzinfo=UTC), "completed": False}  # 10 min ago
        async def fetchval(self,*_a,**_k): return None
        async def fetch(self,*_a,**_k): return []
        async def execute(self,*_a,**_k): return None
    class _P:
        @contextlib.asynccontextmanager
        async def acquire(self): yield _C()
    with patch("ops.engine_dispatch.should_fire", AsyncMock(return_value=already)), \
         patch("ops.engine_dispatch._invoke_scheduler", new=AsyncMock()) as inv:
        await dispatch_once(_P(), now=datetime(2026,5,5,21,30,tzinfo=UTC))
    inv.assert_not_called()  # recent in-flight run — do NOT double-fire
```

- [ ] **Step 2: Run, verify fail** — `pytest -k startup` → FAIL (no guard).

- [ ] **Step 3: Implement**

Add to `ops/engine_dispatch.py`:
```python
import os as _os
_STALE_STARTUP_SECONDS = int(_os.environ.get("ENGINE_DISPATCH_STALE_STARTUP_SECONDS", "7200"))  # 2h default (spec §6)


async def _crashed_startup_refire(conn, engine: str, now: datetime,
                                  window_start: datetime) -> bool:
    """True iff this cycle has a STARTUP with NO completion and the
    STARTUP is older than the stale threshold (a crashed pre-trade run
    — should_fire's STARTUP-based 'already ran' would wrongly skip it,
    potentially for a whole month for momentum)."""
    row = await conn.fetchrow(
        """
        SELECT
          max(recorded_at) FILTER (WHERE event_type = 'STARTUP')        AS started_at,
          bool_or(event_type IN ('SCAN_COMPLETE','SHUTDOWN'))           AS completed
        FROM platform.application_log
        WHERE engine = $1 AND recorded_at >= $2
        """,
        engine, window_start,
    )
    if not row or row["started_at"] is None or row["completed"]:
        return False
    age = (now - row["started_at"]).total_seconds()
    return age >= _STALE_STARTUP_SECONDS
```
In `dispatch_once`, when `not decision.fire and decision.reason == "already ran this cycle"`, before skipping: compute `window_start` (as in Task 3) and if `await _crashed_startup_refire(conn, engine, now, window_start)` → log `engine_dispatch.crashed_startup_refire` and `await _invoke_scheduler(engine)` then `continue`. Place this branch BEFORE the generic skip and AFTER the data-blocked branch. (Read the real `application_log` event_type literals; `SCAN_COMPLETE`/`SHUTDOWN` are the canonical completion events per `db_handler.py`. If a SHUTDOWN with non-zero exit_code should NOT count as completed, refine the SQL to check `data` — confirm db_handler's shutdown payload shape and pin it; default: any SHUTDOWN/SCAN_COMPLETE counts as "completed" — a non-zero exit is a real failure that should escalate, not silently re-fire.)

- [ ] **Step 4: Run, verify pass** — `pytest -q scripts/tests/test_engine_dispatch.py -v` → PASS.
- [ ] **Step 5: ruff + commit**

```bash
/Users/michael/short-term-trading-engine/.venv/bin/python -m ruff check ops/engine_dispatch.py
git add ops/engine_dispatch.py scripts/tests/test_engine_dispatch.py
git commit -m "feat(engine_dispatch): crashed-STARTUP guard — re-fire stale started-but-incomplete runs

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: terminal-event handling — green re-fire / escalated / timeout skip

**Files:**
- Modify: `ops/engine_dispatch.py`
- Test: `scripts/tests/test_engine_dispatch.py`

- [ ] **Step 1: Write the failing test**

```python
_REQ_TS = datetime(2026,5,5,21,0,tzinfo=UTC)

def _pool_with_request(terminal=None, req_ts=_REQ_TS):
    class _C:
        async def fetchrow(self, sql, *a):
            if "ENGINE_DATA_REQUEST" in sql:
                return {"request_id": "rid-1", "recorded_at": req_ts,
                        "terminal_type": (terminal or {}).get("t"),
                        "green": (terminal or {}).get("green")}
            return None
        async def fetchval(self,*_a,**_k): return None
        async def fetch(self,*_a,**_k): return []
        async def execute(self,*_a,**_k): return None
    class _P:
        @contextlib.asynccontextmanager
        async def acquire(self): yield _C()
    return _P()

async def test_green_terminal_refires_engine():
    nofire = FireDecision(False, "data not ready", {"data_ready": False})
    fire = FireDecision(True, "ready", {"data_ready": True})
    sf = AsyncMock(side_effect=[nofire, fire])  # 1st gate blocked; re-eval green
    with patch("ops.engine_dispatch.should_fire", sf), \
         patch("ops.engine_dispatch._open_request_state",
               new=AsyncMock(return_value={"request_id":"rid-1","terminal":"DATA_REPAIR_COMPLETE","green":True,"req_ts":_REQ_TS})), \
         patch("ops.engine_dispatch._invoke_scheduler", new=AsyncMock()) as inv:
        await dispatch_once(_pool_with_request(), now=datetime(2026,5,5,21,30,tzinfo=UTC))
    assert any(c.args[0]=="reversion" for c in inv.await_args_list)

async def test_escalated_terminal_skips_and_alarms(caplog):
    nofire = FireDecision(False, "data not ready", {"data_ready": False})
    with patch("ops.engine_dispatch.should_fire", AsyncMock(return_value=nofire)), \
         patch("ops.engine_dispatch._open_request_state",
               new=AsyncMock(return_value={"request_id":"rid-1","terminal":"DATA_REPAIR_ESCALATED","green":False,"req_ts":_REQ_TS})), \
         patch("ops.engine_dispatch._invoke_scheduler", new=AsyncMock()) as inv:
        await dispatch_once(_pool_with_request(), now=datetime(2026,5,5,21,30,tzinfo=UTC))
    inv.assert_not_called()

async def test_timeout_no_terminal_skips_and_alarms():
    nofire = FireDecision(False, "data not ready", {"data_ready": False})
    old = datetime(2026,5,5,18,0,tzinfo=UTC)  # request 3.5h old, no terminal
    with patch("ops.engine_dispatch.should_fire", AsyncMock(return_value=nofire)), \
         patch("ops.engine_dispatch._open_request_state",
               new=AsyncMock(return_value={"request_id":"rid-1","terminal":None,"green":None,"req_ts":old})), \
         patch("ops.engine_dispatch._invoke_scheduler", new=AsyncMock()) as inv:
        await dispatch_once(_pool_with_request(req_ts=old), now=datetime(2026,5,5,21,30,tzinfo=UTC))
    inv.assert_not_called()
```

- [ ] **Step 2: Run, verify fail** — `_open_request_state` not defined.

- [ ] **Step 3: Implement**

Add to `ops/engine_dispatch.py`:
```python
_NO_TERMINAL_TIMEOUT_SECONDS = int(
    _os.environ.get("ENGINE_DISPATCH_REQUEST_TIMEOUT_SECONDS", "5400"))  # 90 min (spec §6)


async def _open_request_state(conn, engine: str, window_start: datetime) -> dict | None:
    """Latest ENGINE_DATA_REQUEST for engine in this cadence window +
    its terminal event (if any). None if no request this window."""
    return await conn.fetchrow(
        """
        SELECT r.data->>'request_id' AS request_id, r.recorded_at AS req_ts,
               t.event_type AS terminal,
               (t.data->>'green')::bool AS green
        FROM platform.application_log r
        LEFT JOIN platform.application_log t
          ON t.event_type = ANY($3::text[])
         AND (t.data->>'request_id') = (r.data->>'request_id')
        WHERE r.event_type = $1 AND r.engine = $2 AND r.recorded_at >= $4
        ORDER BY r.recorded_at DESC LIMIT 1
        """,
        _REQUEST_EVENT, engine, list(_TERMINAL_EVENTS), window_start,
    )
```
Restructure `dispatch_once`'s data-blocked branch: when `decision.checks.get("data_ready") is False`, first `state = await _open_request_state(conn, engine, window_start)`:
- `state is None` → no request yet → emit (Task 3 path).
- `state["terminal"] == "DATA_REPAIR_COMPLETE" and state["green"]` → re-evaluate `await should_fire(...)`; if it now fires → `await _invoke_scheduler(engine)`; else skip (logged).
- `state["terminal"] == "DATA_REPAIR_ESCALATED"` OR (`terminal == "DATA_REPAIR_COMPLETE"` and not green) → `logger.error("engine_dispatch.data_unrecovered", engine=engine, request_id=...)` (operator alarm), skip.
- `state["terminal"] is None` and `(now - state["req_ts"]).total_seconds() >= _NO_TERMINAL_TIMEOUT_SECONDS` → `logger.error("engine_dispatch.data_request_timeout", engine=engine)` (alarm), skip.
- `state["terminal"] is None` and not timed out → request still open, skip silently (`engine_dispatch.request_open`).

(The Task-3 `_has_open_request` is now subsumed by `_open_request_state` — replace its use; keep ONE probe. Update Task-3 tests if they referenced `_has_open_request` directly.)

- [ ] **Step 4: Run, verify pass** — full `scripts/tests/test_engine_dispatch.py` green.
- [ ] **Step 5: ruff + commit**

```bash
/Users/michael/short-term-trading-engine/.venv/bin/python -m ruff check ops/engine_dispatch.py
git add ops/engine_dispatch.py scripts/tests/test_engine_dispatch.py
git commit -m "feat(engine_dispatch): terminal-event handling — green re-fire / escalated+timeout skip+alarm

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: `scripts/run_all_engines.sh` → thin caller

**Files:**
- Modify: `scripts/run_all_engines.sh`

- [ ] **Step 1: Read the current script** (lines 1-80). Note the env preamble (`DATABASE_URL_IPV4`), the `LATEST_VALIDATION` global-gate block (36-66), the for-loop (73-80).

- [ ] **Step 2: Replace**

Keep the shebang + `set -euo pipefail` + repo-root `cd` + the `DATABASE_URL_IPV4` env export. **Delete** the `LATEST_VALIDATION` global-gate block (lines ~36-66) — per-engine readiness is now `should_fire`/`capital_gate`'s job inside the dispatcher; a global bash gate would re-impose the all-or-nothing behavior `capital_gate` was explicitly built to refine. **Delete** the `for engine` loop (73-80). Replace the body with:
```bash
echo "▶ engine dispatch (profile-gated)"
DATABASE_URL="$DATABASE_URL_IPV4" exec .venv/bin/python -m ops.engine_dispatch "$@"
```
(`exec` so the script's exit code is the dispatcher's; `"$@"` preserved for forward-compat though the dispatcher currently ignores args — manual `--force` is the direct `python -m X.scheduler` path per spec §7.)

- [ ] **Step 3: Verify**

Run: `bash -n scripts/run_all_engines.sh` → no syntax error.
Run: `grep -c 'for engine in' scripts/run_all_engines.sh` → `0`.
Run: `grep -c 'ops.engine_dispatch' scripts/run_all_engines.sh` → `1`.

- [ ] **Step 4: Commit**

```bash
git add scripts/run_all_engines.sh
git commit -m "refactor(run_all_engines): thin caller of ops.engine_dispatch (gating moved to Python)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: `ops/engine_service.py` — add `DATA_REPAIR_COMPLETE` trigger

**Files:**
- Modify: `ops/engine_service.py`
- Test: `scripts/tests/test_engine_service.py` (create if absent)

- [ ] **Step 1: Write the failing test**

```python
# scripts/tests/test_engine_service.py
import contextlib
from datetime import UTC, datetime, timedelta
from ops.engine_service import _find_new_trigger, TRIGGER_EVENT_TYPES

class _Conn:
    def __init__(self, row): self._row = row
    async def fetchrow(self, sql, *args):
        # assert the SQL filters on a SET of event types
        assert "ANY(" in sql or "= ANY" in sql, sql
        return self._row
class _Pool:
    def __init__(self, row):
        self._row = row
    @contextlib.asynccontextmanager
    async def acquire(self): yield _Conn(self._row)

async def test_data_repair_complete_is_a_trigger():
    assert "DATA_OPERATIONS_COMPLETE" in TRIGGER_EVENT_TYPES
    assert "DATA_REPAIR_COMPLETE" in TRIGGER_EVENT_TYPES
    ts = datetime.now(UTC)
    got = await _find_new_trigger(_Pool({"recorded_at": ts}),
                                  ts - timedelta(hours=1))
    assert got == ts
```

- [ ] **Step 2: Run, verify fail** — `TRIGGER_EVENT_TYPES` undefined / SQL not ANY().

- [ ] **Step 3: Implement (minimal change)**

In `ops/engine_service.py`: replace `TRIGGER_EVENT_TYPE = "DATA_OPERATIONS_COMPLETE"` with
```python
TRIGGER_EVENT_TYPES: tuple[str, ...] = ("DATA_OPERATIONS_COMPLETE", "DATA_REPAIR_COMPLETE")
```
Change `_find_new_trigger`'s SQL `WHERE event_type = $1` → `WHERE event_type = ANY($1::text[])` and pass `list(TRIGGER_EVENT_TYPES)` instead of `TRIGGER_EVENT_TYPE`. The cursor logic and `_run_engine_sweep` are unchanged — any newer trigger of either type fires the (now profile-gated, idempotent) sweep; a `DATA_REPAIR_COMPLETE` re-runs the sweep and `should_fire` lets only the now-unblocked engine through. (Optional: filter `DATA_REPAIR_COMPLETE` to `data->>'green' = 'true'` in the SQL so a non-green terminal doesn't trigger a no-op sweep — include `AND (event_type <> 'DATA_REPAIR_COMPLETE' OR (data->>'green')::bool)`; add a test asserting a green=false DATA_REPAIR_COMPLETE does NOT trigger.)

- [ ] **Step 4: Run, verify pass** — new test + any existing engine_service test green.
- [ ] **Step 5: ruff + check_imports + commit**

```bash
/Users/michael/short-term-trading-engine/.venv/bin/python -m ruff check ops/engine_service.py scripts/tests/test_engine_service.py
/Users/michael/short-term-trading-engine/.venv/bin/python tpcore/scripts/check_imports.py ops
git add ops/engine_service.py scripts/tests/test_engine_service.py
git commit -m "feat(engine_service): DATA_REPAIR_COMPLETE re-dispatch trigger

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: delete per-scheduler cadence gates; keep/add `--force`

**Files:**
- Modify: `momentum/scheduler.py`, `sentinel/scheduler.py`
- Test: `momentum/tests/`, `sentinel/tests/` (adjust existing)

- [ ] **Step 1: Read** `momentum/scheduler.py:273-290` (the `if not plan.is_rebalance_day ...` gate) + `:549-556` (`--force-rebalance`) + `:560-583` (amain). `sentinel/scheduler.py:144-150` (`is_trading_day` gate) + `:370-376` (argparse) + amain.

- [ ] **Step 2: Write/adjust failing tests**

Momentum: existing tests likely assert "no rebalance on non-first-day". Those assertions are now wrong (cadence is the dispatcher's job). Update them to: `run_once` on a non-first-day **does** proceed to build the decision (cadence no longer gates inside the scheduler); `--force-rebalance` still works as the explicit override path. Add `momentum/tests/test_scheduler_cadence_removed.py`:
```python
async def test_run_once_no_longer_self_gates_on_rebalance_day(momentum_fixture):
    # On a non-first-trading-day, run_once proceeds (dispatcher gates,
    # not the scheduler). It must NOT early-return "no_rebalance".
    summary = await momentum_fixture.run_once(as_of=NON_FIRST_DAY)
    assert summary.is_rebalance_day is True or summary.decision is not None
```
Sentinel: add `sentinel/tests/test_scheduler_force.py`:
```python
async def test_force_flag_bypasses_trading_day_gate(...):
    # is_trading_day gate deleted; --force present and parsed.
    args = _parse_args(["--force"])
    assert args.force is True
```
(Use the engines' existing test fixtures/harness — read `momentum/tests/` & `sentinel/tests/` for the established pattern; mirror it.)

- [ ] **Step 3: Run, verify fail** (old behavior still present).

- [ ] **Step 4: Implement**

Momentum: delete the `lifecycle.assess` cadence gate at `:273-290` that early-returns when `not plan.is_rebalance_day` (keep building/submitting the rebalance unconditionally when called). KEEP `--force-rebalance` arg + its plumbing (operator escape hatch, now only meaningful for direct manual invocation). Remove the now-dead `is_rebalance_day`/`_month_end` helpers ONLY if unused after the deletion (grep first; do not remove shared helpers still referenced elsewhere). Do not touch strategy/sizing logic.
Sentinel: delete the `is_trading_day` early-return at `:144-150`. Add to `_parse_args` (`:370-376`): `p.add_argument("--force", action="store_true")`; thread `force=args.force` into `SentinelScheduler.__init__`/`run_once` only as a documented no-op-compatible flag (there is no remaining internal cadence gate to bypass — `--force` is reserved for parity with momentum + future use; it must parse and be accepted without error).

- [ ] **Step 5: Run, verify pass**

Run: `/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q momentum/tests sentinel/tests -v`
Expected: PASS (adjusted + existing). Report exactly which existing assertions changed and why (cadence authority moved — not a weakening).

- [ ] **Step 6: ruff + commit**

```bash
/Users/michael/short-term-trading-engine/.venv/bin/python -m ruff check momentum/scheduler.py sentinel/scheduler.py
git add momentum/ sentinel/
git commit -m "refactor(momentum,sentinel): delete in-scheduler cadence gates (engine_profile is sole authority); keep/add --force

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 9: Verification gate

**Files:** none.

- [ ] **Step 1: Scope diff** — `git diff --stat origin/main...HEAD` shows ONLY: `ops/engine_dispatch.py`, `ops/engine_service.py`, `scripts/run_all_engines.sh`, `scripts/tests/test_engine_dispatch.py`, `scripts/tests/test_engine_service.py`, `tpcore/quality/validation/capital_gate.py` (+test), `momentum/scheduler.py` (+tests), `sentinel/scheduler.py` (+tests), spec/plan docs. NO `tpcore/selfheal`, `tpcore/feeds`, ingestion, `ops/data_repair_service.py` (data lane). Any other path → STOP, scope violation.
- [ ] **Step 2: Full gate**

```bash
/Users/michael/short-term-trading-engine/.venv/bin/python -m ruff check ops/ tpcore/ momentum/ sentinel/ scripts/
/Users/michael/short-term-trading-engine/.venv/bin/python tpcore/scripts/check_imports.py ops tpcore momentum sentinel
/Users/michael/short-term-trading-engine/.venv/bin/python -m pytest -q
bash -n scripts/run_all_engines.sh
```
Expected: ruff clean; check_imports clean (engine_dispatch imports only tpcore + stdlib + structlog, no cross-engine import — `ops/` may import engine schedulers only via subprocess, never `import {engine}`); full suite green; bash OK.

- [ ] **Step 3: Completion commit**

```bash
git commit --allow-empty -m "chore(sub-project-b): event-driven engine dispatch complete — suite green

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:** §2 architecture → T2 (dispatcher) + T6 (thin bash) + T7 (engine_service). §3 dispatch flow → T2 (fire/skip) + T3 (data-request) + T5 (terminal handling). §4 re-trigger → T7. §5 contract → T3 (`schema:1`, uuid `request_id`, `HealSpec.source` via T1, no client timestamp — DB `recorded_at`) + T5 (consume terminal by `request_id`). §6 idempotency/timeout/crashed-STARTUP → T5 (timeout) + T4 (crashed-STARTUP) + T3 (dedup). §7 cadence removal → T8. §8 testing → each task's TDD. §9 scope → T9 Step 1. D-B1..D-B5 all mapped (D-B2 = T3 never-heal assertion `heal.assert_not_called()`; D-B4 vocab = T1). No gaps.

**Placeholder scan:** No "TBD"/"handle errors". The few "read X:line and mirror the real predicate/fixture" instructions are explicit reuse-of-verified-internals with a concrete TDD test pinning behavior (capital_gate `_evaluate` predicate, engine test fixtures) — not placeholders; the test is the contract. Exact event literals (`ENGINE_DATA_REQUEST`, `DATA_REPAIR_COMPLETE/ESCALATED`, `STARTUP`, `SCAN_COMPLETE`/`SHUTDOWN`) and the real `_INSERT_SQL` columns are pinned from source.

**Type/name consistency:** `dispatch_once(pool, now)`, `_invoke_scheduler(engine)->int`, `ROSTER`, `_REQUEST_EVENT`, `_TERMINAL_EVENTS`, `_open_request_state` (T5 supersedes T3's `_has_open_request` — flagged in T5 Step 3 to replace, one probe), `failing_sources_for_engine(pool, engine, *, max_age_days=7)->list[str]` (T1) used identically in T3. `FireDecision.checks["data_ready"]` key matches Sub-project A. `TRIGGER_EVENT_TYPES` (T7) consistent. Consistent throughout.
