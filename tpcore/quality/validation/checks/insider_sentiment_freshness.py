"""insider_sentiment freshness — Finnhub MSPR must not be stale.

Insider-sentiment is *monthly* data. This check asserts the table has
at least one record whose (year, month) period is within the last
``MAX_AGE_MONTHS`` months. Deliberately a freshness assertion, NOT an
invented coverage-% threshold — the catalyst/SEC episode showed a
fabricated coverage bar causes false reds and threshold-reasoning
confusion. Coverage tuning, if ever needed, comes later with evidence.
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

CHECK_NAME = "insider_sentiment_freshness"
MAX_AGE_MONTHS = 3

_SQL = """
    SELECT MAX(year * 12 + month) AS newest_period, COUNT(*) AS rows_total
    FROM platform.insider_sentiment
"""


async def check_insider_sentiment_freshness(
    pool: asyncpg.Pool, source: Any = None,
) -> CheckResult:
    """The newest insider-sentiment period must be ≤ MAX_AGE_MONTHS old."""
    del source
    started = time.perf_counter()
    failures: list[FailureDetail] = []

    async with pool.acquire() as conn:
        row = await conn.fetchrow(_SQL)

    newest_period = row["newest_period"] if row else None
    total = int(row["rows_total"]) if row and row["rows_total"] else 0
    now = datetime.now(UTC)
    cur_period = now.year * 12 + now.month

    if newest_period is None or total == 0:
        failures.append(FailureDetail(
            ticker="<insider_sentiment>", reason="empty",
            expected=f"a record within {MAX_AGE_MONTHS} months",
            observed="zero rows in platform.insider_sentiment",
        ))
    else:
        age_months = cur_period - int(newest_period)
        if age_months > MAX_AGE_MONTHS:
            failures.append(FailureDetail(
                ticker="<insider_sentiment>", reason="stale",
                expected=f"newest period within {MAX_AGE_MONTHS} months",
                observed=f"newest period is {age_months} months old",
            ))

    duration_ms = int((time.perf_counter() - started) * 1000)
    if failures:
        logger.warning("tpcore.validation.insider_sentiment.stale",
                        reason=failures[0].reason)
    return CheckResult(
        name=CHECK_NAME,
        passed=len(failures) == 0,
        total=1,
        failed=len(failures),
        duration_ms=duration_ms,
        failures=failures,
    )


__all__ = ["CHECK_NAME", "check_insider_sentiment_freshness"]
