# Sub-project C — Allocator → Event-Driven Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fold the allocator into the single event-driven dispatch path (`ops/engine_dispatch.py`) as the first gated step before the engine ROSTER loop, and retire its Monday launchd cron.

**Architecture:** B's per-engine dispatch ladder (fire / data-blocked / repair-terminal / crashed-startup / skip) is extracted into a reusable `_dispatch_engine(pool, now, engine, invoke)` (pure behavior-preserving refactor, guarded by B's existing suite). The ROSTER loop calls it with `_safe_invoke`; a new `_dispatch_allocator` calls it FIRST with a new `_invoke_allocator` that subprocesses the exact canonical command `python scripts/ops.py --allocate`. The allocator gets STARTUP/SHUTDOWN instrumentation (so `should_fire` WEEKLY idempotency works) and a real per-engine data gate (`ENGINE_TABLES["allocator"] = {"prices_daily"}`). The launchd cron is deleted.

**Tech Stack:** Python 3.11, asyncio, asyncpg, structlog, pytest (`asyncio_mode = "auto"`), the `tpcore.engine_profile.should_fire` State-of-Truth, `tpcore.logging.db_handler.DBLogHandler`.

**Lane / scope discipline:** C touches ONLY `ops/engine_dispatch.py`, `tpcore/allocator/service.py`, `tpcore/quality/validation/capital_gate.py`, `scripts/install_all_daemons.sh`, `scripts/install_launchd_allocator.sh` (delete), `CLAUDE.md`, `docs/OPERATIONS.md`, and the matching test files. It does NOT modify `scripts/ops.py` internals, `tpcore/selfheal`, `tpcore/feeds`, `tpcore/ingestion`, `reversion/`, `vector/`, `ops/data_repair_service.py` (data lane), nor the allocator's allocation/sizing/freeze strategy logic (instrumentation only). Resolved §3b plan-time verification (no assumption): a raising `cmd_allocate` propagates uncaught through `scripts/ops.py`'s `try/finally` (the `finally` only closes the pool) → `asyncio.run` re-raises → non-zero process exit; a freeze/skip is a *valid* normal `run_once` return → exit 0 and is NOT a failure (engines proceed). Therefore the subprocess returncode is the correct and sufficient failure signal and `scripts/ops.py` is left untouched (honors the data-lane boundary).

---

## File Structure

| File | Responsibility | Change |
|------|----------------|--------|
| `ops/engine_dispatch.py` | Single event-driven dispatch path | Extract `_dispatch_engine`; add `_invoke_allocator`, `_dispatch_allocator`; call allocator first in `dispatch_once` |
| `tpcore/allocator/service.py` | Weekly inverse-vol capital rebalance | Add STARTUP/SHUTDOWN/ERROR instrumentation around `run_once` (instrumentation only) |
| `tpcore/quality/validation/capital_gate.py` | Per-engine data gate | Add `"allocator": frozenset({"prices_daily"})` to `ENGINE_TABLES` |
| `scripts/install_all_daemons.sh` | 5→4 daemon installer | Remove `install_launchd_allocator` from loop + summary line |
| `scripts/install_launchd_allocator.sh` | (retired) | Delete |
| `CLAUDE.md` / `docs/OPERATIONS.md` | Docs | "daemon Mon 13:00 UTC" → "event-driven via engine_dispatch" |
| `scripts/tests/test_engine_dispatch.py` | Dispatch tests | Add allocator dispatch tests + autouse `_invoke_allocator` no-op fixture; reconcile count-based B assertions |
| `tpcore/tests/test_allocator_startup_shutdown.py` | New | STARTUP/SHUTDOWN instrumentation tests |
| `tpcore/quality/validation/tests/test_capital_gate.py` | Gate tests | Add allocator ENGINE_TABLES / failing_sources tests |

---

## Task 1: Extract `_dispatch_engine` (behavior-preserving refactor)

B's per-engine ladder is currently inlined in `dispatch_once`'s `for engine in ROSTER` loop. Extract it verbatim into `_dispatch_engine(pool, now, engine, invoke)` so the allocator can reuse the SAME ladder (DRY; spec §3 "reused, not duplicated"). The three subprocess-invoke sites (`fire`, `refire_after_repair`, `crashed_startup_refire`) become `await invoke(engine)`. Zero behavior change for ROSTER — B's existing suite is the regression guard.

**Files:**
- Modify: `ops/engine_dispatch.py` (the `dispatch_once` body, lines ~137–203)
- Test: `scripts/tests/test_engine_dispatch.py`

- [ ] **Step 1: Run B's existing suite — confirm green baseline**

Run: `cd /Users/michael/short-term-trading-engine/.claude/worktrees/allocator-event-driven-spC && python -m pytest scripts/tests/test_engine_dispatch.py -q`
Expected: PASS (all existing B tests green — this is the equivalence oracle for the refactor).

- [ ] **Step 2: Write the failing seam test**

Add to `scripts/tests/test_engine_dispatch.py` (after the existing imports/header — do NOT alter the ops-collision guard block):

```python
async def test_dispatch_once_delegates_each_roster_engine_to_dispatch_engine():
    """dispatch_once delegates per-engine work to _dispatch_engine with
    _safe_invoke as the injected invoker (the extraction seam)."""
    calls: list[tuple[str, object]] = []

    async def _spy(pool, now, engine, invoke):
        calls.append((engine, invoke))

    pool = object()
    now = datetime(2026, 5, 18, 13, 0, tzinfo=UTC)
    with patch.object(ed, "_dispatch_engine", _spy), \
         patch.object(ed, "_dispatch_allocator", AsyncMock()):
        await dispatch_once(pool, now)

    assert [c[0] for c in calls] == list(ROSTER)
    assert all(c[1] is ed._safe_invoke for c in calls)
```

