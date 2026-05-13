"""Corporate-actions integrity check — scans ``platform.corporate_actions``.

Violations:
  * NULL ticker / action_date / action_type
  * ratio IS NULL OR ratio <= 0
  * ratio > 1000 (implausibly large; dividends shouldn't use ratio)
  * action_date > CURRENT_DATE + 365 days (far-future is suspicious)
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import structlog

from tpcore.quality.validation.models import CheckResult, FailureDetail

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)

CHECK_NAME = "corporate_actions_integrity"
FAILURE_CAP = 50

_CA_PREDICATE = """
       ticker IS NULL
    OR action_date IS NULL
    OR action_type IS NULL
    OR ratio IS NULL
    OR ratio <= 0
    OR ratio > 1000
    OR action_date > CURRENT_DATE + INTERVAL '365 days'
"""

_CA_SQL = f"""
    SELECT ticker, action_date, action_type, ratio,
           CASE
               WHEN ticker IS NULL                              THEN 'ticker_null'
               WHEN action_date IS NULL                         THEN 'action_date_null'
               WHEN action_type IS NULL                         THEN 'action_type_null'
               WHEN ratio IS NULL                               THEN 'ratio_null'
               WHEN ratio <= 0                                  THEN 'ratio_nonpositive'
               WHEN ratio > 1000                                THEN 'ratio_implausible'
               WHEN action_date > CURRENT_DATE + INTERVAL '365 days' THEN 'action_date_far_future'
           END AS violation
    FROM platform.corporate_actions
    WHERE {_CA_PREDICATE}
    ORDER BY action_date DESC, ticker
    LIMIT $1
"""

_CA_COUNT_SQL = f"""
    SELECT COUNT(*) FROM platform.corporate_actions WHERE {_CA_PREDICATE}
"""


async def check_corporate_actions_integrity(
    pool: asyncpg.Pool,
    source: Any = None,
) -> CheckResult:
    """Scan ``platform.corporate_actions`` for structural anomalies."""
    del source
    started = time.perf_counter()
    async with pool.acquire() as conn:
        total = int(await conn.fetchval(_CA_COUNT_SQL) or 0)
        rows = await conn.fetch(_CA_SQL, FAILURE_CAP)

    failures: list[FailureDetail] = []
    for r in rows:
        ticker = r["ticker"] or "<null>"
        date_iso = r["action_date"].isoformat() if r["action_date"] else "<null>"
        failures.append(
            FailureDetail(
                ticker=f"{ticker}@{date_iso}",
                reason=r["violation"] or "unknown",
                expected="ratio in (0, 1000], dates not far-future, non-null cols",
                observed=f"type={r['action_type']}, ratio={r['ratio']}",
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


__all__ = ["check_corporate_actions_integrity", "CHECK_NAME", "FAILURE_CAP"]
