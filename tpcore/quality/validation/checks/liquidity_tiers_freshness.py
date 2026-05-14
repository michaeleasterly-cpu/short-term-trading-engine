"""Liquidity-tiers freshness check.

Tier assignments drift slowly — spread observations accumulate
over weeks, not days. But if the operator forgets the quarterly
``scripts/run_tier_refresh.sh``, the table goes stale and the cost
model + universe filter both rot.

Failure conditions:

* Newest ``last_updated`` is older than ``MAX_AGE_DAYS`` (default 100).
* Less than ``MIN_T1_T2_COVERAGE_PCT`` of the active prices_daily
  universe has a T1 or T2 row. T1+T2 is the active trading universe —
  the engines refuse to size positions on T3+ tickers. Coverage
  rotting below this fraction means the operator's tier refresh
  hasn't run against the latest universe expansion.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import structlog

from tpcore.quality.validation.models import CheckResult, FailureDetail

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)

CHECK_NAME = "liquidity_tiers_freshness"
MAX_AGE_DAYS = 100
# T1+T2 should cover at least 5% of the active universe (the engines'
# tradable subset is intentionally small — sub-cap names get filtered
# out). Below 3% means the refresh hasn't run.
MIN_T1_T2_COVERAGE_PCT = 0.03


_FRESHNESS_SQL = """
    SELECT
        (SELECT MAX(last_updated) FROM platform.liquidity_tiers) AS latest,
        (SELECT COUNT(*) FROM platform.liquidity_tiers) AS rows_total,
        (SELECT COUNT(*) FROM platform.liquidity_tiers WHERE tier <= 2) AS t1_t2_count,
        (SELECT COUNT(DISTINCT ticker) FROM platform.prices_daily
         WHERE date >= CURRENT_DATE - INTERVAL '30 days'
           AND delisted = false) AS active_universe
"""


async def check_liquidity_tiers_freshness(
    pool: asyncpg.Pool,
    source: Any = None,
) -> CheckResult:
    """Verify liquidity_tiers is fresh + covers the trading universe."""
    del source
    started = time.perf_counter()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_FRESHNESS_SQL)

    latest = row["latest"] if row else None
    rows_total = int(row["rows_total"] or 0) if row else 0
    t1_t2 = int(row["t1_t2_count"] or 0) if row else 0
    universe = int(row["active_universe"] or 0) if row else 0

    failures: list[FailureDetail] = []

    if latest is None:
        failures.append(FailureDetail(
            ticker="<table>",
            reason="empty_table",
            expected="liquidity_tiers populated",
            observed="table is empty",
        ))
    else:
        from datetime import UTC, datetime
        age_days = (datetime.now(UTC) - latest).days
        if age_days > MAX_AGE_DAYS:
            failures.append(FailureDetail(
                ticker="<freshness>",
                reason="stale_assignment",
                expected=f"newest last_updated within {MAX_AGE_DAYS}d",
                observed=f"latest_assignment={latest.isoformat()} ({age_days}d ago)",
            ))

    if universe > 0 and rows_total > 0:
        coverage = t1_t2 / universe
        if coverage < MIN_T1_T2_COVERAGE_PCT:
            failures.append(FailureDetail(
                ticker="<coverage>",
                reason="insufficient_t1_t2_coverage",
                expected=(
                    f"≥ {MIN_T1_T2_COVERAGE_PCT:.0%} of active universe in T1+T2 "
                    f"(active={universe}, would need ≥ {int(MIN_T1_T2_COVERAGE_PCT * universe)})"
                ),
                observed=f"only {t1_t2}/{universe} ({coverage:.2%}) tickers in T1+T2",
            ))

    duration_ms = int((time.perf_counter() - started) * 1000)
    passed = len(failures) == 0
    return CheckResult(
        name=CHECK_NAME,
        passed=passed,
        total=1,
        failed=0 if passed else 1,
        duration_ms=duration_ms,
        failures=failures,
    )


__all__ = [
    "CHECK_NAME",
    "MAX_AGE_DAYS",
    "MIN_T1_T2_COVERAGE_PCT",
    "check_liquidity_tiers_freshness",
]
