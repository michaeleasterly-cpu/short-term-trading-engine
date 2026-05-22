"""AARCriticRun + AARFinding persistence — spec §3.2.

Writes one append-only ``LAB_AAR_CRITIC_RUN`` event per run + one
``LAB_AAR_CRITIC_FINDING`` event per finding to ``platform.application_log``.

Schema reuses the existing ``application_log`` table; no migration:
- engine = 'llm_aar_critic'
- event_type ∈ {'LAB_AAR_CRITIC_RUN', 'LAB_AAR_CRITIC_FINDING'}
- payload carries the full pydantic model_dump

Mirrors ``tpcore/lab/llm_finder/run_writer.py`` discipline.
"""
from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING

import structlog

from tpcore.lab.llm_aar.models import AARCriticRun, AARFinding

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

log = structlog.get_logger(__name__)


_AAR_CRITIC_RUN_INSERT_SQL = """
    INSERT INTO platform.application_log
        (engine, run_id, event_type, severity, message, data)
    VALUES
        ('llm_aar_critic', $1, 'LAB_AAR_CRITIC_RUN', 'INFO',
         'aar-critic run completed', $2::jsonb)
"""


async def record_aar_critic_run(
    pool: asyncpg.Pool, run: AARCriticRun
) -> None:
    """Write one LAB_AAR_CRITIC_RUN provenance row.

    Per spec §3.2 — provenance is non-negotiable. Fail-loud on DB error
    (caller's responsibility to wrap in retry/defer if needed).
    """
    payload_json = run.model_dump_json()
    async with pool.acquire() as conn:
        await conn.execute(_AAR_CRITIC_RUN_INSERT_SQL, run.run_id, payload_json)
    log.info(
        "aar_critic_run.recorded",
        run_id=str(run.run_id),
        trigger=run.trigger,
        engines=len(run.engines_examined),
        findings=len(run.findings_emitted),
    )


_AAR_CRITIC_FINDING_INSERT_SQL = """
    INSERT INTO platform.application_log
        (engine, run_id, event_type, severity, message, data)
    VALUES
        ('llm_aar_critic', $1, 'LAB_AAR_CRITIC_FINDING', 'INFO',
         $2, $3::jsonb)
"""


async def record_aar_finding(
    pool: asyncpg.Pool,
    *,
    run_id: str,
    finding: AARFinding,
) -> None:
    """Write one LAB_AAR_CRITIC_FINDING row per emitted finding.

    The operator inspects findings via application_log queries OR via
    the §12 audit dashboard (if extended for AAR findings — out of v1
    scope but the data is available).
    """
    try:
        rid_uuid = uuid.UUID(run_id)
    except (ValueError, AttributeError):
        rid_uuid = uuid.UUID(int=0)
    message = (
        f"finding engine={finding.engine} theme={finding.theme} "
        f"confidence={finding.confidence} finding_id={finding.finding_id}"
    )
    payload_json = json.dumps(finding.model_dump(mode="json"))
    async with pool.acquire() as conn:
        await conn.execute(
            _AAR_CRITIC_FINDING_INSERT_SQL,
            rid_uuid,
            message,
            payload_json,
        )
    log.info(
        "aar_critic_finding.recorded",
        run_id=run_id,
        finding_id=finding.finding_id,
        engine=finding.engine,
        theme=finding.theme,
        confidence=finding.confidence,
    )


_RUN_COUNT_SQL = """
    SELECT COUNT(*)::int
    FROM platform.application_log
    WHERE engine = 'llm_aar_critic'
      AND event_type = 'LAB_AAR_CRITIC_RUN'
      AND created_at >= $1::timestamp
      AND created_at < $2::timestamp
"""


async def count_runs_in_utc_day(
    pool: asyncpg.Pool, utc_day_start: str, utc_day_end: str
) -> int:
    """Return the number of LAB_AAR_CRITIC_RUN rows in the half-open interval.

    Used by the rate ceiling check (spec §5.4 — MAX_AAR_CRITIC_RUNS_PER_DAY).

    Args:
        pool: asyncpg pool.
        utc_day_start: ISO timestamp for the start of the UTC day.
        utc_day_end: ISO timestamp for the end of the UTC day.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchval(_RUN_COUNT_SQL, utc_day_start, utc_day_end)
    return int(row or 0)


__all__ = [
    "count_runs_in_utc_day",
    "record_aar_critic_run",
    "record_aar_finding",
]
