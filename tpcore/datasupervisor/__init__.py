"""Data Supervisor — per-source hold + autonomous auto-clear.

Data-native symmetric counterpart of the engine-lane DA-1 supervisor
(tpcore/supervisor_state.py + ops/engine_supervisor.py). NOT a copy:
per-source (not per-engine); consumes the rung-1 escalations
selfheal/auditheal/contract-sentinel already emit (does not re-heal);
the sacred whole-cycle emit gate is untouched (no new gate).
"""
from tpcore.datasupervisor.state import (
    CLEARED_EVENT,
    ESCALATED_EVENT,
    HELD_EVENT,
    RECOVERED_EVENT,
    SCHEMA_VERSION,
    SourceHoldState,
    current_source_hold,
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