- [ ] **Step 3: Run it to verify it fails**

Run: `python -m pytest scripts/tests/test_engine_dispatch.py::test_dispatch_once_delegates_each_roster_engine_to_dispatch_engine -q`
Expected: FAIL with `AttributeError: <module 'ops.engine_dispatch'> does not have the attribute '_dispatch_engine'` (and/or `_dispatch_allocator`).

- [ ] **Step 4: Extract the ladder into `_dispatch_engine`**

In `ops/engine_dispatch.py`, replace the entire `async def dispatch_once(pool, now: datetime) -> None:` body (the `for engine in ROSTER:` block, lines ~137–203) with this — the ladder moves verbatim into `_dispatch_engine`, the loop variable `engine` becomes a parameter, and every `await _safe_invoke(engine)` becomes `await invoke(engine)`:

```python
async def _dispatch_engine(pool, now: datetime, engine: str,
                           invoke) -> None:
    """One profiled actor's gated dispatch (B's ladder, extracted so
    the allocator reuses it — spec C §3, reused not duplicated).

    `invoke` is an awaitable `(engine: str) -> None` that runs the
    actor with crash isolation (`_safe_invoke` for ROSTER engines,
    `_invoke_allocator` for the allocator).
    """
    decision = await should_fire(engine, now, pool)
    if decision.fire:
        logger.info("engine_dispatch.dispatched", engine=engine)
        await invoke(engine)
    elif decision.checks.get("data_ready") is False:
        window_start = cadence_window_start(engine, now)
        # CLEANUP #2 (deferred from B-T3): compute failing sources FIRST
        # (failing_sources_for_engine does its own pool.acquire) and
        # only THEN open our outer conn — there is never a nested
        # acquire (one conn held at a time for the whole branch).
        sources = await failing_sources_for_engine(pool, engine)
        async with pool.acquire() as conn:
            state = await _open_request_state(conn, engine, window_start)
            if state is None:
                # no request yet → emit one (dedup boundary)
                await _emit_data_request(
                    conn, engine, sources, decision.reason)
                return
            terminal = state["terminal"]
            if terminal == "DATA_REPAIR_COMPLETE" and state["green"] is True:
                redecision = await should_fire(engine, now, pool)
                if redecision.fire:
                    logger.info("engine_dispatch.refire_after_repair",
                                engine=engine)
                    await invoke(engine)
                else:
                    logger.info(
                        "engine_dispatch.repair_green_but_still_no_fire",
                        engine=engine, reason=redecision.reason)
                return
            if (terminal == "DATA_REPAIR_ESCALATED"
                    or (terminal == "DATA_REPAIR_COMPLETE"
                        and not state["green"])):
                logger.error("engine_dispatch.data_unrecovered",
                             engine=engine, request_id=state["request_id"])
                return
            # terminal is None — request open, no terminal event yet
            if (now - state["req_ts"]).total_seconds() \
                    >= _NO_TERMINAL_TIMEOUT_SECONDS:
                logger.error("engine_dispatch.data_request_timeout",
                             engine=engine,
                             request_id=state["request_id"])
            else:
                logger.info("engine_dispatch.request_open", engine=engine)
            return
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
    else:
        logger.info(
            "engine_dispatch.skipped", engine=engine,
            reason=decision.reason,
            data_ready=decision.checks.get("data_ready"),
        )


async def dispatch_once(pool, now: datetime) -> None:
    for engine in ROSTER:
        await _dispatch_engine(pool, now, engine, _safe_invoke)
```

