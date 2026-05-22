"""insider_filings freshness — FMP daily-granularity Form-4 data must
not be stale.

The vector engine candidate ``vector_beat_reversal_insider_filter_v1``
reads ``platform.insider_filings`` for a 30d-rolling MSPR signal at
daily resolution. If the table goes stale (the nightly delta stage
silently stopped landing rows), the engine reads off-by-N-days data
and the signal is wrong. This check is the tripwire.

FAIL if the most-recent ``transaction_date`` is older than
``MAX_AGE_DAYS``. The bound is read from
``FEED_PROFILES['insider_sentiment_daily']`` so the freshness budget,
the skip-guard, and the heal expectation come from the same number.
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

CHECK_NAME = "insider_filings_freshness"
MAX_AGE_DAYS = freshness_max_age_days("insider_sentiment_daily", 5)
"""Pulled from the FeedProfile (single source of truth). Default 5d
keeps the check honest — Form 4 has a 2-business-day filing deadline,
so a daily delta should land fresh rows within 1-2 sessions."""


async def check_insider_filings_freshness(
    pool: asyncpg.Pool, source: Any = None,
) -> CheckResult:
    """Newest insider_filings transaction_date must be ≤ MAX_AGE_DAYS old.

    The check intentionally tolerates an empty table without failing
    PRE-BACKFILL: if the table is empty AND zero rows have ever been
    written to it (according to its own MAX), we emit a single warning-
    shaped failure ('empty') so the operator sees the gap until the
    one-shot ``--stage historical_insider_sentiment_daily`` runs.
    """
    del source
    started = time.perf_counter()
    failures: list[FailureDetail] = []
    async with pool.acquire() as conn:
        latest = await conn.fetchval(
            "SELECT MAX(transaction_date) FROM platform.insider_filings"
        )
    if latest is None:
        failures.append(FailureDetail(
            ticker="<insider_filings>", reason="empty",
            expected=f"data within {MAX_AGE_DAYS}d",
            observed="zero rows in platform.insider_filings — operator "
                     "must run historical_insider_sentiment_daily",
        ))
    else:
        age = (datetime.now(UTC).date() - latest).days
        if age > MAX_AGE_DAYS:
            failures.append(FailureDetail(
                ticker="<insider_filings>", reason="stale",
                expected=f"newest transaction_date within {MAX_AGE_DAYS}d",
                observed=f"latest {latest.isoformat()} ({age}d ago)",
            ))
    duration_ms = int((time.perf_counter() - started) * 1000)
    if failures:
        logger.warning(
            "tpcore.validation.insider_filings.fail",
            reason=failures[0].reason,
        )
    return CheckResult(
        name=CHECK_NAME, passed=len(failures) == 0, total=1,
        failed=len(failures), duration_ms=duration_ms, failures=failures,
    )


__all__ = ["CHECK_NAME", "check_insider_filings_freshness"]
