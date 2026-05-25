"""issuer_securities SCD-2 integrity invariants.

FAIL if any open-window duplicates per (issuer_id, classification_id)
or zero-duration / invalid-range rows. Healed by the
`issuer_history_cleanup` stage (same window-function chain).
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import structlog

from tpcore.quality.validation.models import CheckResult, FailureDetail

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)

CHECK_NAME = "issuer_securities_integrity"


async def check_issuer_securities_integrity(
    pool: asyncpg.Pool, source: Any = None,
) -> CheckResult:
    """SCD-2 integrity for platform.issuer_securities."""
    del source
    started = time.perf_counter()

    async with pool.acquire() as conn:
        n_zero = int(await conn.fetchval(
            "SELECT count(*) FROM platform.issuer_securities "
            "WHERE valid_from = valid_to"
        ) or 0)
        n_invalid = int(await conn.fetchval(
            "SELECT count(*) FROM platform.issuer_securities "
            "WHERE valid_to IS NOT NULL AND valid_to < valid_from"
        ) or 0)
        n_open_dup = int(await conn.fetchval(
            """
            SELECT count(*) FROM (
                SELECT issuer_id, classification_id
                FROM platform.issuer_securities
                WHERE valid_to IS NULL
                GROUP BY issuer_id, classification_id HAVING count(*) > 1
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
    if n_open_dup > 0:
        failures.append(FailureDetail(
            ticker="(table)", reason="open_row_dup_pairs",
            expected="at most 1 valid_to=NULL row per (issuer, classification)",
            observed=f"{n_open_dup} pairs with >1 open row",
        ))

    passed = not failures
    duration_ms = int((time.perf_counter() - started) * 1000)
    if not passed:
        logger.warning(
            "tpcore.validation.issuer_securities_integrity.fail",
            zero=n_zero, invalid=n_invalid, open_dup_pairs=n_open_dup,
        )
    return CheckResult(
        name=CHECK_NAME, passed=passed,
        total=n_zero + n_invalid + n_open_dup,
        failed=len(failures), duration_ms=duration_ms, failures=failures,
    )


__all__ = ["CHECK_NAME", "check_issuer_securities_integrity"]