Note the two `continue` statements in the original inlined loop become `return` (the body is now a function — semantically identical: `continue` ended that engine's iteration, `return` ends `_dispatch_engine`). All logger event keys and branch order are preserved exactly.

- [ ] **Step 5: Run the seam test + full B suite — verify green**

Run: `python -m pytest scripts/tests/test_engine_dispatch.py -q`
Expected: PASS — the new seam test passes AND every pre-existing B test still passes (behavior preserved). If any pre-existing test fails, the extraction changed behavior — fix the extraction, do not edit the assertion.

- [ ] **Step 6: Commit**

```bash
git add ops/engine_dispatch.py scripts/tests/test_engine_dispatch.py
git commit -m "$(cat <<'EOF'
refactor(engine_dispatch): extract _dispatch_engine from dispatch_once

Behavior-preserving extraction of B's per-engine ladder into
_dispatch_engine(pool, now, engine, invoke); ROSTER loop delegates
with _safe_invoke. Enables allocator reuse (Sub-project C §3, reused
not duplicated). B's existing suite is the equivalence oracle.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: `_invoke_allocator` — canonical allocator subprocess + crash isolation

A new invoker that runs the EXACT canonical command the retired launchd cron ran (`python scripts/ops.py --allocate`), with the same crash-isolation contract as `_safe_invoke` plus the operator alarm on failure (spec §3b, D-C3). Signature matches the injected-invoker shape `(engine: str) -> None` so it slots into `_dispatch_engine`.

**Files:**
- Modify: `ops/engine_dispatch.py` (add after `_safe_invoke`, ~line 134)
- Test: `scripts/tests/test_engine_dispatch.py`

- [ ] **Step 1: Write the failing tests**

Add to `scripts/tests/test_engine_dispatch.py`:

```python
async def test_invoke_allocator_runs_canonical_command_exit_zero(caplog):
    proc = AsyncMock()
    proc.wait = AsyncMock(return_value=0)
    with patch.object(ed.asyncio, "create_subprocess_exec",
                      AsyncMock(return_value=proc)) as spawn:
        await ed._invoke_allocator("allocator")
    args = spawn.call_args[0]
    assert args[0] == sys.executable
    assert args[1:] == ("scripts/ops.py", "--allocate")
    assert "engine_dispatch.allocator_done" in caplog.text
    assert "engine_dispatch.allocator_failed" not in caplog.text


async def test_invoke_allocator_nonzero_exit_alarms_and_returns(caplog):
    proc = AsyncMock()
    proc.wait = AsyncMock(return_value=2)
    with patch.object(ed.asyncio, "create_subprocess_exec",
                      AsyncMock(return_value=proc)):
        await ed._invoke_allocator("allocator")  # must NOT raise
    assert "engine_dispatch.allocator_failed" in caplog.text


async def test_invoke_allocator_spawn_raises_is_isolated(caplog):
    with patch.object(ed.asyncio, "create_subprocess_exec",
                      AsyncMock(side_effect=OSError("no fork"))):
        await ed._invoke_allocator("allocator")  # must NOT raise
    assert "engine_dispatch.allocator_failed" in caplog.text
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest scripts/tests/test_engine_dispatch.py -k invoke_allocator -q`
Expected: FAIL with `AttributeError: ... does not have the attribute '_invoke_allocator'`.

- [ ] **Step 3: Implement `_invoke_allocator`**

In `ops/engine_dispatch.py`, immediately after the `_safe_invoke` function (ends ~line 134), add:

```python
async def _invoke_allocator(engine: str = "allocator") -> None:
    """Run the weekly capital rebalance as an isolated subprocess via
    the EXACT canonical command the retired launchd cron ran
    (`python scripts/ops.py --allocate`; spec C §3b / D-C2). Crash-
    isolated like `_safe_invoke` AND raises the operator alarm
    `engine_dispatch.allocator_failed` on non-zero / spawn error
    (D-C3) so the engine ROSTER loop proceeds on the persisted
    prior-week risk_state.engine_equity — a weekly-rebalance failure
    is degraded-not-broken and must NEVER abort the daily sweep.

    `engine` is always "allocator" by construction (kept for the
    uniform injected-invoker signature `_dispatch_engine` expects);
    a freeze/skip is a valid exit-0 outcome and is NOT a failure.
    """
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "scripts/ops.py", "--allocate", cwd=repo,
        )
        rc = await proc.wait()
    except Exception as exc:  # noqa: BLE001 — isolate: never abort sweep
        logger.error("engine_dispatch.allocator_failed", error=str(exc))
        return
    if rc == 0:
        logger.info("engine_dispatch.allocator_done", returncode=rc)
    else:
        logger.error("engine_dispatch.allocator_failed", returncode=rc)
```

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest scripts/tests/test_engine_dispatch.py -k invoke_allocator -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add ops/engine_dispatch.py scripts/tests/test_engine_dispatch.py
git commit -m "$(cat <<'EOF'
feat(engine_dispatch): _invoke_allocator canonical subprocess invoker

Runs `python scripts/ops.py --allocate` (exact retired-cron command,
D-C2); crash-isolated + engine_dispatch.allocator_failed operator
alarm on non-zero/spawn error (D-C3) — never aborts the sweep.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: `_dispatch_allocator` first, before the ROSTER loop

Wire the allocator as the FIRST gated step in `dispatch_once` (spec §2/§3, D-C1). `_dispatch_allocator` is a thin wrapper delegating to the Task-1 ladder with the Task-2 invoker — so the allocator inherits B's data-blocked / repair-terminal / crashed-startup handling for free via the SAME helpers (spec §3d). Ordering (allocator → engines) is guaranteed by construction (sequential `await`).

**Files:**
- Modify: `ops/engine_dispatch.py` (`dispatch_once`, add `_dispatch_allocator`)
- Test: `scripts/tests/test_engine_dispatch.py`

- [ ] **Step 1: Add the autouse no-op fixture (prevents real subprocess in unrelated B tests)**

After Task 2, every existing B test that mocks `should_fire` with a fire-returning `return_value` will now also "fire" the allocator and could spawn a real `scripts/ops.py --allocate`. Add this autouse fixture once, immediately after the import header in `scripts/tests/test_engine_dispatch.py`:

```python
import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_real_allocator():
    """Sub-project C: dispatch_once now runs the allocator first.
    Neutralize the real subprocess for every test that doesn't
    explicitly exercise it (allocator-specific tests patch
    ed._invoke_allocator / ed._dispatch_allocator themselves)."""
    with patch.object(ed, "_invoke_allocator", AsyncMock()):
        yield
```

- [ ] **Step 2: Write the failing tests**

Add to `scripts/tests/test_engine_dispatch.py`:

```python
def _fire(reason="fire"):
    return FireDecision(fire=True, reason=reason,
                        checks={"profiled": True, "cadence": True,
                                "market_closed": True, "data_ready": True,
                                "not_already_run": True})


def _skip(reason="off cadence"):
    return FireDecision(fire=False, reason=reason,
                        checks={"profiled": True, "cadence": False})


def _blocked():
    return FireDecision(fire=False, reason="data not ready",
                        checks={"profiled": True, "cadence": True,
                                "market_closed": True, "data_ready": False})


