"""Fundamentals integrity check — scans ``platform.fundamentals_quarterly``.

Violations:
  * ``period_end_date > filing_date``  — physically impossible; a filing
    cannot precede the period it describes (off-by-one in legacy ingest)
  * ``shares_outstanding <= 0``        — placeholder/garbage
  * ``filing_date > CURRENT_DATE``     — future-dated filing
  * NULLs in ``ticker`` or ``filing_date``

Same cadence as ``row_integrity``: scans the whole table on each run.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import structlog

from tpcore.quality.validation.models import CheckResult, FailureDetail

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)

CHECK_NAME = "fundamentals_integrity"
FAILURE_CAP = 50

_FUND_PREDICATE = """
       ticker IS NULL
    OR filing_date IS NULL
    OR filing_date > CURRENT_DATE
    OR (period_end_date IS NOT NULL AND period_end_date > filing_date)
    OR (shares_outstanding IS NOT NULL AND shares_outstanding <= 0)
"""

_FUND_SQL = f"""
    SELECT ticker, period_end_date, filing_date, shares_outstanding,
           CASE
               WHEN ticker IS NULL                                     THEN 'ticker_null'
               WHEN filing_date IS NULL                                THEN 'filing_date_null'
               WHEN filing_date > CURRENT_DATE                         THEN 'filing_date_future'
               WHEN period_end_date IS NOT NULL
                    AND period_end_date > filing_date                  THEN 'period_after_filing'
               WHEN shares_outstanding IS NOT NULL
                    AND shares_outstanding <= 0                        THEN 'shares_nonpositive'
           END AS violation
    FROM platform.fundamentals_quarterly
    WHERE {_FUND_PREDICATE}
    ORDER BY filing_date DESC, ticker
    LIMIT $1
"""

_FUND_COUNT_SQL = f"""
    SELECT COUNT(*) FROM platform.fundamentals_quarterly
    WHERE {_FUND_PREDICATE}
"""


async def check_fundamentals_integrity(
    pool: asyncpg.Pool,
    source: Any = None,
) -> CheckResult:
    """Scan ``platform.fundamentals_quarterly`` for structural anomalies."""
    del source
    started = time.perf_counter()
    async with pool.acquire() as conn:
        total = int(await conn.fetchval(_FUND_COUNT_SQL) or 0)
        rows = await conn.fetch(_FUND_SQL, FAILURE_CAP)

    failures: list[FailureDetail] = []
    for r in rows:
        ticker = r["ticker"] or "<null>"
        filing = r["filing_date"].isoformat() if r["filing_date"] else "<null>"
        period_end = r["period_end_date"].isoformat() if r["period_end_date"] else "<null>"
        violation = r["violation"] or "unknown"
        observed = f"filing={filing}, period_end={period_end}, shares={r['shares_outstanding']}"
        failures.append(
            FailureDetail(
                ticker=f"{ticker}@{filing}",
                reason=violation,
                expected="filing_date NOT NULL, period_end <= filing_date, shares > 0 OR NULL, filing <= today",
                observed=observed,
            )
        )

    duration_ms = int((time.perf_counter() - started) * 1000)
    return CheckResult(
        name=CHECK_NAME,
        passed=total == 0,
        total=1,
        failed=0 if total == 0 else 1,
        duration_ms=duration_ms,
        failures=failures,
    )


__all__ = ["check_fundamentals_integrity", "CHECK_NAME", "FAILURE_CAP"]
