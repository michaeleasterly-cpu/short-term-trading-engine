"""The generic self-heal orchestrator — zero source-specific logic.

Flow (bounded):

1. Run the canonical ``data_validation`` stage (refreshes
   ``data_quality_log`` via the real suite + real sources — one source
   of truth, no fixture divergence).
2. Read the red ``validation.*`` checks from ``data_quality_log``.
3. All green → done.
4. Map each red check → its :class:`HealSpec`. If ANY red is
   unhealable (no spec, or ``healable=False``) → escalate immediately:
   the suite cannot reach green this cycle and a human is required;
   healing the rest would waste time and the gate stays red anyway.
   Report every red with its disposition.
5. Otherwise run each distinct healable repair (canonical
   ``ops.py --stage`` via the injected ``run_stage``). Any non-zero
   exit → escalate (can't self-heal through a failing repair).
6. Re-validate; loop up to ``max_iterations``. Still red after that →
   escalate ("auto-heal exhausted").

``run_stage`` is injected (``Callable[[str, dict[str,str]],
Awaitable[int]]`` → process/stage exit code) so the engine is pure and
unit-testable; production injects the canonical ``ops.py --stage``
subprocess runner (``tpcore.selfheal.runner``). The orchestrator NEVER
shells out or reimplements ingestion itself.
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

VALIDATION_STAGE = "data_validation"
DEFAULT_MAX_ITERATIONS = 4

_RED_SQL = """
    WITH latest AS (
        SELECT source, MAX(timestamp) AS t
        FROM platform.data_quality_log
        WHERE source LIKE 'validation.%'
        GROUP BY source
    )
    SELECT q.source
    FROM platform.data_quality_log q
    JOIN latest l ON l.source = q.source AND l.t = q.timestamp
    WHERE q.stale OR (q.confidence IS NOT NULL AND q.confidence < 1.0)
    ORDER BY q.source
"""


class SelfHealOutcome(BaseModel):
    """Structured result. ``green`` is the ONLY thing the wrapper needs
    to decide whether to emit DATA_OPERATIONS_COMPLETE."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    green: bool
    iterations: int
    healed: list[str] = Field(default_factory=list)
    # (source, reason) — everything a human must look at, by-design or bug.
    escalated: list[tuple[str, str]] = Field(default_factory=list)


async def _red_checks(pool: asyncpg.Pool) -> list[str]:
    """Bare validation check names currently red (suite-written)."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(_RED_SQL)
    return [r["source"].removeprefix("validation.") for r in rows]


async def run_self_heal(
    pool: asyncpg.Pool,
    run_stage: RunStage,
    *,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
) -> SelfHealOutcome:
    """Drive the data layer to 100% green or an honest escalation."""
    healed: list[str] = []

    for iteration in range(1, max_iterations + 1):
        rc = await run_stage(VALIDATION_STAGE, {})
        if rc != 0:
            logger.error("selfheal.validation_stage_failed", rc=rc, iteration=iteration)
            return SelfHealOutcome(
                green=False, iterations=iteration, healed=healed,
                escalated=[(VALIDATION_STAGE, f"validation stage exited {rc}")],
            )

        reds = await _red_checks(pool)
        if not reds:
            logger.info("selfheal.green", iterations=iteration, healed=healed)
            return SelfHealOutcome(green=True, iterations=iteration, healed=healed)

        # Resolve dispositions.
        unhealable: list[tuple[str, str]] = []
        actions: dict[tuple[str, frozenset], tuple[str, dict[str, str]]] = {}
        for check in reds:
            spec = spec_for(check)
            if spec is None:
                unhealable.append(
                    (check, "unknown red — no HealSpec in registry (must "
                            "never be silently ignored; add a spec)"))
            elif not spec.healable:
                unhealable.append((spec.source, f"{check}: {spec.unhealable_reason}"))
            else:
                key = (spec.stage, frozenset(spec.params.items()))
                actions[key] = (spec.stage, spec.params)

        if unhealable:
            # A human is needed regardless — escalate the full picture
            # now rather than burn heal cycles that can't reach green.
            logger.warning(
                "selfheal.escalate_unhealable",
                iteration=iteration, unhealable=unhealable,
                deferred_healable=[a[0] for a in actions.values()],
            )
            return SelfHealOutcome(
                green=False, iterations=iteration, healed=healed,
                escalated=unhealable,
            )

        # Run each distinct bounded canonical repair.
        for stage, params in actions.values():
            logger.info("selfheal.repair", stage=stage, params=params, iteration=iteration)
            hrc = await run_stage(stage, params)
            if hrc != 0:
                logger.error("selfheal.repair_failed", stage=stage, rc=hrc)
                return SelfHealOutcome(
                    green=False, iterations=iteration, healed=healed,
                    escalated=[(stage, f"bounded repair exited {hrc} — cannot "
                                       "self-heal through a failing repair")],
                )
            healed.append(stage)

    # Exhausted: still red after max_iterations.
    final_reds = await _red_checks(pool)
    logger.error("selfheal.exhausted", iterations=max_iterations, still_red=final_reds)
    return SelfHealOutcome(
        green=False, iterations=max_iterations, healed=healed,
        escalated=[(c, f"auto-heal exhausted after {max_iterations} iterations")
                   for c in final_reds],
    )


__all__ = ["DEFAULT_MAX_ITERATIONS", "RunStage", "SelfHealOutcome", "run_self_heal"]