async def test_allocator_fires_before_any_roster_engine():
    order: list[str] = []

    async def _sf(engine, now, pool):
        return _fire()

    async def _alloc(engine="allocator"):
        order.append("allocator")

    async def _eng(engine):
        order.append(engine)

    with patch.object(ed, "should_fire", _sf), \
         patch.object(ed, "_invoke_allocator", _alloc), \
         patch.object(ed, "_safe_invoke", _eng):
        await dispatch_once(object(), datetime(2026, 5, 18, 13, 0, tzinfo=UTC))

    assert order[0] == "allocator"
    assert order[1:] == list(ROSTER)


async def test_allocator_failure_does_not_abort_roster(caplog):
    async def _sf(engine, now, pool):
        return _fire()

    ran: list[str] = []

    async def _alloc(engine="allocator"):
        # _invoke_allocator never raises (it self-isolates); simulate
        # the alarm it logs on failure.
        ed.logger.error("engine_dispatch.allocator_failed", returncode=2)

    async def _eng(engine):
        ran.append(engine)

    with patch.object(ed, "should_fire", _sf), \
         patch.object(ed, "_invoke_allocator", _alloc), \
         patch.object(ed, "_safe_invoke", _eng):
        await dispatch_once(object(), datetime(2026, 5, 18, 13, 0, tzinfo=UTC))

    assert "engine_dispatch.allocator_failed" in caplog.text
    assert ran == list(ROSTER)  # engines still ran (Q3)


async def test_allocator_data_blocked_emits_request_for_allocator():
    async def _sf(engine, now, pool):
        return _blocked() if engine == "allocator" else _skip()

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)  # _open_request_state: none
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    with patch.object(ed, "should_fire", _sf), \
         patch.object(ed, "failing_sources_for_engine",
                      AsyncMock(return_value=["prices_daily"])), \
         patch.object(ed, "_emit_data_request",
                      AsyncMock(return_value="rid-1")) as emit, \
         patch.object(ed, "_invoke_allocator", AsyncMock()) as alloc, \
         patch.object(ed, "_safe_invoke", AsyncMock()):
        await dispatch_once(pool, datetime(2026, 5, 18, 13, 0, tzinfo=UTC))

    emit.assert_awaited_once()
    assert emit.call_args[0][1] == "allocator"
    assert emit.call_args[0][2] == ["prices_daily"]
    alloc.assert_not_awaited()  # blocked → allocator not run


async def test_allocator_off_cadence_skips_no_invoke():
    async def _sf(engine, now, pool):
        return _skip()

    with patch.object(ed, "should_fire", _sf), \
         patch.object(ed, "_invoke_allocator", AsyncMock()) as alloc, \
         patch.object(ed, "_safe_invoke", AsyncMock()):
        await dispatch_once(object(), datetime(2026, 5, 19, 13, 0, tzinfo=UTC))

    alloc.assert_not_awaited()
```

Ensure `MagicMock` is imported in the test header alongside `AsyncMock, patch` (`from unittest.mock import AsyncMock, MagicMock, patch`) — add `MagicMock` if absent.

- [ ] **Step 3: Run to verify they fail**

Run: `python -m pytest scripts/tests/test_engine_dispatch.py -k allocator -q`
Expected: FAIL — `test_allocator_fires_before_any_roster_engine` fails (allocator not dispatched first; `order[0]` is a ROSTER engine) and `_dispatch_allocator` not yet wired.

- [ ] **Step 4: Wire `_dispatch_allocator` into `dispatch_once`**

In `ops/engine_dispatch.py`, add `_dispatch_allocator` and call it first. Replace the Task-1 `dispatch_once` with:

```python
async def _dispatch_allocator(pool, now: datetime) -> None:
    """Sub-project C (D-C1): the allocator is the FIRST gated step,
    before the engine ROSTER loop. Reuses B's exact ladder via
    `_dispatch_engine` with the canonical `_invoke_allocator`
    (subprocess `scripts/ops.py --allocate`). should_fire("allocator")
    applies the WEEKLY_FIRST_TRADING_DAY cadence + market-closed +
    per-engine data gate + STARTUP idempotency uniformly; data-blocked
    emits ENGINE_DATA_REQUEST(engine="allocator") on the locked
    inter-lane contract. Ordering (allocator before engines) is
    guaranteed by construction (sequential await); on allocator
    failure the engines run on the persisted prior-week
    risk_state.engine_equity (D-C3).
    """
    await _dispatch_engine(pool, now, "allocator", _invoke_allocator)


async def dispatch_once(pool, now: datetime) -> None:
    await _dispatch_allocator(pool, now)
    for engine in ROSTER:
        await _dispatch_engine(pool, now, engine, _safe_invoke)
```

- [ ] **Step 5: Run the allocator tests — verify green**

Run: `python -m pytest scripts/tests/test_engine_dispatch.py -k allocator -q`
Expected: PASS (the 5 allocator tests + the Task-1 seam test).

- [ ] **Step 6: Run the FULL dispatch suite + reconcile count-based B assertions**

Run: `python -m pytest scripts/tests/test_engine_dispatch.py -q`
Expected: PASS. `dispatch_once` now calls `should_fire` once more (for `"allocator"`) before the ROSTER loop. The autouse `_no_real_allocator` fixture neutralizes the subprocess. Two reconciliation cases — apply ONLY if a pre-existing B test now fails:
  1. A test asserting `should_fire`/`mock_should_fire.call_count == len(ROSTER)` (or `== 4`): update to `== len(ROSTER) + 1` (the leading allocator gate call). Do NOT weaken the assertion otherwise.
  2. A test driving `should_fire` via `side_effect=[<list of 4 decisions>]`: prepend an allocator decision matching that test's intent — `_skip()` if the test doesn't care about the allocator (most common), or the test's existing first decision pattern if it asserts uniform behavior.
Make the minimal change that restores the original test's intent; never delete a B assertion.

- [ ] **Step 7: Commit**

```bash
git add ops/engine_dispatch.py scripts/tests/test_engine_dispatch.py
git commit -m "$(cat <<'EOF'
feat(engine_dispatch): allocator as first gated step (Sub-project C)

