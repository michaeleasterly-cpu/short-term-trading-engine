"""prices_daily classification_id completeness — the FK closure invariant.

FAIL if any row in ``platform.prices_daily`` has NULL ``classification_id``.
Path-A's nullable design (per v2.2 P6 migration 20260524_0600) allows the
column to be NULL, but operationally the goal is 100% population — engines
and AAR will join on classification_id once they migrate (operator scope
per v2.2 P8/P9). A NULL means we ingested bars for a ticker that wasn't
in ``ticker_classifications`` at INSERT time.

Healable: the ``sec_orphan_resolve`` ops stage closes orphans via three
phases:
  - Phase A: truth-set CIK lookup (operator-curated CSV)
  - Phase B: EDGAR direct ticker→CIK lookup (delisted-issuer-safe)
  - Phase C: OpenFIGI + FMP /profile alternate-source fallback

The check yields one failure per distinct orphan ticker (capped at 50 in
the report; full count in ``total``).
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import structlog

from tpcore.quality.validation.models import CheckResult, FailureDetail

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)

CHECK_NAME = "prices_daily_classification_id_completeness"


async def check_prices_daily_classification_id_completeness(
    pool: asyncpg.Pool, source: Any = None,
) -> CheckResult:
    """Every row in platform.prices_daily must have classification_id populated."""
    del source
    started = time.perf_counter()

    async with pool.acquire() as conn:
        n_null_rows = int(await conn.fetchval(
            "SELECT count(*) FROM platform.prices_daily WHERE classification_id IS NULL"
        ) or 0)
        if n_null_rows == 0:
            return CheckResult(
                name=CHECK_NAME, passed=True, total=0, failed=0,
                duration_ms=int((time.perf_counter() - started) * 1000),
                failures=[],
            )
        # Surface up to 50 distinct orphan tickers — caller decides on
        # heal vs escalate based on count.
        orphan_rows = await conn.fetch(
            "SELECT ticker, count(*) AS n FROM platform.prices_daily "
            "WHERE classification_id IS NULL "
            "GROUP BY ticker ORDER BY count(*) DESC LIMIT 50"
        )
        n_distinct_tickers = int(await conn.fetchval(
            "SELECT count(DISTINCT ticker) FROM platform.prices_daily "
            "WHERE classification_id IS NULL"
        ) or 0)

    failures: list[FailureDetail] = [
        FailureDetail(
            ticker=r["ticker"], reason="classification_id_null",
            expected="non-NULL classification_id (FK into ticker_classifications.id)",
            observed=f"{int(r['n'])} rows in prices_daily with classification_id IS NULL",
        )
        for r in orphan_rows
    ]

    duration_ms = int((time.perf_counter() - started) * 1000)
    logger.warning(
        "tpcore.validation.prices_daily_classification_id_completeness.fail",
        n_distinct_orphan_tickers=n_distinct_tickers,
        n_null_rows=n_null_rows,
        sample_top_orphans=[r["ticker"] for r in orphan_rows[:10]],
    )
    return CheckResult(
        name=CHECK_NAME, passed=False,
        total=n_distinct_tickers, failed=len(failures),
        duration_ms=duration_ms, failures=failures,
    )


__all__ = ["CHECK_NAME", "check_prices_daily_classification_id_completeness"]
