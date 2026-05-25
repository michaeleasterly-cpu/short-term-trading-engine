"""corporate_events bitemporal integrity invariants.

PK is (event_id, realtime_start). FAIL if more than one bitemporal-
active row exists for the same event_id (realtime_end IS NULL OR
realtime_end = 'infinity'). Healed by `audit_cleanup_2026_05_24`
which closes older versions' realtime_end.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import structlog

from tpcore.quality.validation.models import CheckResult, FailureDetail

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)

CHECK_NAME = "corporate_events_integrity"


async def check_corporate_events_integrity(
    pool: asyncpg.Pool, source: Any = None,
) -> CheckResult:
    """Bitemporal integrity for platform.corporate_events."""
    del source
    started = time.perf_counter()

    async with pool.acquire() as conn:
        n_open_dup = int(await conn.fetchval(
            """
            SELECT count(*) FROM (
                SELECT event_id FROM platform.corporate_events
                WHERE realtime_end IS NULL
                   OR realtime_end = 'infinity'::timestamptz
                GROUP BY event_id HAVING count(*) > 1
            ) s
            """
        ) or 0)
        # event_date should never be after realtime_start (event
        # happened before we recorded it).
        n_event_after_record = int(await conn.fetchval(
            """
            SELECT count(*) FROM platform.corporate_events
            WHERE event_date > realtime_start::date
            """
        ) or 0)

    failures: list[FailureDetail] = []
    if n_open_dup > 0:
        failures.append(FailureDetail(
            ticker="(table)", reason="bitemporal_open_dups",
            expected="at most 1 realtime-active row per event_id",
            observed=f"{n_open_dup} event_ids with >1 open version",
        ))
    if n_event_after_record > 0:
        failures.append(FailureDetail(
            ticker="(table)", reason="event_after_record",
            expected="event_date <= realtime_start (event happened before we recorded it)",
            observed=f"{n_event_after_record} rows where event_date > realtime_start",
        ))

    passed = not failures
    duration_ms = int((time.perf_counter() - started) * 1000)
    if not passed:
        logger.warning(
            "tpcore.validation.corporate_events_integrity.fail",
            open_dup_events=n_open_dup,
            event_after_record=n_event_after_record,
        )
    return CheckResult(
        name=CHECK_NAME, passed=passed,
        total=n_open_dup + n_event_after_record,
        failed=len(failures), duration_ms=duration_ms, failures=failures,
    )


__all__ = ["CHECK_NAME", "check_corporate_events_integrity"]