dispatch_once runs _dispatch_allocator before the ROSTER loop, reusing
B's _dispatch_engine ladder with _invoke_allocator. should_fire(
"allocator") applies WEEKLY cadence + per-engine data gate + STARTUP
idempotency uniformly; data-blocked emits ENGINE_DATA_REQUEST(
engine="allocator"); allocator failure never aborts the sweep (D-C1/
D-C3). Autouse fixture neutralizes the real subprocess in B tests.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Allocator STARTUP / SHUTDOWN / ERROR instrumentation

`should_fire("allocator")` keys WEEKLY idempotency off a `STARTUP` row in `platform.application_log` within the cadence window. `AllocatorService.run_once` emits `ALLOCATOR_*` events but never `STARTUP`/`SHUTDOWN` (the exact gap momentum had pre-T4b). Wrap `run_once` with the canonical `reversion`/momentum-T4b idiom: `startup()` before any IO, `try/except` (`error()` + `exit_code=1` + re-raise), `finally` `shutdown(duration_ms, exit_code)`. Instrumentation ONLY — zero allocation/sizing/freeze logic change (spec §4, D-C4).

**Files:**
- Modify: `tpcore/allocator/service.py` (`run_once`, lines ~168–247; imports)
- Test: `tpcore/tests/test_allocator_startup_shutdown.py` (create)

- [ ] **Step 1: Write the failing tests**

Create `tpcore/tests/test_allocator_startup_shutdown.py`:

```python
from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from tpcore.allocator import AllocatorService


def _svc() -> AllocatorService:
    # pool=None keeps _db_log lazily None; we inject a fake handler.
    svc = AllocatorService(pool=None, platform_capital=Decimal("40000"))  # type: ignore[arg-type]
    svc._db_log = AsyncMock()
    return svc


async def test_run_once_emits_startup_then_shutdown_on_success():
    svc = _svc()
    with patch.object(svc, "_load_histories", AsyncMock(return_value={})), \
         patch.object(svc, "_decide", return_value=[]), \
         patch.object(svc, "_compute_drift",
                      AsyncMock(return_value=(Decimal("0"), {}))), \
         patch.object(svc, "_fetch_market_regime",
                      AsyncMock(return_value=("trending", None))), \
         patch.object(svc, "_classify_rebalance",
                      return_value=(None, "drift ok")), \
         patch.object(svc, "_persist", AsyncMock(return_value=[])):
        await svc.run_once()

    svc._db_log.startup.assert_awaited_once()
    svc._db_log.shutdown.assert_awaited_once()
    # exit_code 0 on success
    assert svc._db_log.shutdown.call_args[0][1] == 0
    svc._db_log.error.assert_not_awaited()


async def test_run_once_shutdown_exit1_and_error_on_exception():
    svc = _svc()
    boom = RuntimeError("histories failed")
    with patch.object(svc, "_load_histories", AsyncMock(side_effect=boom)):
        with pytest.raises(RuntimeError, match="histories failed"):
            await svc.run_once()

    svc._db_log.startup.assert_awaited_once()
    svc._db_log.error.assert_awaited_once()
    svc._db_log.shutdown.assert_awaited_once()
    assert svc._db_log.shutdown.call_args[0][1] == 1  # exit_code


async def test_run_once_no_db_log_is_a_noop_not_a_crash():
    svc = AllocatorService(pool=None, platform_capital=Decimal("40000"))  # type: ignore[arg-type]
    assert svc._db_log is None
    with patch.object(svc, "_load_histories", AsyncMock(return_value={})), \
         patch.object(svc, "_decide", return_value=[]), \
         patch.object(svc, "_compute_drift",
                      AsyncMock(return_value=(Decimal("0"), {}))), \
         patch.object(svc, "_fetch_market_regime",
                      AsyncMock(return_value=("trending", None))), \
         patch.object(svc, "_classify_rebalance",
                      return_value=(None, "drift ok")), \
         patch.object(svc, "_persist", AsyncMock(return_value=[])):
        await svc.run_once()  # must not raise
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tpcore/tests/test_allocator_startup_shutdown.py -q`
Expected: FAIL — `startup`/`shutdown`/`error` are never awaited (no instrumentation yet).

- [ ] **Step 3: Add the imports**

In `tpcore/allocator/service.py`, ensure `time` is imported (top-of-file imports). If `import time` is absent, add it alphabetically among the stdlib imports.

- [ ] **Step 4: Instrument `run_once`**

Wrap the entire existing `run_once` body. Keep every existing statement byte-for-byte; only add the startup call, the `try/except/finally`, and indent the existing body into the `try`. The new shape:

