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
import json
import os
import sys
import uuid
from datetime import UTC, datetime

import structlog

from ops import engine_supervisor
from tpcore.db import build_asyncpg_pool
from tpcore.engine_profile import cadence_window_start, should_fire
from tpcore.quality.validation.capital_gate import failing_sources_for_engine

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


async def _safe_invoke(engine: str) -> None:
    """Spawn one engine's scheduler with per-engine crash isolation
    (CLEANUP #1, deferred from T2). A raising subprocess spawn (OSError
    et al.) must NOT abort the sweep — mirror the old bash ``|| continue``.
    """
    try:
        await _invoke_scheduler(engine)
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
    await _dispatch_engine(pool, now, "allocator", _invoke_allocator)


async def dispatch_once(pool, now: datetime) -> None:
    await _dispatch_allocator(pool, now)
    for engine in ROSTER:
        await _safe_supervise(pool, engine, now, _safe_invoke)
        await _dispatch_engine(pool, now, engine, _safe_invoke)


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
