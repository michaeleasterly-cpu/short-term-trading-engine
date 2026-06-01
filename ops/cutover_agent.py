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
import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from tpcore import providers as P
from tpcore.db import build_asyncpg_pool
from tpcore.providers import ProviderStatus
from tpcore.selfheal.registry import HEAL_SPECS

logger = structlog.get_logger(__name__)

# F0 (2026-06-01) — maximum age (days) of a "evaluate.{feed}.{candidate}"
# PASS verdict before the cutover_agent considers it stale and blocks
# the swap. Mirrors the operator's MAX_PARITY_AGE decision (30 days) —
# tight enough to surface provider drift, loose enough to avoid forcing
# a re-evaluate on every cycle. Constant lives next to the rest of the
# data-feed-lifecycle constants by convention.
_MAX_PARITY_AGE_DAYS: int = 30

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

# F0 freshness gate: the most-recent ``evaluate.{feed}.{candidate}``
# row drives the decision. data_quality_log columns: source, timestamp,
# confidence (1.0=PASS, 0.0=FAIL, NULL=NOT_EVALUABLE per the EVALUATE
# stage in scripts/ops.py), notes (JSON-encoded verdict payload).
_PARITY_LATEST_SQL = """
    SELECT timestamp, confidence, notes
    FROM platform.data_quality_log
    WHERE source = $1
    ORDER BY timestamp DESC
    LIMIT 1
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


@dataclass(frozen=True)
class ParityFreshness:
    """Outcome of the F0 freshness check on a (feed, candidate) pair.

    ``fresh`` is the dispositive gate: True iff a recent PASS verdict
    was found in ``platform.data_quality_log``. ``reason`` carries the
    operator-facing block message used by ``run_cutover_pass`` to
    surface a clear "why blocked" without re-running parity at
    cutover time.
    """

    fresh: bool
    reason: str
    verdict: str | None = None
    verdict_age_days: int | None = None


async def _parity_verdict_fresh(
    pool: Any, *, feed: str, candidate: str,
) -> ParityFreshness:
    """F0 (2026-06-01) — cutover-time parity freshness gate.

    Reads the most-recent ``data_quality_log`` row whose source is
    ``evaluate.{feed}.{candidate}`` and decides BLOCK vs ALLOW per
    the F0 rules:

      * No verdict row → BLOCK (operator has not run the EVALUATE
        stage for this pair yet).
      * Latest verdict is NOT_EVALUABLE → BLOCK (honest non-pass).
      * Latest verdict is FAIL → BLOCK (parity failed; promotion
        must not proceed even if an older PASS exists).
      * Latest verdict is PASS but older than ``_MAX_PARITY_AGE_DAYS``
        → BLOCK (stale; re-evaluate).
      * Latest verdict is PASS within the window → ALLOW.

    Operator hard rule: no fail-open path. Any uncertainty BLOCKS.
    """
    source = f"evaluate.{feed}.{candidate}"
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_PARITY_LATEST_SQL, source)

    if row is None:
        return ParityFreshness(
            fresh=False,
            reason=(
                f"no parity verdict on file for {source!r} — operator "
                f"must run `python scripts/ops.py --stage "
                f"evaluate_provider_parity --param feed={feed} "
                f"--param candidate={candidate} --param dry_run=false` "
                f"before cutover can proceed"
            ),
        )

    confidence = row["confidence"]
    # confidence: 1.0=PASS, 0.0=FAIL, NULL=NOT_EVALUABLE
    if confidence is None:
        # Recover the verdict label from the persisted notes JSON for
        # the operator-facing message; default to NOT_EVALUABLE on
        # malformed JSON.
        verdict_label = "not_evaluable"
        try:
            payload = json.loads(row["notes"]) if row["notes"] else {}
            verdict_label = str(payload.get("verdict") or "not_evaluable")
        except (json.JSONDecodeError, TypeError):
            pass
        return ParityFreshness(
            fresh=False, verdict=verdict_label,
            reason=(
                f"latest parity verdict for {source!r} is "
                f"{verdict_label.upper()} — honest non-pass; promotion "
                f"blocked. Re-evaluate after addressing the underlying "
                f"data gap (no incumbent samples, or DERIVED feed)"
            ),
        )

    # Confidence is non-None: PASS (1.0) vs FAIL (0.0).
    if float(confidence) < 1.0:
        return ParityFreshness(
            fresh=False, verdict="fail",
            reason=(
                f"latest parity verdict for {source!r} is FAIL "
                f"(confidence={confidence}) — promotion blocked. "
                f"Re-evaluate after addressing the failing dimension "
                f"(coverage / freshness / accuracy per the verdict "
                f"evidence in notes)"
            ),
        )

    # PASS — check freshness.
    ts = row["timestamp"]
    if ts is None:
        return ParityFreshness(
            fresh=False, verdict="pass",
            reason=(
                f"latest parity verdict for {source!r} has NULL "
                f"timestamp — malformed row; promotion blocked"
            ),
        )
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    age = datetime.now(UTC) - ts
    age_days = age.days
    if age > timedelta(days=_MAX_PARITY_AGE_DAYS):
        return ParityFreshness(
            fresh=False, verdict="pass", verdict_age_days=age_days,
            reason=(
                f"latest parity verdict for {source!r} is PASS but "
                f"stale ({age_days}d old, max "
                f"{_MAX_PARITY_AGE_DAYS}d) — re-evaluate before "
                f"cutover. Run `python scripts/ops.py --stage "
                f"evaluate_provider_parity --param feed={feed} "
                f"--param candidate={candidate} --param "
                f"force=true --param dry_run=false`"
            ),
        )
    return ParityFreshness(
        fresh=True, verdict="pass", verdict_age_days=age_days,
        reason=(
            f"recent PASS verdict for {source!r} "
            f"({age_days}d old, within {_MAX_PARITY_AGE_DAYS}d)"
        ),
    )


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
        # F0 (2026-06-01): parity freshness gate. The static FALLBACK
        # status invariant trusts ``parity_verified_at`` set at
        # construction — but a verified-once provider may have drifted.
        # This check reads the most-recent EVALUATE-stage verdict from
        # ``platform.data_quality_log`` and blocks the cutover if:
        # missing / NOT_EVALUABLE / latest FAIL / PASS older than
        # _MAX_PARITY_AGE_DAYS. Never fails open — any uncertainty
        # blocks.
        freshness = await _parity_verdict_fresh(
            pool, feed=feed, candidate=fb.provider,
        )
        if not freshness.fresh:
            stale_block = (
                f"BLOCKED {feed}→{fb.provider}: {freshness.reason}"
            )
            blocked.append(stale_block)
            logger.warning(
                "cutover_agent.blocked",
                summary=stale_block,
                feed=feed, candidate=fb.provider,
                verdict=freshness.verdict,
                verdict_age_days=freshness.verdict_age_days,
            )
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
