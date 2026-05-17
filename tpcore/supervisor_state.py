"""Engine-supervisor inter-lane vocabulary + event-sourced hold read.

Pure tpcore (NO ops import): `should_fire` (tpcore) and the supervisor
(ops) both read hold state through `current_hold`. The supervisor
(ops/engine_supervisor.py) is the sole WRITER of these events; this
module only defines the locked vocabulary and the read.

Locked contract (schema:1, parity with ENGINE_DATA_REQUEST /
DATA_REPAIR_*): `hold_id` is a uuid4 string, the sole correlation
key; NO client timestamps in payloads (DB `recorded_at` only);
one-terminal liveness — an ENGINE_HELD is eventually followed by
exactly one ENGINE_CLEARED.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

SCHEMA_VERSION = 1

HELD_EVENT = "ENGINE_HELD"
CLEARED_EVENT = "ENGINE_CLEARED"
ESCALATED_EVENT = "ENGINE_ESCALATED"
RECOVERED_EVENT = "ENGINE_SUPERVISOR_RECOVERED"


@dataclass(frozen=True)
class HoldState:
    """An engine's currently-open supervisor hold."""

    hold_id: str
    failure_class: str
    reason: str
    held_at: datetime


async def current_hold(pool, engine: str) -> HoldState | None:
    """The engine's open hold, or None.

    Latest ENGINE_HELD for ``engine`` whose ``hold_id`` has no later
    ENGINE_CLEARED. Mirrors engine_dispatch._open_request_state's
    request/terminal LEFT JOIN, keyed on hold_id.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT h.data->>'hold_id'        AS hold_id,
                   h.data->>'failure_class'  AS failure_class,
                   h.data->>'reason'         AS reason,
                   h.recorded_at             AS held_at,
                   c.event_type              AS cleared
            FROM platform.application_log h
            LEFT JOIN platform.application_log c
              ON c.event_type = $2
             AND (c.data->>'hold_id') = (h.data->>'hold_id')
            WHERE h.event_type = $1 AND h.engine = $3
            ORDER BY h.recorded_at DESC LIMIT 1
            """,
            HELD_EVENT, CLEARED_EVENT, engine,
        )
    if row is None or row["cleared"] is not None:
        return None
    return HoldState(
        hold_id=row["hold_id"],
        failure_class=row["failure_class"],
        reason=row["reason"],
        held_at=row["held_at"],
    )
