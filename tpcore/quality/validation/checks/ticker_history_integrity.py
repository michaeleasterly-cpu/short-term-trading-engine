"""ticker_history SCD-2 integrity — backstop to the GIST exclude constraint.

The GIST exclude constraint on (classification_id, daterange(valid_from,
COALESCE(valid_to, 'infinity'), '[)')) already prevents range overlap
per classification_id. This check provides a defense-in-depth surface
for:

  - Zero-duration rows (valid_from = valid_to)
  - Invalid range (valid_to < valid_from)
  - Multiple open rows per classification_id (GIST should prevent
    but the assertion is cheap and surfaces the constraint state)

Healer stage: `ticker_history_backfill` already handles incremental
maintenance; no auto-heal is wired for these defect classes (would
indicate an upstream loader bug, not a routine drift).
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import structlog

from tpcore.quality.validation.models import CheckResult, FailureDetail

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)

CHECK_NAME = "ticker_history_integrity"


async def check_ticker_history_integrity(
    pool: asyncpg.Pool, source: Any = None,
) -> CheckResult:
    """SCD-2 backstop for platform.ticker_history."""
    del source
    started = time.perf_counter()

    async with pool.acquire() as conn:
        n_zero = int(await conn.fetchval(
            "SELECT count(*) FROM platform.ticker_history "
            "WHERE valid_from = valid_to"
        ) or 0)
        n_invalid = int(await conn.fetchval(
            "SELECT count(*) FROM platform.ticker_history "
            "WHERE valid_to IS NOT NULL AND valid_to < valid_from"
        ) or 0)
        n_open_dup_cls = int(await conn.fetchval(
            """
            SELECT count(*) FROM (
                SELECT classification_id FROM platform.ticker_history
                WHERE valid_to IS NULL
                GROUP BY classification_id HAVING count(*) > 1
            ) s
            """
        ) or 0)

    failures: list[FailureDetail] = []
    if n_zero > 0:
        failures.append(FailureDetail(
            ticker="(table)", reason="zero_duration_rows",
            expected="0", observed=f"{n_zero}",
        ))
    if n_invalid > 0:
        failures.append(FailureDetail(
            ticker="(table)", reason="invalid_range_rows",
            expected="0", observed=f"{n_invalid}",
        ))
    if n_open_dup_cls > 0:
        failures.append(FailureDetail(
            ticker="(table)", reason="open_row_dup_classifications",
            expected="at most 1 valid_to=NULL row per classification_id (GIST-enforced)",
            observed=f"{n_open_dup_cls} classifications with >1 open row",
        ))

    passed = not failures
    duration_ms = int((time.perf_counter() - started) * 1000)
    if not passed:
        logger.warning(
            "tpcore.validation.ticker_history_integrity.fail",
            zero=n_zero, invalid=n_invalid, open_dup_classifications=n_open_dup_cls,
        )
    return CheckResult(
        name=CHECK_NAME, passed=passed,
        total=n_zero + n_invalid + n_open_dup_cls,
        failed=len(failures), duration_ms=duration_ms, failures=failures,
    )


__all__ = ["CHECK_NAME", "check_ticker_history_integrity"]
