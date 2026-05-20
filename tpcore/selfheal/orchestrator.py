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

from .probes import VENDOR_PROBES, VendorState
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
    to decide whether to emit DATA_OPERATIONS_COMPLETE.

    ``vendor_late`` carries sources where a per-source probe positively
    showed the vendor has nothing newer than what we hold — the heal
    cycle would be wasted churn (no newer rows to pull). The
    orchestrator skips heal for those sources AND skips re-classifying
    them as ``escalated`` (they're not an our-defect to investigate).
    The wrapper emits a distinct INFO event (TRIGGER_VENDOR_LATE) per
    entry so the dashboard distinguishes "the vendor missed a publish"
    from "our ingestion is broken." **The 100%-green-or-don't-trade
    invariant is unchanged: ``green=True`` still requires no remaining
    reds in the data_quality_log — vendor-late entries leave the row
    red, so the gate stays sacred.**
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    green: bool
    iterations: int
    healed: list[str] = Field(default_factory=list)
    # (source, reason) — everything a human must look at, by-design or bug.
    escalated: list[tuple[str, str]] = Field(default_factory=list)
    # (source, our_latest_iso, vendor_latest_iso) — vendor-MISSED edge,
    # informational only, the wrapper emits TRIGGER_VENDOR_LATE for each.
    vendor_late: list[tuple[str, str, str]] = Field(default_factory=list)


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
    # Across iterations: keep the latest (our_latest, vendor_latest) per
    # source for the wrapper's INFO event. Re-probed each iteration in
    # case the heal of another source advances the snapshot.
    vendor_late_acc: dict[str, VendorState] = {}

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
            return SelfHealOutcome(
                green=True, iterations=iteration, healed=healed,
                vendor_late=_vendor_late_payload(vendor_late_acc),
            )

        # Resolve dispositions.
        unhealable: list[tuple[str, str]] = []
        actions: dict[tuple[str, frozenset], tuple[str, dict[str, str]]] = {}
        # vendor-late classifications recorded this iteration (deduped by source).
        vendor_late_this_iter: dict[str, VendorState] = {}
        for check in reds:
            spec = spec_for(check)
            if spec is None:
                unhealable.append(
                    (check, "unknown red — no HealSpec in registry (must "
                            "never be silently ignored; add a spec)"))
            elif not spec.healable:
                unhealable.append((spec.source, f"{check}: {spec.unhealable_reason}"))
            else:
                # Vendor-late consult — BEFORE adding a heal action,
                # ask the per-source probe whether the vendor actually
                # has anything newer than we hold. If positively False,
                # the heal would be wasted churn — skip and classify as
                # vendor_late (the wrapper emits TRIGGER_VENDOR_LATE for
                # visibility). None / probe-unavailable → fall back to
                # the existing heal-as-usual behaviour.
                probe = VENDOR_PROBES.get(spec.source)
                if probe is not None and spec.source not in vendor_late_this_iter:
                    state = await probe(pool)
                    if state is not None and not state.has_newer:
                        vendor_late_this_iter[spec.source] = state
                        logger.info(
                            "selfheal.vendor_late",
                            source=spec.source, check=check,
                            our_latest=state.our_latest.isoformat(),
                            vendor_latest=state.vendor_latest.isoformat(),
                            iteration=iteration,
                        )
                        continue
                key = (spec.stage, frozenset(spec.params.items()))
                actions[key] = (spec.stage, spec.params)

        # Latest vendor_late wins (re-probed each iter). Stale entries
        # from prior iterations stay around so a vendor-MISSED red
        # healed-then-rebrowed-as-late still surfaces.
        vendor_late_acc.update(vendor_late_this_iter)

        if unhealable:
            # A human is needed regardless — escalate the full picture
            # now rather than burn heal cycles that can't reach green.
            logger.warning(
                "selfheal.escalate_unhealable",
                iteration=iteration, unhealable=unhealable,
                deferred_healable=[a[0] for a in actions.values()],
                vendor_late=list(vendor_late_acc.keys()),
            )
            return SelfHealOutcome(
                green=False, iterations=iteration, healed=healed,
                escalated=unhealable,
                vendor_late=_vendor_late_payload(vendor_late_acc),
            )

        # If every remaining red is vendor-late, no heal action is
        # possible this iteration — exit instead of looping until
        # max_iterations exhausts on a hopeless re-probe.
        if not actions:
            logger.info(
                "selfheal.vendor_late_exit",
                iteration=iteration, vendor_late=list(vendor_late_acc.keys()),
            )
            return SelfHealOutcome(
                green=False, iterations=iteration, healed=healed,
                vendor_late=_vendor_late_payload(vendor_late_acc),
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
        vendor_late=_vendor_late_payload(vendor_late_acc),
    )


def _vendor_late_payload(
    acc: dict[str, VendorState],
) -> list[tuple[str, str, str]]:
    """Freeze the dict into the SelfHealOutcome tuple shape: (source,
    our_latest_iso, vendor_latest_iso). Sorted by source for stable
    test assertions and for the wrapper's deterministic INFO emission."""
    return [
        (src, st.our_latest.isoformat(), st.vendor_latest.isoformat())
        for src, st in sorted(acc.items())
    ]


__all__ = ["DEFAULT_MAX_ITERATIONS", "RunStage", "SelfHealOutcome", "run_self_heal"]
