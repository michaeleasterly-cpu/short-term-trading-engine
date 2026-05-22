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
        (engine, run_id, event_type, severity, message, data)
    VALUES
        ('llm_edge_finder', $1, 'LAB_FINDER_RUN', 'INFO',
         'finder run completed', $2::jsonb)
"""


async def record_finder_run(pool: asyncpg.Pool, run: FinderRun) -> None:
    """Write one LAB_FINDER_RUN row + matching LAB_FINDER_ACTION rows.

    Per spec §4.6 + §2.16 (provenance is non-negotiable).
    """
    payload_json = run.model_dump_json()
    async with pool.acquire() as conn:
        await conn.execute(_FINDER_RUN_INSERT_SQL, run.run_id, payload_json)
    log.info(
        "finder_run.recorded",
        run_id=str(run.run_id),
        trigger=run.trigger,
        emissions=run.proposed_spec_count,
    )


_FINDER_ACTION_INSERT_SQL = """
    INSERT INTO platform.application_log
        (engine, run_id, event_type, severity, message, data)
    VALUES
        ('llm_edge_finder', $1, 'LAB_FINDER_ACTION', 'INFO',
         $2, $3::jsonb)
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
    # The application_log run_id column is UUID; convert from str defensively.
    # Bare string run_id (e.g. "(monitor)" from Phase E) → use NIL UUID.
    import uuid as _uuid
    try:
        rid_uuid = _uuid.UUID(run_id)
    except (ValueError, AttributeError):
        rid_uuid = _uuid.UUID(int=0)
    message = f"{action} triggered_by={triggered_by}"
    async with pool.acquire() as conn:
        await conn.execute(
            _FINDER_ACTION_INSERT_SQL, rid_uuid, message, json.dumps(payload)
        )
    log.info(
        "finder_action.recorded",
        run_id=run_id,
        action=action,
        triggered_by=triggered_by,
    )


_FINDER_EMISSION_INSERT_SQL = """
    INSERT INTO platform.application_log
        (engine, run_id, event_type, severity, message, data)
    VALUES
        ('llm_edge_finder', $1, 'LAB_FINDER_EMISSION', 'INFO',
         $2, $3::jsonb)
"""


async def record_finder_emission(
    pool: asyncpg.Pool,
    *,
    run_id: str,
    spec_index: int,
    spec_dict: dict,
) -> None:
    """Write ONE LAB_FINDER_EMISSION row per ProposedSpec the LLM emitted.

    The operator inspects these to judge structural distinctness against
    the 4 failed deep-research candidates (per spec §10.6.b gate pilot).
    Each row carries the FULL ProposedSpec content (rationale, hypothesis,
    falsification, evidence refs) as JSON in the `data` column.
    """
    import uuid as _uuid
    try:
        rid_uuid = _uuid.UUID(run_id)
    except (ValueError, AttributeError):
        rid_uuid = _uuid.UUID(int=0)
    candidate = spec_dict.get("candidate_name", "?")
    target = spec_dict.get("target_engine", "?")
    message = f"emission[{spec_index}] candidate={candidate} target={target}"
    async with pool.acquire() as conn:
        await conn.execute(
            _FINDER_EMISSION_INSERT_SQL, rid_uuid, message, json.dumps(spec_dict)
        )
    log.info(
        "finder_emission.recorded",
        run_id=run_id,
        spec_index=spec_index,
        candidate=candidate,
        target=target,
    )


__all__ = [
    "record_finder_action",
    "record_finder_emission",
    "record_finder_run",
]
