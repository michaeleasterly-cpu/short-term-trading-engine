"""social_sentiment freshness + coverage (ApeWisdom).

Per task spec: FAIL if the most-recent data is > 7 days old OR fewer
than 30% of T1+T2 stocks have a row on that latest date. (This coverage
threshold is operator-specified, not invented — if live data later
shows it is structurally unreachable that is a separate evidence-based
recalibration, per the no-lazy-vendor-blame rule.)
"""
from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from tpcore.quality.validation.models import CheckResult, FailureDetail

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)

CHECK_NAME = "social_sentiment_freshness"
MAX_AGE_DAYS = 7
MIN_COVERAGE_PCT = 0.30

_LATEST_SQL = "SELECT MAX(date) AS latest FROM platform.social_sentiment"
_COVERAGE_SQL = """
    WITH t12 AS (
        SELECT COUNT(*) AS n
        FROM platform.liquidity_tiers lt
        LEFT JOIN platform.ticker_classifications tc USING (ticker)
        WHERE lt.tier <= 2 AND COALESCE(tc.asset_class, 'stock') = 'stock'
    ),
    covered AS (
        SELECT COUNT(DISTINCT s.ticker) AS n
        FROM platform.social_sentiment s
        JOIN platform.liquidity_tiers lt ON lt.ticker = s.ticker
        LEFT JOIN platform.ticker_classifications tc ON tc.ticker = s.ticker
        WHERE s.date = $1 AND lt.tier <= 2
          AND COALESCE(tc.asset_class, 'stock') = 'stock'
    )
    SELECT (SELECT n FROM t12) AS universe, (SELECT n FROM covered) AS covered
"""


async def check_social_sentiment_freshness(
    pool: asyncpg.Pool, source: Any = None,
) -> CheckResult:
    """FAIL if latest data > 7d old OR < 30% of T1+T2 covered."""
    del source
    started = time.perf_counter()
    failures: list[FailureDetail] = []

    async with pool.acquire() as conn:
        latest = await conn.fetchval(_LATEST_SQL)
        if latest is None:
            failures.append(FailureDetail(
                ticker="<social_sentiment>", reason="empty",
                expected=f"data within {MAX_AGE_DAYS}d",
                observed="zero rows in platform.social_sentiment",
            ))
        else:
            age = (datetime.now(UTC).date() - latest).days
            if age > MAX_AGE_DAYS:
                failures.append(FailureDetail(
                    ticker="<social_sentiment>", reason="stale",
                    expected=f"latest within {MAX_AGE_DAYS}d",
                    observed=f"latest {latest.isoformat()} ({age}d ago)",
                ))
            cov = await conn.fetchrow(_COVERAGE_SQL, latest)
            universe = int(cov["universe"] or 0)
            covered = int(cov["covered"] or 0)
            if universe > 0:
                pct = covered / universe
                if pct < MIN_COVERAGE_PCT:
                    failures.append(FailureDetail(
                        ticker="<social_sentiment>", reason="low_coverage",
                        expected=f"≥ {MIN_COVERAGE_PCT:.0%} of T1+T2 stocks",
                        observed=(f"{covered}/{universe} ({pct:.1%}) on "
                                  f"{latest.isoformat()}"),
                    ))

    duration_ms = int((time.perf_counter() - started) * 1000)
    if failures:
        logger.warning("tpcore.validation.social_sentiment.fail",
                        reasons=[f.reason for f in failures])
    return CheckResult(
        name=CHECK_NAME,
        passed=len(failures) == 0,
        total=2,  # freshness + coverage
        failed=len(failures),
        duration_ms=duration_ms,
        failures=failures,
    )


__all__ = ["CHECK_NAME", "check_social_sentiment_freshness"]
