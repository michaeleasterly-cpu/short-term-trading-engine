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

from tpcore.engine_profile import cadence_window_start, profile_for
from tpcore.supervisor_state import (
    CLEARED_EVENT,
    ESCALATED_EVENT,
    HELD_EVENT,
    RECOVERED_EVENT,
    SCHEMA_VERSION,
    current_hold,
)

logger = structlog.get_logger(__name__)

_MAX_REINVOKE = int(os.environ.get("ENGINE_SUPERVISOR_MAX_REINVOKE", "2"))
_MISSED_CYCLES_N = int(os.environ.get("ENGINE_SUPERVISOR_MISSED_CYCLES", "2"))
_STALE_STARTUP_SECONDS = int(
    os.environ.get("ENGINE_DISPATCH_STALE_STARTUP_SECONDS", "7200"))  # 2h

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
