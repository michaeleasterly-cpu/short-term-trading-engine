"""Event-driven engine dispatcher (Sub-project B).

Replaces the unconditional bash engine loop. Per engine: consult
``tpcore.engine_profile.should_fire``. Fire → invoke that engine's
scheduler. Data-blocked → emit ENGINE_DATA_REQUEST and skip (async
hand-off to the data lane; NEVER self-heal in-process — that would
couple trade latency to data-repair and contend on the pooler).
See docs/superpowers/specs/2026-05-17-sub-project-b-event-driven-dispatch-design.md.

Wave-3 engine-lane deterministic self-heal (E1 + E9, 2026-05-22)
----------------------------------------------------------------

Two cascade decision points added at the per-engine subprocess seam,
mirroring PR #261's data-lane ``_VALIDATION_CASCADE_MAP`` pattern:

* **E9 — ``ENGINE_IMPORT_FAILED``** (``_pre_check_engine_import``): before
  spawning ``python -m <engine>.scheduler`` we resolve the module spec
  via :func:`importlib.util.find_spec`. A missing / broken engine
  package emits ``ENGINE_IMPORT_FAILED`` to ``platform.application_log``
  and the engine is SKIPPED for the cycle — the sweep continues to the
  next engine. Distinct from a generic subprocess rc≠0 (which is the
  E1 path) and from the supervisor's existing ``scheduler_crash``
  classification (which fires next cycle on an exit_code≠0 SHUTDOWN
  row, here we fire same-cycle BEFORE the subprocess ever runs).

* **E1 — ``ENGINE_STAGE_ESCALATED``** (``_invoke_scheduler_with_recovery``):
  on subprocess rc≠0 we retry ONCE with the same args; if the second
  attempt also returns non-zero, ``ENGINE_STAGE_ESCALATED`` is emitted
  and the engine is skipped (sweep continues — one engine's transient
  failure must NEVER abort engine_service, the same daemon-invariant
  that protects against ``invoke_failed`` raised exceptions).

The pool is threaded into the subprocess seam via the
``_dispatch_pool`` ContextVar (set by ``dispatch_once`` for the
duration of one sweep). Module-globals would have race issues across
concurrent sweeps; the ContextVar is per-task and unwinds cleanly.
"""
from __future__ import annotations

import asyncio
import contextvars
import importlib
import importlib.util
import json
import os
import sys
import uuid
from datetime import UTC, datetime
from typing import Any

import structlog

from ops import aar_autotune, engine_supervisor
from tpcore.db import build_asyncpg_pool
from tpcore.engine_profile import cadence_window_start, roster_for_dispatch, should_fire
from tpcore.quality.validation.capital_gate import failing_sources_for_engine

logger = structlog.get_logger(__name__)

# Engine roster + dispatch ORDER are the engine_profile SoT (the
# dispatch_order field). NEVER re-hardcode — see roster_for_dispatch().
ROSTER: tuple[str, ...] = roster_for_dispatch()


# Wave-3 cascade event names. Distinct from supervisor.HELD/ESCALATED
# (per-engine ladder) and from engine_service.SWEEP_DONE (daemon
# observability). The two new event types here own the engine-lane
# self-heal cascade decision points (E1 + E9).
ENGINE_STAGE_ESCALATED_EVENT: str = "ENGINE_STAGE_ESCALATED"
ENGINE_IMPORT_FAILED_EVENT: str = "ENGINE_IMPORT_FAILED"


# Wave-3 E1 cascade map (mirrors PR #261's _VALIDATION_CASCADE_MAP).
# Per-engine retry tuning lives here; the default applies to every
# engine in ROSTER. A future PR may add per-engine overrides (e.g. a
# very-slow engine like ``catalyst`` may want a longer retry window) —
# right now uniform "retry once" is the canonical spec answer for
# Wave-3.
_DEFAULT_STAGE_CASCADE: dict[str, Any] = {
    "max_attempts": 2,  # one initial + one retry = "retry ONCE" per spec
}
_ENGINE_STAGE_CASCADE_MAP: dict[str, dict[str, Any]] = {
    # Empty by default — every engine uses _DEFAULT_STAGE_CASCADE.
    # Per-engine overrides land here as engines grow distinct
    # retry-tuning needs.
}


# Pool ContextVar — set by dispatch_once for the duration of one sweep.
# The per-engine _safe_invoke reads this to thread the pool down to the
# Wave-3 cascade emit calls without changing _safe_invoke's signature
# (the existing test harness patches _safe_invoke / _invoke_scheduler
# with mocks of the original signature; preserving it keeps every
# existing test green).
_dispatch_pool: contextvars.ContextVar = contextvars.ContextVar(
    "_dispatch_pool", default=None)


_ENGINE_DISPATCH_INSERT_SQL = """
    INSERT INTO platform.application_log
        (engine, run_id, event_type, severity, message, data)
    VALUES ($1, $2, $3, $4, $5, $6::jsonb)
"""


