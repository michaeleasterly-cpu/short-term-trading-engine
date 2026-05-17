"""The generic audit-heal orchestrator — zero check-specific logic.
Mirrors tpcore.selfheal.orchestrator.

Flow (bounded): re-run the structured cross-table audit (refreshes
``cross_table_audit.*`` rows) -> read the red set -> all green: done
-> map each red to its RemediationSpec; any unremediable/unknown red
-> escalate the full picture now -> else run each distinct canonical
remediation (injected run_stage) -> loop up to max_iterations -> still
red -> escalate ("exhausted"). ``run_audit`` and ``run_stage`` are
injected so the engine is pure + unit-testable.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import structlog
from pydantic import BaseModel, ConfigDict, Field

from .registry import spec_for

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)

RunStage = Callable[[str, dict[str, str]], Awaitable[int]]
RunAudit = Callable[[], Awaitable[int]]

DEFAULT_MAX_ITERATIONS = 4

_RED_SQL = """
    WITH latest AS (
        SELECT source, MAX(timestamp) AS t
        FROM platform.data_quality_log
        WHERE source LIKE 'cross_table_audit.%'
        GROUP BY source
    )
    SELECT q.source
    FROM platform.data_quality_log q
    JOIN latest l ON l.source = q.source AND l.t = q.timestamp
    WHERE q.stale OR (q.confidence IS NOT NULL AND q.confidence < 1.0)
    ORDER BY q.source
"""


class AuditHealOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    green: bool
    iterations: int
    remediated: list[str] = Field(default_factory=list)
    escalated: list[tuple[str, str]] = Field(default_factory=list)


def _source_to_key(source: str) -> str:
    rest = source.removeprefix("cross_table_audit.")
    table, _, check = rest.partition(".")
    return f"{table}/{check}"


async def _red_keys(pool: asyncpg.Pool) -> list[str]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(_RED_SQL)
    return [_source_to_key(r["source"]) for r in rows]


async def run_audit_heal(
    pool: asyncpg.Pool,
    run_stage: RunStage,
    run_audit: RunAudit,
    *,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
) -> AuditHealOutcome:
    """Drive the cross-table layer to 100% green or honest escalation."""
    remediated: list[str] = []

    for iteration in range(1, max_iterations + 1):
        arc = await run_audit()
        if arc != 0:
            logger.error("auditheal.audit_failed", rc=arc, iteration=iteration)
            return AuditHealOutcome(
                green=False, iterations=iteration, remediated=remediated,
                escalated=[("cross_table_audit",
                            f"structured audit exited {arc}")],
            )

        reds = await _red_keys(pool)
        if not reds:
            logger.info("auditheal.green", iterations=iteration,
                        remediated=remediated)
            return AuditHealOutcome(green=True, iterations=iteration,
                                    remediated=remediated)

        unremediable: list[tuple[str, str]] = []
        actions: dict[tuple[str, frozenset], tuple[str, dict[str, str]]] = {}
        for key in reds:
            spec = spec_for(key)
            if spec is None:
                unremediable.append(
                    (key, "unknown cross-table red — no RemediationSpec "
                          "(never silently ignored; add a spec)"))
            elif not spec.remediable:
                unremediable.append((key, f"{key}: {spec.escalate_reason}"))
            else:
                k = (spec.stage, frozenset(spec.params.items()))
                actions[k] = (spec.stage, spec.params)

        if unremediable:
            logger.warning("auditheal.escalate_unremediable",
                           iteration=iteration, unremediable=unremediable)
            return AuditHealOutcome(
                green=False, iterations=iteration, remediated=remediated,
                escalated=unremediable,
            )

        for stage, params in actions.values():
            logger.info("auditheal.remediate", stage=stage, params=params,
                        iteration=iteration)
            hrc = await run_stage(stage, params)
            if hrc != 0:
                logger.error("auditheal.remediation_failed", stage=stage,
                             rc=hrc)
                return AuditHealOutcome(
                    green=False, iterations=iteration,
                    remediated=remediated,
                    escalated=[(stage, f"bounded remediation exited {hrc} "
                                       "— cannot heal through a failing "
                                       "remediation")],
                )
            remediated.append(stage)

    final = await _red_keys(pool)
    logger.error("auditheal.exhausted", iterations=max_iterations,
                 still_red=final)
    return AuditHealOutcome(
        green=False, iterations=max_iterations, remediated=remediated,
        escalated=[(k, f"auto-remediation exhausted after "
                       f"{max_iterations} iterations") for k in final],
    )


__all__ = [
    "DEFAULT_MAX_ITERATIONS",
    "AuditHealOutcome",
    "RunAudit",
    "RunStage",
    "run_audit_heal",
]
