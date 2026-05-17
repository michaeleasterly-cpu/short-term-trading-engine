"""Automated provider-CUTOVER agent (Data Provider Lifecycle Phase 5).

Deterministic, bounded, idempotent — NO LLM. Spec §10: cutover is
automated (a provider swap for an existing feed is reversible +
parity-gated; the parity gate already supplied the human-equivalent
judgement, so no operator approval). The operator only approves
ADD/REMOVE via the Data Feed Change Request.

One pass: for every feed whose ACTIVE provider's validation is red,
if a parity-verified FALLBACK exists in the declared registry and the
live overlay is not already on it, `plan_cutover` (the pure legality
guard) → `apply_cutover` (flip the runtime overlay + emit
`PROVIDER_CUTOVER`). Every swap surfaces in the weekly digest.

Honest state: this lands FUNCTIONAL + CORRECT but **dormant** — no
feed has a parity-verified FALLBACK yet (none have passed EVALUATE),
so the pass correctly finds nothing to swap and is a no-op until one
does. Building it right means cutover Just Works the instant a
fallback is verified. The natural trigger is *after* self-heal
escalates a still-red feed (a documented integration point — not
wired across the cycle here; dormant makes that equivalent today).
"""
from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from typing import Any

import structlog

from tpcore import providers as P
from tpcore.db import build_asyncpg_pool
from tpcore.providers import ProviderStatus
from tpcore.selfheal.registry import HEAL_SPECS

logger = structlog.get_logger(__name__)

# Same red predicate as the self-heal orchestrator / data_repair_service
# (latest validation.* row that is stale or below confidence 1.0).
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
"""


@dataclass(frozen=True)
class CutoverPassResult:
    cutovers: list[str]     # "feed→provider" applied
    blocked: list[str]      # plan.summary for blocked attempts
    dormant_feeds: list[str]  # red feed, no parity-verified FALLBACK
    already_on_fallback: list[str]


async def _red_feeds(pool: Any) -> set[str]:
    """Feeds whose validation check(s) are currently red. check→feed via
    the HealSpec registry source (the existing single source of truth)."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(_RED_SQL)
    red_checks = {r["source"].removeprefix("validation.") for r in rows}
    return {
        spec.source
        for check, spec in HEAL_SPECS.items()
        if check in red_checks
    }


def _fallback_for(feed: str):
    # Read the LIVE SoT (not an import-time snapshot) — an agent that
    # acts on current state must see the current registry/overlay.
    for b in P.PROVIDER_BINDINGS.get(feed, []):
        if b.status is ProviderStatus.FALLBACK:
            return b  # model guarantees parity_verified_at is set
    return None


async def run_cutover_pass(pool: Any) -> CutoverPassResult:
    """One deterministic, idempotent cutover pass. Safe to run every
    cycle: no red feed / no FALLBACK / already-on-fallback ⇒ no-op."""
    red = await _red_feeds(pool)
    cutovers: list[str] = []
    blocked: list[str] = []
    dormant: list[str] = []
    already: list[str] = []

    for feed in sorted(red):
        fb = _fallback_for(feed)
        if fb is None:
            dormant.append(feed)  # red but no parity-verified fallback
            continue
        cur = await P.resolve_active_provider(pool, feed)
        if cur is not None and cur.provider == fb.provider:
            already.append(feed)  # idempotent — already cut over
            continue
        plan = P.plan_cutover(feed, fb.provider)
        if not plan.allowed:
            blocked.append(plan.summary)
            logger.warning("cutover_agent.blocked", summary=plan.summary)
            continue
        await P.apply_cutover(pool, plan)
        cutovers.append(f"{feed}→{fb.provider}")
        logger.info("cutover_agent.applied", feed=feed, to=fb.provider)

    result = CutoverPassResult(cutovers, blocked, dormant, already)
    logger.info(
        "cutover_agent.pass_done",
        cutovers=len(cutovers), blocked=len(blocked),
        dormant=len(dormant), already=len(already),
    )
    return result


async def _amain() -> int:
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_URL_IPV4")
    if not dsn:
        logger.error("cutover_agent.no_dsn")
        return 1
    pool = await build_asyncpg_pool(dsn)
    try:
        r = await run_cutover_pass(pool)
    finally:
        await pool.close()
    print(
        f"cutover pass: applied={r.cutovers} blocked={len(r.blocked)} "
        f"dormant(no-fallback)={len(r.dormant_feeds)} "
        f"already-on-fallback={len(r.already_on_fallback)}"
    )
    return 2 if r.blocked else 0


def main() -> None:  # pragma: no cover - CLI shim
    sys.exit(asyncio.run(_amain()))


if __name__ == "__main__":  # pragma: no cover
    main()