async def _emit_engine_dispatch_event(
    pool: Any,
    engine: str,
    event_type: str,
    severity: str,
    message: str,
    payload: dict[str, Any],
) -> None:
    """Crash-isolated emit of a Wave-3 cascade row to application_log.

    Mirrors ``engine_supervisor._emit`` shape (engine, run_id=uuid4(),
    event_type, severity, message, data::jsonb). An emit failure NEVER
    aborts the sweep — observability is best-effort, the same invariant
    that protects the supervisor's escalate path."""
    if pool is None:
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                _ENGINE_DISPATCH_INSERT_SQL,
                engine, uuid.uuid4(), event_type, severity,
                message, json.dumps(payload, default=str),
            )
    except Exception as exc:  # noqa: BLE001 — observability is best-effort
        logger.warning("engine_dispatch.cascade_emit_failed",
                       engine=engine, event_type=event_type, error=str(exc))


def _pre_check_engine_import(engine: str) -> tuple[bool, str | None]:
    """Wave-3 E9: try to resolve the engine's scheduler module BEFORE
    spawning the subprocess.

    Returns ``(ok, error_repr)``:

    * ``(True, None)`` — module spec resolves cleanly; subprocess spawn
      is safe to proceed.
    * ``(False, repr)`` — ``find_spec`` raised (parent package import
      itself failed) OR returned ``None`` (no such module). The caller
      emits ``ENGINE_IMPORT_FAILED`` and skips the engine.

    The pre-check is a STRUCTURAL guard — it catches the "engine package
    deleted / typo in roster / dependency import-time crash" failure
    BEFORE the subprocess emits a scheduler_crash row (which would
    trigger the supervisor's next-cycle re-invoke loop indefinitely
    against a permanently-broken engine). The supervisor still owns
    the runtime-crash case where the scheduler imports clean but
    crashes after STARTUP.

    Uses ``importlib.util.find_spec`` (NOT ``import``): find_spec
    triggers the parent package's ``__init__.py`` import (so a
    broken-at-import-time engine surfaces here), but does NOT execute
    the scheduler module itself — that's the subprocess's job.
    """
    module_name = f"{engine}.scheduler"
    try:
        spec = importlib.util.find_spec(module_name)
    except Exception as exc:  # noqa: BLE001 — broken parent package surfaces here
        return False, f"{type(exc).__name__}: {exc}"
    if spec is None:
        return False, f"ModuleNotFoundError: No module named {module_name!r}"
    return True, None


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


async def _invoke_scheduler_with_recovery(engine: str) -> int:
    """Wave-3 E1+E9 cascade entry — pre-check import, then retry-once-
    on-rc≠0 around :func:`_invoke_scheduler`.

    Cascade decision points (mirror PR #261's ``_VALIDATION_CASCADE_MAP``
    convention — a map + decision function + named event):

    1. **E9 pre-check**: :func:`_pre_check_engine_import`. On failure,
       emit ``ENGINE_IMPORT_FAILED`` and return rc=127 (the canonical
       "command not found" rc; chosen so downstream observability sees
       a non-zero rc distinct from the engine's own exit codes).
    2. **E1 retry-once**: first ``_invoke_scheduler`` call. On rc≠0,
       log and retry ONCE. On the second rc≠0 emit
       ``ENGINE_STAGE_ESCALATED`` and return the second rc unchanged.

    Pool is read from ``_dispatch_pool`` ContextVar; ``None`` => emits
    become structlog-only no-ops (the same "pool may be None in tests"
    invariant the supervisor uses). The cascade NEVER raises — a
    raised subprocess spawn is caught in :func:`_safe_invoke` upstream
    so an engine's failure cannot abort the sweep (existing CLEANUP #1
    isolation contract preserved).
    """
    pool = _dispatch_pool.get()
    cascade = _ENGINE_STAGE_CASCADE_MAP.get(engine, _DEFAULT_STAGE_CASCADE)
    max_attempts: int = int(cascade.get("max_attempts", 2))

    # E9 — import pre-check.
    ok, err = _pre_check_engine_import(engine)
    if not ok:
        logger.error(
            "engine_dispatch.import_failed", engine=engine, error=err,
        )
        await _emit_engine_dispatch_event(
            pool, engine, ENGINE_IMPORT_FAILED_EVENT, "ERROR",
            f"{engine} import failed — engine skipped this cycle: {err}",
            {"schema": 1, "engine": engine, "error": err,
             "module": f"{engine}.scheduler"},
        )
        return 127  # "command not found" canonical rc

    # E1 — retry-once on rc≠0. Only a non-zero INT rc counts as failure;
    # a None rc (test mocks that don't set a return value via
    # ``AsyncMock(side_effect=lambda e: list.append(...))`` whose lambda
    # implicitly returns None) is treated as "no meaningful rc" → no
    # retry. This is the right semantic because ``proc.wait()`` always
    # returns an int in production; a None rc is by construction a test
    # artifact, not a real failure to react to.
    last_rc: int | None = None
    for attempt in range(1, max_attempts + 1):
        last_rc = await _invoke_scheduler(engine)
        if not isinstance(last_rc, int) or last_rc == 0:
            return last_rc if isinstance(last_rc, int) else 0
        if attempt < max_attempts:
            logger.warning(
                "engine_dispatch.scheduler_stage_retry",
                engine=engine, attempt=attempt,
                max_attempts=max_attempts, returncode=last_rc,
            )
            continue
    # Exhausted — emit ENGINE_STAGE_ESCALATED.
    logger.error(
        "engine_dispatch.scheduler_stage_escalated",
        engine=engine, attempts=max_attempts, returncode=last_rc,
    )
    await _emit_engine_dispatch_event(
        pool, engine, ENGINE_STAGE_ESCALATED_EVENT, "ERROR",
        (f"{engine} scheduler stage escalated after {max_attempts} "
         f"attempt(s): final rc={last_rc}"),
        {"schema": 1, "engine": engine, "attempts": max_attempts,
         "returncode": last_rc},
    )
    return last_rc