```python
async def run_once(self) -> list[AllocationDecision]:
    started_at = time.monotonic()
    exit_code = 0
    if self._db_log is not None:
        await self._db_log.startup(
            commit_sha=os.getenv("RAILWAY_GIT_COMMIT_SHA")
            or os.getenv("GIT_COMMIT_SHA")
        )
    try:
        histories = await self._load_histories()
        decisions = self._decide(histories)

        # ── Rebalance gating — items 44 + 45 (2026-05-14) ───────────
        # 1. Compute drift per active engine (frozen engines bypass).
        max_drift, drift_per_engine = await self._compute_drift(decisions)
        # 2. Fetch SPY-based market regime.
        regime, chop_value = await self._fetch_market_regime()
        # 3. Classify rebalance decision.
        skip_reason, rebalance_reason = self._classify_rebalance(
            max_drift, regime)
        # 4. Persist (always for frozen rows; conditional for active).
        if skip_reason is not None:
            pruned_engines = await self._persist(decisions, active_skip=True)
            if self._db_log is not None:
                await self._db_log.log(
                    event_type="ALLOCATOR_SKIPPED",
                    message=f"rebalance skipped — {skip_reason}",
                    severity="INFO",
                    data={
                        "as_of": self._as_of.isoformat(),
                        "reason": skip_reason,
                        "max_drift_pct": float(max_drift),
                        "drift_per_engine": {
                            k: float(v) for k, v in drift_per_engine.items()},
                        "regime": regime,
                        "chop_value": (
                            float(chop_value)
                            if chop_value is not None else None),
                        "frozen_engines_persisted": [
                            d.engine for d in decisions
                            if d.freeze_state != "active"],
                    },
                )
        else:
            pruned_engines = await self._persist(
                decisions, active_skip=False)
            if self._db_log is not None:
                await self._db_log.log(
                    event_type="ALLOCATOR_REBALANCED",
                    message=f"rebalanced — {rebalance_reason}",
                    severity="INFO",
                    data={
                        "as_of": self._as_of.isoformat(),
                        "reason": rebalance_reason,
                        "max_drift_pct": float(max_drift),
                        "drift_per_engine": {
                            k: float(v) for k, v in drift_per_engine.items()},
                        "regime": regime,
                        "chop_value": (
                            float(chop_value)
                            if chop_value is not None else None),
                        "new_weights": {
                            d.engine: float(d.weight) for d in decisions},
                    },
                )

        # ── Prune audit (only when something was actually pruned) ────
        if pruned_engines and self._db_log is not None:
            await self._db_log.log(
                event_type="ALLOCATOR_PRUNED_RISK_STATE",
                message=f"pruned {len(pruned_engines)} stale risk_state row(s)",
                severity="INFO",
                data={
                    "as_of": self._as_of.isoformat(),
                    "pruned_engines": pruned_engines,
                    "live_engines": list(self._engines),
                },
            )

        logger.info(
            "tpcore.allocator.rebalance",
            as_of=self._as_of.isoformat(),
            platform_capital=str(self._platform_capital),
            decisions={
                d.engine: str(d.allocated_capital) for d in decisions},
            frozen=[
                d.engine for d in decisions if d.freeze_state != "active"],
            enforce_freeze=self._enforce_freeze,
            max_drift_pct=float(max_drift),
            regime=regime,
            skipped=skip_reason is not None,
            decision_reason=skip_reason or rebalance_reason,
        )
        return decisions
    except Exception as exc:
        exit_code = 1
        if self._db_log is not None:
            await self._db_log.error(exc, context="allocator_crash")
        raise
    finally:
        if self._db_log is not None:
            duration_ms = int((time.monotonic() - started_at) * 1000)
            await self._db_log.shutdown(duration_ms, exit_code)
```

(`os` is already imported in `service.py` — it is used by `os.getenv` elsewhere; if a lint flags it as unused-before, it is now used here. Verify `import os` is present at top; it is.)

- [ ] **Step 5: Run the new tests — verify green**

Run: `python -m pytest tpcore/tests/test_allocator_startup_shutdown.py -q`
Expected: PASS (3 passed).

- [ ] **Step 6: Run the full allocator suite — no regression**

Run: `python -m pytest tpcore/tests/test_allocator.py tpcore/tests/test_allocator_prune.py tpcore/tests/test_allocator_drift_gating.py tpcore/tests/test_allocator_engine_default.py -q`
Expected: PASS (instrumentation-only change — existing allocator behavior unchanged; `_db_log` is None in `pool=None` tests so the new calls are no-ops).

- [ ] **Step 7: Commit**

```bash
git add tpcore/allocator/service.py tpcore/tests/test_allocator_startup_shutdown.py
git commit -m "$(cat <<'EOF'
feat(allocator): STARTUP/SHUTDOWN/ERROR instrumentation (Sub-project C)

run_once now emits STARTUP before IO and SHUTDOWN(duration_ms,
exit_code) in finally on every path (success/skip/exception), plus
ERROR on crash — the canonical reversion/momentum-T4b idiom. Required
so should_fire("allocator") WEEKLY idempotency + crashed-STARTUP guard
work (D-C4). Instrumentation only; zero allocation logic change.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Per-engine data gate — `ENGINE_TABLES["allocator"]`

`should_fire("allocator")` calls the per-engine capital gate. `"allocator"` is absent from `ENGINE_TABLES`, so `failing_sources_for_engine("allocator")` returns `[]` (unknown engine → nothing to report) and the gate falls back to the over-broad global all-green. Add the allocator's REAL validation-gated dependency: `prices_daily` (SPY regime/CHOP). AAR/risk_state are engine *output* tables, not validation-gated (spec §5, D-C5).

**Files:**
- Modify: `tpcore/quality/validation/capital_gate.py` (`ENGINE_TABLES`, lines ~60–67)
- Test: `tpcore/quality/validation/tests/test_capital_gate.py`

- [ ] **Step 1: Write the failing tests**

Add to `tpcore/quality/validation/tests/test_capital_gate.py`:

```python
def test_engine_tables_has_allocator_prices_daily():
    from tpcore.quality.validation.capital_gate import ENGINE_TABLES

    assert ENGINE_TABLES["allocator"] == frozenset({"prices_daily"})


