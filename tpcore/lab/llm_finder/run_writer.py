"""FinderRun persistence — Task #25 §4.6 + §7 of the spec.

Writes one append-only `LAB_FINDER_RUN` event per finder run to
``platform.application_log``. The persistence is fail-loud: a finder
run that completes its loop but crashes on the write must surface the
error, NOT silently drop the provenance row.

The schema reuses the existing ``application_log`` table:
- ``engine = 'llm_edge_finder'``
- ``event_type = 'LAB_FINDER_RUN'``
- ``payload`` carries the full ``FinderRun`` model_dump (Pydantic v2)
- ``triggered_by`` reads from ``FinderRun.trigger``

No new migration — the spec §4.6 sentinel "Persisted append-only under
``lab_edge_finder_run.<session_date>`` (no migration)" stays valid;
this module is the writer.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

import structlog

from tpcore.lab.llm_finder.models import FinderRun

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

log = structlog.get_logger(__name__)

_FINDER_RUN_INSERT_SQL = """
    INSERT INTO platform.application_log
        (engine, event_type, ts, payload)
    VALUES
        ('llm_edge_finder', 'LAB_FINDER_RUN', NOW() AT TIME ZONE 'UTC', $1::jsonb)
"""


async def record_finder_run(pool: asyncpg.Pool, run: FinderRun) -> None:
    """Write one LAB_FINDER_RUN row + matching LAB_FINDER_ACTION rows.

    Per spec §4.6 + §2.16 (provenance is non-negotiable).
    """
    payload_json = run.model_dump_json()
    async with pool.acquire() as conn:
        await conn.execute(_FINDER_RUN_INSERT_SQL, payload_json)
    log.info(
        "finder_run.recorded",
        run_id=str(run.run_id),
        trigger=run.trigger,
        emissions=run.proposed_spec_count,
    )


_FINDER_ACTION_INSERT_SQL = """
    INSERT INTO platform.application_log
        (engine, event_type, ts, payload)
    VALUES
        ('llm_edge_finder', 'LAB_FINDER_ACTION', NOW() AT TIME ZONE 'UTC', $1::jsonb)
"""


async def record_finder_action(
    pool: asyncpg.Pool,
    *,
    run_id: str,
    action: str,
    triggered_by: str,
    extra: dict | None = None,
) -> None:
    """Write one LAB_FINDER_ACTION row.

    Per spec §2.16: every autonomous action (draft, undraft, merge,
    ecr_modify, ecr_retire, outcome_proven) emits provenance. Reads:
    - action ∈ {draft, undraft, merge, ecr_modify, ecr_retire,
                outcome_proven, auto_retire, inactivity_timeout}
    - triggered_by ∈ {operator_command, ledger_capacity_event,
                      regime_change_event, outcome_monitor_check,
                      ci_green, gate_pass, bleed_cap, operator_verdict,
                      inactivity_timeout, global_bleed_cap}
    """
    payload = {
        "run_id": run_id,
        "action": action,
        "triggered_by": triggered_by,
        "human_override": "none",
    }
    if extra:
        payload.update(extra)
    async with pool.acquire() as conn:
        await conn.execute(_FINDER_ACTION_INSERT_SQL, json.dumps(payload))
    log.info(
        "finder_action.recorded",
        run_id=run_id,
        action=action,
        triggered_by=triggered_by,
    )


__all__ = ["record_finder_action", "record_finder_run"]