_REQUEST_EVENT = "ENGINE_DATA_REQUEST"
_TERMINAL_EVENTS = ("DATA_REPAIR_COMPLETE", "DATA_REPAIR_ESCALATED")

_NO_TERMINAL_TIMEOUT_SECONDS = int(
    os.environ.get("ENGINE_DISPATCH_REQUEST_TIMEOUT_SECONDS", "5400"))  # 90 min (spec §6)


async def _open_request_state(conn, engine: str, window_start: datetime) -> dict | None:
    """Latest ENGINE_DATA_REQUEST for engine in this cadence window +
    its terminal event (if any). None if no request this window."""
    return await conn.fetchrow(
        """
        SELECT r.data->>'request_id' AS request_id,
               r.recorded_at         AS req_ts,
               t.event_type          AS terminal,
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


async def _safe_supervise(pool, engine: str, now: datetime, invoke) -> None:
    """Call the supervisor with call-site crash isolation (defense in
    depth — supervise() is already internally isolated; a broken
    supervisor must NEVER abort the sweep, DA-1 §2/§10)."""
    try:
        await engine_supervisor.supervise(pool, engine, now, invoke)
    except Exception as exc:  # noqa: BLE001 — never abort the sweep
        logger.error("engine_dispatch.supervisor_failed", engine=engine,
                     error=str(exc))


async def _safe_autotune(pool, engine: str, now: datetime) -> None:
    """Call the behavioral auto-tune with call-site crash isolation
    (defense in depth — autotune() is already internally isolated; a
    broken autotune must NEVER abort the sweep, DA-2 §9). No `invoke`:
    behavioral holds have no self-heal."""
    try:
        await aar_autotune.autotune(pool, engine, now)
    except Exception as exc:  # noqa: BLE001 — never abort the sweep
        logger.error("engine_dispatch.autotune_failed", engine=engine,
                     error=str(exc))


async def _safe_invoke(engine: str) -> None:
    """Spawn one engine's scheduler with per-engine crash isolation
    (CLEANUP #1, deferred from T2). A raising subprocess spawn (OSError
    et al.) must NOT abort the sweep — mirror the old bash ``|| continue``.

    Wave-3 (2026-05-22): routes through
    :func:`_invoke_scheduler_with_recovery` so an rc≠0 first attempt is
    retried once (E1) and a missing engine package is caught before
    spawning the subprocess (E9). The CLEANUP-#1 try/except still wraps
    everything — a raising recovery helper (e.g. an emit raising mid-
    cascade) must NEVER abort the sweep.
    """
    try:
        await _invoke_scheduler_with_recovery(engine)
    except Exception as exc:  # noqa: BLE001 — isolate one engine's failure
        logger.error("engine_dispatch.invoke_failed", engine=engine,
                     error=str(exc))


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
        # DA-1: crashed-STARTUP re-invoke is owned by engine_supervisor
        # (ran above, before should_fire). Here we only record the skip.
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


async def dispatch_once(pool, now: datetime) -> None:
    # Wave-3: set the per-sweep pool ContextVar so the per-engine
    # cascade (_invoke_scheduler_with_recovery) can emit ENGINE_STAGE_
    # ESCALATED / ENGINE_IMPORT_FAILED rows without needing the pool
    # threaded through _safe_invoke's signature (preserves the existing
    # test harness that patches _safe_invoke/_invoke_scheduler with
    # zero-arg mocks).
    token = _dispatch_pool.set(pool)
    try:
        await _dispatch_allocator(pool, now)
        for engine in ROSTER:
            await _safe_supervise(pool, engine, now, _safe_invoke)
            await _safe_autotune(pool, engine, now)
            await _dispatch_engine(pool, now, engine, _safe_invoke)
    finally:
        _dispatch_pool.reset(token)


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