def test_allocator_source_is_a_real_healspec_source():
    from tpcore.quality.validation.capital_gate import ENGINE_TABLES
    from tpcore.selfheal.registry import HEAL_SPECS

    known_sources = {spec.source for spec in HEAL_SPECS.values()}
    assert ENGINE_TABLES["allocator"] <= known_sources
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tpcore/quality/validation/tests/test_capital_gate.py -k allocator -q`
Expected: FAIL with `KeyError: 'allocator'`.

- [ ] **Step 3: Add the allocator entry**

In `tpcore/quality/validation/capital_gate.py`, add the `allocator` key to `ENGINE_TABLES` (after the `sentinel` entry, before the closing brace):

```python
ENGINE_TABLES: dict[str, frozenset[str]] = {
    "reversion": frozenset({"prices_daily", "fundamentals_quarterly"}),
    "vector": frozenset({
        "prices_daily", "fundamentals_quarterly", "earnings_events",
    }),
    "momentum": frozenset({"prices_daily", "liquidity_tiers"}),
    "sentinel": frozenset({"prices_daily", "macro_indicators"}),
    # Sub-project C (D-C5): the allocator's only validation-gated input
    # is prices_daily (SPY regime/CHOP). AAR/risk_state are engine
    # *output* tables, not validation-gated. Gating here on the REAL
    # dependency per the per-engine-gate model (not the global
    # fail-safe) makes failing_sources_for_engine("allocator") return
    # the right HealSpec.source for the ENGINE_DATA_REQUEST path.
    "allocator": frozenset({"prices_daily"}),
}
```

- [ ] **Step 4: Run the new tests — verify green**

Run: `python -m pytest tpcore/quality/validation/tests/test_capital_gate.py -k allocator -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Run the full capital_gate suite — registry-coverage still green**

Run: `python -m pytest tpcore/quality/validation/tests/test_capital_gate.py -q`
Expected: PASS — including the existing `test_failing_sources_for_engine_returns_healspec_source_names` (the allocator entry uses an existing valid HealSpec.source so no coverage assertion breaks).

- [ ] **Step 6: Commit**

