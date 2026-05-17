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
