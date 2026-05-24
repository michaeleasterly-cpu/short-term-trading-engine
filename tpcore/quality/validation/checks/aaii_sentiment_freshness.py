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

from tpcore.feeds import freshness_max_age_days
from tpcore.quality.validation.models import CheckResult, FailureDetail

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)

CHECK_NAME = "aaii_sentiment_freshness"
MAX_AGE_DAYS = freshness_max_age_days("aaii_sentiment", 10)  # single source of truth: tpcore.feeds profile


async def check_aaii_sentiment_freshness(
    pool: asyncpg.Pool, source: Any = None,
) -> CheckResult:
    """Newest AAII survey date must be ≤ MAX_AGE_DAYS old."""
    del source
    started = time.perf_counter()
    failures: list[FailureDetail] = []
    async with pool.acquire() as conn:
        # Task #18 P7: reads platform.macro_data directly (legacy
        # platform.aaii_sentiment table/view dropped). Source='aaii';
        # current rows only (realtime_end='infinity') so SCD-2 revision
        # history doesn't shift the max.
        latest = await conn.fetchval(
            "SELECT MAX(observed_date) FROM platform.macro_data "
            "WHERE source = 'aaii' AND realtime_end = 'infinity'"
        )
    if latest is None:
        failures.append(FailureDetail(
            ticker="<aaii_sentiment>", reason="empty",
            expected=f"data within {MAX_AGE_DAYS}d",
            observed="zero rows for source='aaii' in platform.macro_data",
        ))
    else:
        now = datetime.now(UTC)
        age = (now.date() - latest).days
        # VENDOR-ANCHORED freshness (#165 facet 4): reason from AAII's
        # own publication calendar in UTC — the last scheduled Thursday
        # publish — NOT "today − N". We are "behind" only if our newest
        # row predates the vendor's most-recent scheduled publish (we
        # missed a publish). If the feed had no fixed schedule we'd
        # fall back to the cadence window (MAX_AGE_DAYS).
        # PURE + offline: reason from AAII's own publication calendar
        # (last scheduled Thursday, UTC) — NOT "today − N", NOT a
        # network call (validation must be deterministic/offline; the
        # conftest fakes the DB precisely to avoid I/O). The live
        # Last-Modified probe that distinguishes "our gap" from
        # "vendor-late" runs in the SELF-HEAL orchestrator before it
        # spends a heal cycle — not here.
        from tpcore.feeds.publication import expected_latest_publish
        expected = expected_latest_publish("aaii_sentiment", now)
        behind = (
            latest < expected if expected is not None
            else age > MAX_AGE_DAYS
        )
        if behind:
            failures.append(FailureDetail(
                ticker="<aaii_sentiment>", reason="stale",
                expected=(
                    f"≥ vendor's last scheduled publish "
                    f"{expected.isoformat()}" if expected
                    else f"newest date within {MAX_AGE_DAYS}d"
                ),
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
