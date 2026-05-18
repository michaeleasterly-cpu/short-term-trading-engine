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
_NO_TERMINAL_TIMEOUT_SECONDS = int(
    os.environ.get("ENGINE_DISPATCH_REQUEST_TIMEOUT_SECONDS", "5400"))

# The DA-1 infra failure-class SoT (the engine-ladder R2 clockwork
# pins _classify's emittable set + the disposition registry to this).
INFRA_FAILURE_CLASSES: frozenset[str] = frozenset({
    "crashed_startup", "scheduler_crash", "data_request_timeout",
    "data_repair_escalated", "missed_cycle"})

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
    # DA-2 seam guard: DA-1 only clears the infra classes it created.
    # Behavioral holds (failure_class="behavioral") are DA-2-owned and
    # operator-cleared — DA-1 must never auto-resume them.
    if hold.failure_class not in INFRA_FAILURE_CLASSES:
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
    if row is None or row.get("req_ts") is None:
        return False
    if row.get("terminal") is not None:
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
    """No STARTUP across the last N eligible windows (silent death).

    Held-window exclusion is enforced STRUCTURALLY by the caller, NOT
    by this SQL: `_detect_and_act` short-circuits to `_auto_clear` and
    returns when `current_hold(...)` is not None, so `_classify` (and
    therefore this detector) is unreachable while the engine is held —
    a held engine is intentionally idle and must never be counted as a
    missed cycle (else held → no STARTUP → missed_cycle → re-invoke
    loop). This query performs NO held-window filtering itself;
    `eligible_windows` is simply the count of distinct recorded days
    observed since `window_start`. Do NOT rely on a SQL-level held
    filter here — the safety is the caller short-circuit plus the
    clean-cycle clear guaranteeing a post-hold STARTUP exists."""
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
