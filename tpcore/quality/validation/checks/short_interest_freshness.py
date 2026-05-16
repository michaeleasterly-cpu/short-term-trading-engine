"""short_interest freshness — FINRA bi-monthly data must not be stale.

FINRA disseminates ~twice a month. FAIL if the most-recent
``settlement_date`` is older than ``MAX_AGE_DAYS``.

MAX_AGE_DAYS is cadence-derived, not guessed (evidence 2026-05-16, a
live FINRA pull returned 10 settlement periods): bi-monthly period
≈ 16d + measured dissemination lag ≈ 13d (release_date −
settlement_date) + ~13d slack = 42d. The earlier 35 was ~right; the
red it threw was NOT miscalibration — it was the FINRA adapter's
missing offset-pagination ingesting only one stale period. That bug
is fixed; this threshold now legitimately means "FINRA published a
newer period we failed to pull" → honestly self-healable.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from tpcore.feeds import freshness_max_age_days
from tpcore.quality.validation.models import CheckResult, FailureDetail

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)

CHECK_NAME = "short_interest_freshness"
MAX_AGE_DAYS = freshness_max_age_days("finra_short_interest", 35)  # single source of truth: tpcore.feeds profile


async def check_short_interest_freshness(
    pool: asyncpg.Pool, source: Any = None,
) -> CheckResult:
    """Newest FINRA settlement_date must be ≤ MAX_AGE_DAYS old."""
    del source
    started = time.perf_counter()
    failures: list[FailureDetail] = []
    async with pool.acquire() as conn:
        latest = await conn.fetchval(
            "SELECT MAX(settlement_date) FROM platform.short_interest"
        )
    if latest is None:
        failures.append(FailureDetail(
            ticker="<short_interest>", reason="empty",
            expected=f"data within {MAX_AGE_DAYS}d",
            observed="zero rows in platform.short_interest",
        ))
    else:
        age = (datetime.now(UTC).date() - latest).days
        if age > MAX_AGE_DAYS:
            failures.append(FailureDetail(
                ticker="<short_interest>", reason="stale",
                expected=f"newest settlement_date within {MAX_AGE_DAYS}d",
                observed=f"latest {latest.isoformat()} ({age}d ago)",
            ))
    duration_ms = int((time.perf_counter() - started) * 1000)
    if failures:
        logger.warning("tpcore.validation.short_interest.fail",
                       reason=failures[0].reason)
    return CheckResult(
        name=CHECK_NAME, passed=len(failures) == 0, total=1,
        failed=len(failures), duration_ms=duration_ms, failures=failures,
    )


__all__ = ["CHECK_NAME", "check_short_interest_freshness"]
