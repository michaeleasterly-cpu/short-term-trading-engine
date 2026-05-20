"""liquidity_tiers completeness — universe-survives-the-cut invariant.

``liquidity_tiers_freshness`` answers two questions: "is the newest
``last_updated`` recent enough?" and "do T1+T2 cover at least 3% of the
active universe?". Both have tolerance knobs. Neither is a physical-
truth invariant: a stale-but-fresh-looking refresh can omit *any*
subset of the active universe and stay green so long as the headline
numbers cross their thresholds.

This check closes that hole with a zero-tolerance invariant that has
no percentage knob:

    Every ticker in the *active universe* must appear in
    ``platform.liquidity_tiers`` (with any tier — 1, 2, or 3). One
    missing active-universe ticker → FAIL.

Why the per-ticker monotone-non-decrease pattern (used for
``sec_insider_monotone`` / ``earnings_events_monotone``) does NOT
apply:

* ``platform.liquidity_tiers`` is DERIVED + RECOMPUTED quarterly by
  ``scripts/ops.py::_stage_tier_refresh`` (Corwin-Schultz spread
  bootstrap + ``assign_tiers`` aggregation). Rows are NOT append-only.
* A recompute can legitimately ADD a row (newly listed ticker meeting
  the bootstrap's liquidity threshold) AND legitimately REMOVE a row
  (delisted ticker, or one that fell out of the eligibility universe).
* So "per-ticker rowcount must monotonically non-decrease" is the
  wrong physical truth — it would false-fail every quarterly
  recompute. The correct invariant is *universe coverage*: the active
  universe must be a subset of liquidity_tiers, regardless of which
  specific rows the recompute added/removed.

Active universe (intersection of two filters — matches the
``sec_filings_freshness`` addressable CTE; this check does NOT
introduce a new definition):

* ``platform.ticker_classifications.asset_class = 'stock'`` — ETFs,
  SPACs, and funds are deliberately NOT tiered (the liquidity-tier
  model is engineered for common stock); their absence from
  liquidity_tiers is legitimate.
* ``platform.prices_daily`` has at least one bar in the trailing
  30-NYSE-session window — already-delisted / dormant tickers don't
  legitimately need a current liquidity tier.

Heal route: the canonical ``tier_refresh`` stage with
``skip_guard_days=0`` (force the bounded recompute past the 90-day
skip-guard). Same stage the existing ``liquidity_tiers_freshness``
HealSpec already uses. Bounded by ``max_attempts=2``.

Detector/healer symmetry: ``compute_liquidity_tiers_repair_targets``
calls the same ``_evaluate`` — they cannot disagree by construction.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from tpcore.quality.validation.models import CheckResult, FailureDetail

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)

CHECK_NAME = "liquidity_tiers_completeness"

# Cap the per-failure surface in CheckResult.failures for log-size
# sanity. CheckResult.failed always carries the TRUE count so confidence
# reflects reality. Matches the sec_insider_monotone /
# earnings_events_monotone cap.
MAX_REPORTED = 5

# Anti-join: active-universe tickers (stock asset_class + active in the
# trailing 30 NYSE sessions per prices_daily) that have NO row in
# liquidity_tiers. The two-filter active-universe definition mirrors
# the sec_filings_freshness addressable CTE; this check deliberately
# does NOT introduce a new definition.
_MISSING_ACTIVE_SQL = """
    WITH active_universe AS (
        SELECT DISTINCT pd.ticker
        FROM platform.prices_daily pd
        LEFT JOIN platform.ticker_classifications tc USING (ticker)
        WHERE pd.date >= CURRENT_DATE - INTERVAL '30 days'
          AND pd.delisted = false
          AND COALESCE(tc.asset_class, 'stock') = 'stock'
    )
    SELECT au.ticker
    FROM active_universe au
    LEFT JOIN platform.liquidity_tiers lt USING (ticker)
    WHERE lt.ticker IS NULL
    ORDER BY au.ticker
"""

# Universe-size + in-tiers counts for the PASS log line + the universe
# numerator in CheckResult.total. Same active-universe shape as the
# anti-join above.
_UNIVERSE_COUNTS_SQL = """
    WITH active_universe AS (
        SELECT DISTINCT pd.ticker
        FROM platform.prices_daily pd
        LEFT JOIN platform.ticker_classifications tc USING (ticker)
        WHERE pd.date >= CURRENT_DATE - INTERVAL '30 days'
          AND pd.delisted = false
          AND COALESCE(tc.asset_class, 'stock') = 'stock'
    )
    SELECT
        (SELECT COUNT(*) FROM active_universe) AS active_universe_size,
        (SELECT COUNT(DISTINCT au.ticker)
         FROM active_universe au
         JOIN platform.liquidity_tiers lt USING (ticker)) AS in_tiers
