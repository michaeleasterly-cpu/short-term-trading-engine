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
    CLEARED_EVENT,
    ESCALATED_EVENT,
    HELD_EVENT,
    RECOVERED_EVENT,
    SCHEMA_VERSION,
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