```bash
git add tpcore/quality/validation/capital_gate.py tpcore/quality/validation/tests/test_capital_gate.py
git commit -m "$(cat <<'EOF'
feat(capital_gate): ENGINE_TABLES["allocator"] = {prices_daily}

The allocator's only validation-gated input is prices_daily (SPY
regime/CHOP). Gates the allocator on its real dependency per the
per-engine-gate model (not the global fail-safe) and makes
failing_sources_for_engine("allocator") return the right
HealSpec.source for the ENGINE_DATA_REQUEST path (D-C5).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Retire the launchd cron + update docs

The event path now fully replaces the Monday 13:00 UTC launchd cron. Remove `install_launchd_allocator` from the 5-daemon installer loop and its summary line, delete the installer script, and update the docs that describe the allocator as a daemon (spec §6).

**Files:**
- Modify: `scripts/install_all_daemons.sh` (loop line 28, summary line ~44)
- Delete: `scripts/install_launchd_allocator.sh`
- Modify: `CLAUDE.md` (line ~15), `docs/OPERATIONS.md` (line ~148)
- Test: `scripts/tests/test_engine_dispatch.py` (a shell-static guard test)

- [ ] **Step 1: Write the failing guard test**

Add to `scripts/tests/test_engine_dispatch.py`:

```python
def test_install_all_daemons_no_longer_references_allocator_launchd():
    repo = Path(__file__).resolve().parents[2]
    sh = (repo / "scripts" / "install_all_daemons.sh").read_text()
    assert "install_launchd_allocator" not in sh
    assert not (repo / "scripts" / "install_launchd_allocator.sh").exists()
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest scripts/tests/test_engine_dispatch.py -k install_all_daemons -q`
Expected: FAIL — `install_launchd_allocator` still in the loop and the script still exists.

- [ ] **Step 3: Edit `scripts/install_all_daemons.sh`**

Change the installer loop (line 28) from:

```bash
for installer in install_launchd_trade_monitor install_launchd_engine_service install_launchd_data_repair_service install_launchd_data_operations install_launchd_allocator; do
```

to (drop the allocator installer — now event-driven via `ops/engine_dispatch.py`):

```bash
# allocator retired from launchd 2026-05-17 (Sub-project C): now the
# first gated step in ops/engine_dispatch.py (event-driven, WEEKLY).
for installer in install_launchd_trade_monitor install_launchd_engine_service install_launchd_data_repair_service install_launchd_data_operations; do
```

Change the summary tail-log line (~line 44) from:

```bash
echo "  tail -f ~/Library/Logs/short-term-trading-engine/{trade-monitor,engine-service,data-repair-service,data-operations,allocator}.log"
```

to:

```bash
echo "  tail -f ~/Library/Logs/short-term-trading-engine/{trade-monitor,engine-service,data-repair-service,data-operations}.log"
```

If the script prints a daemon count anywhere (e.g. "ALL DAEMONS INSTALLED" with a number), update any "5" → "4". Verify by reading the full script first.

- [ ] **Step 4: Delete the retired installer**

```bash
git rm scripts/install_launchd_allocator.sh
```

- [ ] **Step 5: Verify shell still parses**

Run: `bash -n scripts/install_all_daemons.sh && echo OK`
Expected: `OK` (no syntax error).

- [ ] **Step 6: Update the docs**

In `CLAUDE.md`, change the `tpcore/allocator/` line (line ~15) from:

```
- tpcore/allocator/ — weekly inverse-vol capital rebalance across engines (deployed 2026-05-13, daemon Mon 13:00 UTC)
```

to:

```
- tpcore/allocator/ — weekly inverse-vol capital rebalance across engines (deployed 2026-05-13; event-driven 2026-05-17 — first gated step in ops/engine_dispatch.py, WEEKLY_FIRST_TRADING_DAY, launchd cron retired)
```

In `docs/OPERATIONS.md`, change the allocator row (line ~148) from:

```
| `allocator` | Cross-engine capital rebalance | Mon 13:00 UTC |
```

to:

```
| `allocator` | Cross-engine capital rebalance | event-driven via engine_dispatch (WEEKLY_FIRST_TRADING_DAY) |
```

If `docs/OPERATIONS.md` also lists the allocator under a "daemons installed" / launchd section, update that prose to state it is event-driven and the launchd plist is removed by re-running `install_all_daemons.sh`. Read the surrounding lines first to keep the table/format consistent.

- [ ] **Step 7: Run the guard test — verify green**

Run: `python -m pytest scripts/tests/test_engine_dispatch.py -k install_all_daemons -q`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add scripts/install_all_daemons.sh CLAUDE.md docs/OPERATIONS.md scripts/tests/test_engine_dispatch.py
git rm --cached scripts/install_launchd_allocator.sh 2>/dev/null || true
git commit -m "$(cat <<'EOF'
chore: retire allocator launchd cron — now event-driven (Sub-project C)

Drop install_launchd_allocator from the daemon installer (5→4), delete
scripts/install_launchd_allocator.sh, update CLAUDE.md/OPERATIONS.md
"daemon Mon 13:00 UTC" → "event-driven via engine_dispatch". The
operator removes the live plist by re-running install_all_daemons.sh.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Full-suite gate + finish

**Files:** none (verification only)

- [ ] **Step 1: Full test suite**

Run: `cd /Users/michael/short-term-trading-engine/.claude/worktrees/allocator-event-driven-spC && python -m pytest -q`
Expected: PASS (entire suite green — same count as the green baseline plus the new C tests; the ops-name-collision guard in `test_engine_dispatch.py` keeps full-suite collection clean).

- [ ] **Step 2: Lint + import-layering**

Run: `ruff check . && python scripts/check_imports.py`
Expected: clean — no ruff findings; `check_imports` passes (no new tpcore→engine layering violation; `_invoke_allocator` lives in `ops/`, not tpcore).

- [ ] **Step 3: Finish the branch**

Use the **superpowers:finishing-a-development-branch** skill. Per the established Sub-project A/B pattern and the operator's standing instruction ("push that shit and make sure ci is green, dont stomp the other session"): push the worktree branch, open a PR, merge when CI is green, then clean the worktree. Do NOT local-merge into the shared checkout (the data session uses it).

---

## Self-Review

**1. Spec coverage:**
- §2/§3 (allocator first gated step, one event path, ordering by construction) → Tasks 1+3 (`_dispatch_engine` extraction, `_dispatch_allocator` first in `dispatch_once`, ordering test).
- §3b/D-C2/D-C3 (canonical `ops.py --allocate` subprocess, crash-isolated, `allocator_failed` alarm, never abort sweep) → Task 2 + Task 3 `test_allocator_failure_does_not_abort_roster`. §3b plan-time exit-code verification → resolved in the header & Task 2 docstring (returncode is sufficient; `scripts/ops.py` untouched).
- §3c (data-blocked → `ENGINE_DATA_REQUEST(engine="allocator")` via B's existing helpers) → reused via `_dispatch_engine`; `test_allocator_data_blocked_emits_request_for_allocator`.
- §3d (repair-terminal / crashed-STARTUP via SAME helpers) → inherited free by Task-1 extraction (no duplicate code).
- §4/D-C4 (STARTUP/SHUTDOWN instrumentation, all paths, instrumentation only) → Task 4 (success/exception/no-db paths tested).
- §5/D-C5 (`ENGINE_TABLES["allocator"]={prices_daily}`, registry-coverage stays green) → Task 5.
- §6 (launchd retirement: installer loop, summary, delete script, docs) → Task 6.
- §8 (testing list) → every bullet maps to a test in Tasks 1–6.
- §9 (scope boundary, full suite + ruff/check_imports green) → Task 7. No gaps.

**2. Placeholder scan:** No "TBD/TODO/handle edge cases/similar to Task N". Every code step shows complete code; every command shows expected output. Task 3 Step 6 / Task 6 Step 3 contain conditional reconciliation ("ONLY if a pre-existing test fails", "if the script prints a count") — these are explicit, bounded contingencies with exact instructions and the exact replacement code, not deferred work.

**3. Type/name consistency:** `_dispatch_engine(pool, now, engine, invoke)` (Task 1) ← called by `dispatch_once` and `_dispatch_allocator` (Tasks 1, 3) with `_safe_invoke` / `_invoke_allocator`. `_invoke_allocator(engine: str = "allocator") -> None` (Task 2) matches the injected-invoker shape `await invoke(engine)` (Task 1). `FireDecision`/`should_fire`/`failing_sources_for_engine`/`_emit_data_request`/`_open_request_state`/`cadence_window_start`/`profile_for` used exactly as defined in the merged A/B modules (signatures confirmed by investigation). `DBLogHandler.startup(commit_sha=…)`, `.shutdown(duration_ms, exit_code)`, `.error(exception, context=…)`, `.log(event_type, message, severity, data)` used per the real signatures. `ENGINE_TABLES` value type `frozenset[str]` consistent with existing entries. Logger event keys (`engine_dispatch.allocator_done`/`allocator_failed`, `engine_dispatch.dispatched`/`skipped`/…) consistent across tasks and tests. No mismatches.