"""


@dataclass(frozen=True)
class _Evaluation:
    """One completeness evaluation — shared by check + healer.

    ``missing_active_tickers`` is the full sorted list of active-
    universe tickers absent from ``platform.liquidity_tiers``; the
    check caps the FailureDetail surface to ``MAX_REPORTED`` but the
    full count is reported in ``CheckResult.failed`` so confidence
    reflects reality.
    """

    missing_active_tickers: list[str] = field(default_factory=list)
    active_universe_size: int = 0
    in_tiers: int = 0


async def _evaluate(pool: asyncpg.Pool) -> _Evaluation:
    """Run the invariant once. Single source of truth for both
    ``check_liquidity_tiers_completeness`` (detection) and
    ``compute_liquidity_tiers_repair_targets`` (healing) — they cannot
    disagree by construction."""
    async with pool.acquire() as conn:
        missing_rows = await conn.fetch(_MISSING_ACTIVE_SQL)
        counts_row = await conn.fetchrow(_UNIVERSE_COUNTS_SQL)

    missing = [r["ticker"] for r in missing_rows]
    active_size = int(counts_row["active_universe_size"] or 0) if counts_row else 0
    in_tiers = int(counts_row["in_tiers"] or 0) if counts_row else 0

    return _Evaluation(
        missing_active_tickers=missing,
        active_universe_size=active_size,
        in_tiers=in_tiers,
    )


async def check_liquidity_tiers_completeness(
    pool: asyncpg.Pool,
    source: Any = None,
) -> CheckResult:
    """Zero-tolerance: every active-universe ticker has a row in
    ``platform.liquidity_tiers`` (with any tier). One missing → FAIL."""
    del source
    started = time.perf_counter()
    ev = await _evaluate(pool)

    if not ev.missing_active_tickers:
        logger.info(
            "tpcore.validation.liquidity_tiers_completeness.ok",
            active_universe_size=ev.active_universe_size,
            in_tiers=ev.in_tiers,
        )
        return CheckResult(
            name=CHECK_NAME,
            passed=True,
            total=max(ev.active_universe_size, 1),
            failed=0,
            duration_ms=int((time.perf_counter() - started) * 1000),
            failures=[],
        )

    sample = ev.missing_active_tickers[:MAX_REPORTED]
    failures: list[FailureDetail] = []
    for ticker in sample:
        failures.append(FailureDetail(
            ticker=ticker,
            reason="missing_from_liquidity_tiers",
            expected=(
                "every active-universe stock has a row in "
                "platform.liquidity_tiers (any tier 1/2/3)"
            ),
            observed=(
                f"{ticker} is in the active universe "
                f"(asset_class=stock + ≥1 bar in trailing 30 sessions) "
                f"but has NO row in liquidity_tiers. "
                f"Total missing: {len(ev.missing_active_tickers)} / "
                f"{ev.active_universe_size} active tickers. "
                f"Heal via tier_refresh stage with skip_guard_days=0."
            ),
        ))
    logger.warning(
        "tpcore.validation.liquidity_tiers_completeness.missing",
        missing_count=len(ev.missing_active_tickers),
        active_universe_size=ev.active_universe_size,
        in_tiers=ev.in_tiers,
        sample=sample,
    )
    return CheckResult(
        name=CHECK_NAME,
        passed=False,
        total=max(ev.active_universe_size, 1),
        failed=len(ev.missing_active_tickers),
        duration_ms=int((time.perf_counter() - started) * 1000),
        failures=failures,
    )


async def compute_liquidity_tiers_repair_targets(
    pool: asyncpg.Pool,
) -> list[str]:
    """Targets for the bounded auto-heal: active-universe tickers
    absent from ``platform.liquidity_tiers``.

    Returns ``[]`` when nothing to repair. Shares :func:`_evaluate`
    with the check; the healer can never target a different set than
    the detector reports.

    NOTE: the canonical ``tier_refresh`` stage today recomputes the
    full universe (no per-ticker scoping). The returned list is
    therefore advisory — for the orchestrator's telemetry and operator
    escalation surface, not for narrowing the stage's scope. If the
    tier_refresh stage later gains a ``--tickers`` knob this list is
    already in the right shape to feed it.
    """
    ev = await _evaluate(pool)
    return list(ev.missing_active_tickers)


__all__ = [
    "CHECK_NAME",
    "MAX_REPORTED",
    "check_liquidity_tiers_completeness",
    "compute_liquidity_tiers_repair_targets",
]
