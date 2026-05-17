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


async def _maybe_clear_behavioral(pool, engine: str) -> None:
    """Operator-only behavioral clear (spec §5). If the engine has an
    uncleared behavioral hold, clear it iff NO HOLD-eligible trigger
    remains open — re-evaluate the §3 HOLD condition against
    currently-open triggers (NOT a stale fingerprint match; the
    operator resolves triggers via forensics_triggers.resolved_at, so
    they leave _open_triggers). A NEWER hold-eligible trigger keeps it
    held (guards against premature resume)."""
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
