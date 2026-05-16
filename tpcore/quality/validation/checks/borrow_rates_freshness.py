"""borrow_rates freshness — IBorrowDesk daily data must not be stale.

FAIL if the most-recent ``date`` is older than ``MAX_AGE_DAYS``.
Lenient (5d) because the source is scrape-fragile and the handler
legitimately skips on blocks rather than crashing.
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

CHECK_NAME = "borrow_rates_freshness"
MAX_AGE_DAYS = 5


async def check_borrow_rates_freshness(
    pool: asyncpg.Pool, source: Any = None,
) -> CheckResult:
    """Newest IBorrowDesk date must be ≤ MAX_AGE_DAYS old."""
    del source
    started = time.perf_counter()
    failures: list[FailureDetail] = []
    async with pool.acquire() as conn:
        latest = await conn.fetchval(
            "SELECT MAX(date) FROM platform.borrow_rates"
        )
    if latest is None:
        failures.append(FailureDetail(
            ticker="<borrow_rates>", reason="empty",
            expected=f"data within {MAX_AGE_DAYS}d",
            observed="zero rows in platform.borrow_rates",
        ))
    else:
        age = (datetime.now(UTC).date() - latest).days
        if age > MAX_AGE_DAYS:
            failures.append(FailureDetail(
                ticker="<borrow_rates>", reason="stale",
                expected=f"newest date within {MAX_AGE_DAYS}d",
                observed=f"latest {latest.isoformat()} ({age}d ago)",
            ))
    duration_ms = int((time.perf_counter() - started) * 1000)
    if failures:
        logger.warning("tpcore.validation.borrow_rates.fail",
                       reason=failures[0].reason)
    return CheckResult(
        name=CHECK_NAME, passed=len(failures) == 0, total=1,
        failed=len(failures), duration_ms=duration_ms, failures=failures,
    )


__all__ = ["CHECK_NAME", "check_borrow_rates_freshness"]
