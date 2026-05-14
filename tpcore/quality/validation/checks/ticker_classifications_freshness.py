"""Ticker classifications coverage check.

Asset class (``stock`` / ``etf`` / ``spac`` / ``fund``) is near-static
for any given ticker — refreshes exist to pick up new listings, not
to track changes to existing rows. So the metric is **coverage**, not
age: are there active prices_daily tickers without a row in
``ticker_classifications``?

Failure condition:

* Fewer than ``MIN_COVERAGE_PCT`` of the active prices_daily universe
  (last 30 days, not delisted) has a row in ticker_classifications.
  90% is the floor — every Phase-1 expansion or new SPAC IPO trips
  this until the operator runs ``scripts/classify_tickers.py``.

Staleness-by-time matters too (for completeness, in case the source
APIs return materially different classifications) but is gated at
``MAX_AGE_DAYS`` (default 60) — much looser than freshness-based
checks because classifications rarely drift.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import structlog

from tpcore.quality.validation.models import CheckResult, FailureDetail

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)

CHECK_NAME = "ticker_classifications_coverage"
MIN_COVERAGE_PCT = 0.90
MAX_AGE_DAYS = 60


_COVERAGE_SQL = """
    SELECT
        (SELECT MAX(last_updated) FROM platform.ticker_classifications) AS latest_update,
        (SELECT COUNT(*) FROM platform.ticker_classifications) AS classified_rows,
        (SELECT COUNT(DISTINCT ticker) FROM platform.prices_daily
         WHERE date >= CURRENT_DATE - INTERVAL '30 days'
           AND delisted = false) AS active_universe,
        (SELECT COUNT(DISTINCT pd.ticker)
         FROM platform.prices_daily pd
         LEFT JOIN platform.ticker_classifications tc USING (ticker)
         WHERE pd.date >= CURRENT_DATE - INTERVAL '30 days'
           AND pd.delisted = false
           AND tc.ticker IS NULL) AS unclassified
"""


async def check_ticker_classifications_coverage(
    pool: asyncpg.Pool,
    source: Any = None,
) -> CheckResult:
    """Verify ticker_classifications covers the active universe."""
    del source
    started = time.perf_counter()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_COVERAGE_SQL)

    latest = row["latest_update"] if row else None
    classified = int(row["classified_rows"] or 0) if row else 0
    universe = int(row["active_universe"] or 0) if row else 0
    unclassified = int(row["unclassified"] or 0) if row else 0

    failures: list[FailureDetail] = []

    if universe == 0:
        pass  # no active universe; not a classifier issue
    else:
        coverage = (universe - unclassified) / universe
        if coverage < MIN_COVERAGE_PCT:
            failures.append(FailureDetail(
                ticker="<coverage>",
                reason="insufficient_classification_coverage",
                expected=f"≥ {MIN_COVERAGE_PCT:.0%} of active tickers classified",
                observed=(
                    f"only {universe - unclassified}/{universe} ({coverage:.1%}) classified; "
                    f"{unclassified} unclassified (run scripts/classify_tickers.py)"
                ),
            ))

    if latest is not None:
        from datetime import UTC, datetime
        age_days = (datetime.now(UTC) - latest).days
        if age_days > MAX_AGE_DAYS:
            failures.append(FailureDetail(
                ticker="<freshness>",
                reason="stale_classifications",
                expected=f"newest last_updated within {MAX_AGE_DAYS}d",
                observed=f"latest_update={latest.isoformat()} ({age_days}d ago)",
            ))

    if classified == 0 and universe > 0:
        # If the table has zero rows and we have an active universe,
        # that's an outright failure regardless of coverage math above.
        failures.append(FailureDetail(
            ticker="<table>",
            reason="empty_table",
            expected="ticker_classifications populated",
            observed=f"table is empty (active universe={universe})",
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
    "MIN_COVERAGE_PCT",
    "check_ticker_classifications_coverage",
]
