"""Data-supervisor pass — bounded per-source hold open/clear/escalate.

Consumes the EXISTING red predicates verbatim from selfheal and auditheal
orchestrators. Does NOT re-heal. Landed dark: no caller wired yet.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

import structlog

from tpcore.auditheal.orchestrator import (
    _RED_SQL as _CT_RED_SQL,
)
from tpcore.auditheal.orchestrator import (
    _source_to_key as _ct_key,
)
from tpcore.datasupervisor.state import (
    CLEARED_EVENT,
    ESCALATED_EVENT,
    HELD_EVENT,
    RECOVERED_EVENT,
    SCHEMA_VERSION,
    current_source_hold,
)
from tpcore.selfheal.orchestrator import _RED_SQL as _VAL_RED_SQL
from tpcore.selfheal.registry import spec_for

logger = structlog.get_logger(__name__)

_ENGINE_TAG = "datasupervisor"
_MAX_HELD_CYCLES = 3

_INSERT_SQL = (
    "INSERT INTO platform.application_log "
    "(engine, run_id, event_type, severity, message, data) "
    "VALUES ($1,$2,$3,$4,$5,$6::jsonb)"
)

_FEED_RE = re.compile(r"feed=(['\"]?)([a-z0-9_]+)\1")

_CONTRACT_RED_SQL = """
    SELECT data->>'error' AS error
    FROM platform.application_log
    WHERE event_type = 'INGESTION_FAILED'
      AND exception_type = 'AdapterContractDrift'
      AND recorded_at > NOW() - INTERVAL '24 hours'
"""

_OPEN_HOLD_SQL = """
    SELECT h.data->>'source' AS source
    FROM platform.application_log h
    LEFT JOIN platform.application_log c
      ON c.event_type = 'DATA_SOURCE_CLEARED'
     AND (c.data->>'hold_id') = (h.data->>'hold_id')
    WHERE h.event_type = 'DATA_SOURCE_HELD' AND c.event_type IS NULL
"""


def _healspec_source(check_name: str) -> str | None:
    """Return the HealSpec.source for a validation check name, or None."""
    spec = spec_for(check_name)
    return spec.source if spec is not None else None


async def _red_sources(pool: Any) -> set[str]:
    """All currently-red source keys, prefixed by lane."""
    result: set[str] = set()
    async with pool.acquire() as conn:
        # Validation lane
        for row in await conn.fetch(_VAL_RED_SQL):
            check = row["source"].removeprefix("validation.")
            src = _healspec_source(check)
            if src is not None:
                result.add(f"validation:{src}")

        # Cross-table audit lane
        for row in await conn.fetch(_CT_RED_SQL):
            table = _ct_key(row["source"]).split("/")[0]
            result.add(f"cross_table:{table}")

        # Adapter contract drift lane
        for row in await conn.fetch(_CONTRACT_RED_SQL):
            m = _FEED_RE.search(row["error"] or "")
            if m:
                result.add(f"contract:{m.group(2)}")

    return result


async def _open_hold_sources(pool: Any) -> set[str]:
    """Real open-hold discovery: sources with a HELD event but no CLEARED."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(_OPEN_HOLD_SQL)
    return {r["source"] for r in rows if r["source"]}


async def _held_cycles(pool: Any, held_at: Any) -> int:
    """Count distinct INGESTION_START run_ids since the hold was opened."""
    async with pool.acquire() as conn:
        n = await conn.fetchval(
            "SELECT COUNT(DISTINCT run_id) AS n "
            "FROM platform.application_log "
            "WHERE event_type='INGESTION_START' AND recorded_at > $1",
            held_at,
        )
    return n or 0


async def _emit(
    pool: Any,
    event_type: str,
    message: str,
    data: dict,
    *,
    severity: str = "INFO",
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            _INSERT_SQL,
            _ENGINE_TAG,
            str(uuid.uuid4()),
            event_type,
            severity,
            message,
            json.dumps(data, default=str),
        )


@dataclass
class DataSupervisorOutcome:
    opened: list[str] = field(default_factory=list)
    cleared: list[str] = field(default_factory=list)
    escalated: list[str] = field(default_factory=list)
    error: str | None = None


async def datasupervise(pool: Any, run_id: str) -> DataSupervisorOutcome:
    """Run one supervisor pass — open/clear/escalate per-source holds."""
    out = DataSupervisorOutcome()
    try:
        # Step 1: gather red sources this cycle
        red = await _red_sources(pool)

        # Step 2: open a hold for each newly-red source (idempotent)
        for source in sorted(red):
            hold = await current_source_hold(pool, source)
            if hold is None:
                hold_id = str(uuid.uuid4())
                await _emit(
                    pool,
                    HELD_EVENT,
                    f"datasupervisor: hold opened for {source}",
                    {
                        "schema": SCHEMA_VERSION,
                        "hold_id": hold_id,
                        "source": source,
                        "reason": f"{source} red post Step-4/4c",
                    },
                    severity="WARNING",
                )
                out.opened.append(source)

        # Step 3: probe = union of currently-red + sources with open holds
        open_held = await _open_hold_sources(pool)
        probe = red | open_held

        for source in sorted(probe):
            hold = await current_source_hold(pool, source)
            if hold is None:
                continue

            if source not in red:
                # Source recovered — auto-clear
                await _emit(
                    pool,
                    CLEARED_EVENT,
                    f"datasupervisor: auto-clear {source}",
                    {
                        "schema": SCHEMA_VERSION,
                        "hold_id": hold.hold_id,
                        "source": source,
                        "clear_reason": "source green after hold (autonomous auto-clear)",
                    },
                )
                await _emit(
                    pool,
                    RECOVERED_EVENT,
                    f"datasupervisor: recovered {source}",
                    {
                        "schema": SCHEMA_VERSION,
                        "source": source,
                    },
                )
                out.cleared.append(source)
            else:
                # Still red — check bounded escalation
                n = await _held_cycles(pool, hold.held_at)
                if n >= _MAX_HELD_CYCLES:
                    await _emit(
                        pool,
                        ESCALATED_EVENT,
                        f"datasupervisor: escalate {source} after {n} held cycles",
                        {
                            "schema": SCHEMA_VERSION,
                            "hold_id": hold.hold_id,
                            "source": source,
                            "held_cycles": n,
                            "reason": f"still red after {_MAX_HELD_CYCLES} held cycles",
                        },
                        severity="ERROR",
                    )
                    out.escalated.append(source)

    except Exception as exc:  # noqa: BLE001
        logger.error("datasupervisor.error", error=str(exc))
        out.error = str(exc)

    return out


__all__ = ["DataSupervisorOutcome", "datasupervise"]
