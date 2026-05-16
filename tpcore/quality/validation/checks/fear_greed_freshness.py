"""fear_greed freshness — the index must be recomputed each session.

FAIL if the most-recent ``platform.fear_greed`` row is more than
``MAX_AGE_TRADING_DAYS`` NYSE sessions old. Trading-day gap via
``tpcore.calendar`` (XNYS) so weekends/holidays don't false-fail.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from tpcore import calendar as cal
from tpcore.quality.validation.models import CheckResult, FailureDetail

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)

CHECK_NAME = "fear_greed_freshness"
MAX_AGE_TRADING_DAYS = 3


async def check_fear_greed_freshness(
    pool: asyncpg.Pool, source: Any = None,
) -> CheckResult:
    """Most-recent fear_greed row must be ≤ 3 NYSE sessions old."""
    del source
    started = time.perf_counter()
    failures: list[FailureDetail] = []

    async with pool.acquire() as conn:
        latest = await conn.fetchval("SELECT MAX(date) FROM platform.fear_greed")

    if latest is None:
        failures.append(FailureDetail(
            ticker="<fear_greed>", reason="empty",
            expected=f"a row within {MAX_AGE_TRADING_DAYS} trading days",
            observed="zero rows in platform.fear_greed",
        ))
    else:
        today = datetime.now(UTC).date()
        # Sessions strictly after `latest` up to today = trading-day age.
        sessions = cal.sessions_in_range(latest, today)
        gap = max(0, len([s for s in sessions if s > latest]))
        if gap > MAX_AGE_TRADING_DAYS:
            failures.append(FailureDetail(
                ticker="<fear_greed>", reason="stale",
                expected=f"≤ {MAX_AGE_TRADING_DAYS} trading days old",
                observed=f"latest {latest.isoformat()} — {gap} sessions ago",
            ))

    duration_ms = int((time.perf_counter() - started) * 1000)
    if failures:
        logger.warning("tpcore.validation.fear_greed.fail",
                       reason=failures[0].reason)
    return CheckResult(
        name=CHECK_NAME,
        passed=len(failures) == 0,
        total=1,
        failed=len(failures),
        duration_ms=duration_ms,
        failures=failures,
    )


__all__ = ["CHECK_NAME", "check_fear_greed_freshness"]
