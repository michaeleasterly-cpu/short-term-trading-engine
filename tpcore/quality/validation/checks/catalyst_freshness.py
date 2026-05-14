"""Catalyst-events freshness check — confirms ``platform.catalyst_events``
is staying current with the active universe.

The vector engine reads earnings-beat events from this table. Without
ongoing refresh the table goes stale within ~3 months (earnings cycles
are quarterly) and the engine silently produces fewer candidates.

Failure conditions:

* Newest ``event_date`` is older than ``MAX_AGE_DAYS`` (default 90).
* Fewer than ``MIN_COVERED_TICKERS`` distinct tickers (in T1+T2) have
  at least one catalyst event in the last ``COVERAGE_WINDOW_DAYS``
  (default 180).

Both pieces matter:

* Freshness alone isn't enough — one new event from one ticker would
  pass a freshness-only check while the rest of the universe rots.
* Coverage alone isn't enough — historical coverage means nothing if
  the table hasn't seen a new event in a year.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import structlog

from tpcore.quality.validation.models import CheckResult, FailureDetail

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)

CHECK_NAME = "catalyst_events_freshness"
MAX_AGE_DAYS = 90
# Minimum coverage of the *addressable* universe — T1+T2 tickers
# classified as stocks (operating companies that report earnings).
# ETFs/SPACs/funds in T1+T2 don't report quarterly earnings, so they
# can't have catalyst events and including them in the denominator
# produces false-fail. The non-ETF/non-SPAC/non-fund stock subset of
# T1+T2 is currently ~66 tickers.
#
# Threshold rationale: catalyst rows only record EARNINGS_BEAT events
# where ``actual_eps > estimated_eps * 1.05``. Historical baseline is
# ~25-35% of stocks beating by >5% in any 180-day window (most stocks
# either meet, miss, or beat by less than 5%). A drop below 20% means
# either the table is genuinely stale, or the backfill silently lost
# data — both warrant operator attention.
MIN_COVERAGE_PCT = 0.20
COVERAGE_WINDOW_DAYS = 180


_FRESHNESS_SQL = f"""
    WITH addressable AS (
        SELECT lt.ticker
        FROM platform.liquidity_tiers lt
        LEFT JOIN platform.ticker_classifications tc USING (ticker)
        WHERE lt.tier <= 2
          AND COALESCE(tc.asset_class, 'stock') = 'stock'
    )
    SELECT
        MAX(event_date) AS newest_event,
        (SELECT COUNT(*) FROM addressable) AS addressable_count,
        (SELECT COUNT(DISTINCT a.ticker)
         FROM addressable a
         JOIN platform.catalyst_events ce ON ce.ticker = a.ticker
         WHERE ce.event_date >= CURRENT_DATE - INTERVAL '{COVERAGE_WINDOW_DAYS} days'
        ) AS covered_count,
        COUNT(*) AS total_rows
    FROM platform.catalyst_events
"""


async def check_catalyst_freshness(
    pool: asyncpg.Pool,
    source: Any = None,
) -> CheckResult:
    """Verify catalyst_events is fresh + covers T1+T2 adequately."""
    del source
    started = time.perf_counter()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_FRESHNESS_SQL)

    newest = row["newest_event"] if row else None
    addressable = int(row["addressable_count"] or 0) if row else 0
    covered = int(row["covered_count"] or 0) if row else 0
    total = int(row["total_rows"] or 0) if row else 0

    failures: list[FailureDetail] = []

    # Age check.
    from datetime import UTC, datetime
    today = datetime.now(UTC).date()
    if newest is None:
        failures.append(FailureDetail(
            ticker="<table>",
            reason="empty_table",
            expected=f"catalyst_events populated with events ≤ {MAX_AGE_DAYS}d old",
            observed="catalyst_events is empty",
        ))
    else:
        age_days = (today - newest).days
        if age_days > MAX_AGE_DAYS:
            failures.append(FailureDetail(
                ticker="<freshness>",
                reason="stale_newest_event",
                expected=f"newest event_date within {MAX_AGE_DAYS}d (today={today})",
                observed=f"newest_event_date={newest} ({age_days}d ago)",
            ))

    # Coverage check — fraction of *addressable* (stock-class) T1+T2
    # tickers with a recent event. ETFs/SPACs/funds excluded from the
    # denominator since they don't report quarterly earnings.
    if addressable == 0:
        # No stocks to measure against — skip silently (universe issue,
        # not a catalyst issue).
        pass
    else:
        coverage_pct = covered / addressable
        if coverage_pct < MIN_COVERAGE_PCT:
            failures.append(FailureDetail(
                ticker="<coverage>",
                reason="insufficient_stock_coverage",
                expected=(
                    f"≥ {MIN_COVERAGE_PCT:.0%} of T1+T2 stocks "
                    f"({int(MIN_COVERAGE_PCT * addressable)} of {addressable}) "
                    f"with a recent event"
                ),
                observed=(
                    f"only {covered}/{addressable} stocks ({coverage_pct:.1%}) "
                    f"have an event in last {COVERAGE_WINDOW_DAYS}d "
                    f"(total catalyst rows={total})"
                ),
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
    "COVERAGE_WINDOW_DAYS",
    "MAX_AGE_DAYS",
    "MIN_COVERAGE_PCT",
    "check_catalyst_freshness",
]
