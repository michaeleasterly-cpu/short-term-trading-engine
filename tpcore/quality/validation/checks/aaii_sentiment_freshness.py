"""aaii_sentiment freshness — AAII weekly survey must not be stale.

FAIL if the most-recent survey ``date`` is older than ``MAX_AGE_DAYS``.
The survey is weekly (published Thursdays); 10d tolerates one missed
publication / a late Friday pull without false-alarming.
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

CHECK_NAME = "aaii_sentiment_freshness"
MAX_AGE_DAYS = 10


async def check_aaii_sentiment_freshness(
    pool: asyncpg.Pool, source: Any = None,
) -> CheckResult:
    """Newest AAII survey date must be ≤ MAX_AGE_DAYS old."""
    del source
    started = time.perf_counter()
    failures: list[FailureDetail] = []
    async with pool.acquire() as conn:
        latest = await conn.fetchval(
            "SELECT MAX(date) FROM platform.aaii_sentiment"
        )
    if latest is None:
        failures.append(FailureDetail(
            ticker="<aaii_sentiment>", reason="empty",
            expected=f"data within {MAX_AGE_DAYS}d",
            observed="zero rows in platform.aaii_sentiment",
        ))
    else:
        age = (datetime.now(UTC).date() - latest).days
        if age > MAX_AGE_DAYS:
            failures.append(FailureDetail(
                ticker="<aaii_sentiment>", reason="stale",
                expected=f"newest date within {MAX_AGE_DAYS}d",
                observed=f"latest {latest.isoformat()} ({age}d ago)",
            ))
    duration_ms = int((time.perf_counter() - started) * 1000)
    if failures:
        logger.warning("tpcore.validation.aaii_sentiment.fail",
                       reason=failures[0].reason)
    return CheckResult(
        name=CHECK_NAME, passed=len(failures) == 0, total=1,
        failed=len(failures), duration_ms=duration_ms, failures=failures,
    )


__all__ = ["CHECK_NAME", "check_aaii_sentiment_freshness"]
