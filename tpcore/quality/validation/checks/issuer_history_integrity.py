"""issuer_history SCD-2 integrity invariants.

FAIL if any of these defect classes exist (caught operator 2026-05-25
when META duplicate became 2,061 overlap pairs once we looked):

  - Zero-duration rows (valid_from = valid_to) — useless
  - Invalid range (valid_to < valid_from) — broken
  - Overlapping windows per issuer
  - More than 1 row with valid_to IS NULL per issuer

The corresponding healer stage is `issuer_history_cleanup`, which
rewrites each issuer's history into a non-overlapping chain via
LEAD-window-function UPDATE.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import structlog

from tpcore.quality.validation.models import CheckResult, FailureDetail

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)

CHECK_NAME = "issuer_history_integrity"


async def check_issuer_history_integrity(
    pool: asyncpg.Pool, source: Any = None,
) -> CheckResult:
    """SCD-2 integrity for platform.issuer_history."""
    del source
    started = time.perf_counter()

    async with pool.acquire() as conn:
        n_zero = int(await conn.fetchval(
            "SELECT count(*) FROM platform.issuer_history "
            "WHERE valid_from = valid_to"
        ) or 0)
        n_invalid = int(await conn.fetchval(
            "SELECT count(*) FROM platform.issuer_history "
            "WHERE valid_to IS NOT NULL AND valid_to < valid_from"
        ) or 0)
        n_overlap = int(await conn.fetchval(
            """
            SELECT count(*) FROM platform.issuer_history a
            JOIN platform.issuer_history b
              ON a.issuer_id = b.issuer_id AND a.valid_from < b.valid_from
            WHERE (a.valid_to IS NULL OR a.valid_to > b.valid_from)
            """
        ) or 0)
        n_open_dup_issuers = int(await conn.fetchval(
            """
            SELECT count(*) FROM (
                SELECT issuer_id FROM platform.issuer_history
                WHERE valid_to IS NULL
                GROUP BY issuer_id HAVING count(*) > 1
            ) s
            """
        ) or 0)

    failures: list[FailureDetail] = []
    if n_zero > 0:
        failures.append(FailureDetail(
            ticker="(table)", reason="zero_duration_rows",
            expected="0 rows where valid_from = valid_to",
            observed=f"{n_zero} zero-duration rows",
        ))
    if n_invalid > 0:
        failures.append(FailureDetail(
            ticker="(table)", reason="invalid_range_rows",
            expected="0 rows where valid_to < valid_from",
            observed=f"{n_invalid} invalid-range rows",
        ))
    if n_overlap > 0:
        failures.append(FailureDetail(
            ticker="(table)", reason="overlapping_windows",
            expected="0 overlapping windows per issuer",
            observed=f"{n_overlap} overlapping pairs",
        ))
    if n_open_dup_issuers > 0:
        failures.append(FailureDetail(
            ticker="(table)", reason="open_row_dup_issuers",
            expected="at most 1 valid_to=NULL row per issuer",
            observed=f"{n_open_dup_issuers} issuers with >1 open row",
        ))

    passed = not failures
    duration_ms = int((time.perf_counter() - started) * 1000)
    if not passed:
        logger.warning(
            "tpcore.validation.issuer_history_integrity.fail",
            zero=n_zero, invalid=n_invalid, overlap=n_overlap,
            open_dup_issuers=n_open_dup_issuers,
        )
    return CheckResult(
        name=CHECK_NAME, passed=passed,
        total=n_zero + n_invalid + n_overlap + n_open_dup_issuers,
        failed=len(failures), duration_ms=duration_ms, failures=failures,
    )


__all__ = ["CHECK_NAME", "check_issuer_history_integrity"]
