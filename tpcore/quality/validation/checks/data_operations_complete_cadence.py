"""DATA_OPERATIONS_COMPLETE cadence check — catches a silently-stopped lane.

The 2026-05-22→25 incident (the trust-audit P0 finding): the data lane
had NEVER emitted ``DATA_OPERATIONS_COMPLETE`` in live history despite
121 partial-step ``INGESTION_COMPLETE`` markers. The gate worked
correctly (it refused to fire on red data), but nothing surfaced to
the operator that the gate had been red for 3+ days.

This check enforces the lane-emission contract directly:
``platform.application_log`` must contain a ``DATA_OPERATIONS_COMPLETE``
row with ``recorded_at`` within the last ``MAX_AGE_SECS``. NULL (never
emitted) is RED; staler than the threshold is RED.

Healable=False on the HealSpec side — emitting the event is the
END product of a fully-green data lane run; no canonical ``ops.py``
stage emits it directly. The correct action on RED is operator
investigation of what's keeping the lane out of green.

The 24h threshold is the operator-stated contract: the lane is
expected to run at least once per day. A grace window of 6h on top
(30h total) keeps a single skipped weekend cron from flagging.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import structlog

from tpcore.quality.validation.models import CheckResult, FailureDetail

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)

CHECK_NAME = "data_operations_complete_cadence"

# 24h target + 6h grace = 30h before RED. Tuned to the once-daily
# data_operations cron; a longer outage IS a real operational gap and
# should escalate.
MAX_AGE_SECS = 30 * 3600

_LAST_EMIT_SQL = (
    "SELECT MAX(recorded_at) AS last_emit, "
    "COUNT(*) AS total_emits "
    "FROM platform.application_log "
    "WHERE event_type = 'DATA_OPERATIONS_COMPLETE'"
)


async def check_data_operations_complete_cadence(
    pool: asyncpg.Pool,
    source: Any = None,
) -> CheckResult:
    """Verify DATA_OPERATIONS_COMPLETE has been emitted within the
    cadence window. NULL last_emit (never fired) is RED."""
    del source
    started = time.perf_counter()
    failures: list[FailureDetail] = []

    async with pool.acquire() as conn:
        row = await conn.fetchrow(_LAST_EMIT_SQL)

    last_emit = row["last_emit"]
    total = int(row["total_emits"] or 0)

    if last_emit is None:
        failures.append(FailureDetail(
            ticker="<lane>",
            reason="never_emitted",
            expected=(
                f"DATA_OPERATIONS_COMPLETE within last "
                f"{MAX_AGE_SECS // 3600}h"
            ),
            observed=(
                "platform.application_log has ZERO rows where "
                "event_type='DATA_OPERATIONS_COMPLETE' — the data "
                "lane has never reached 100% green and emitted the "
                "gate event. Investigate validation suite + self-heal "
                "escalations."
            ),
        ))
    else:
        from datetime import UTC, datetime
        now = datetime.now(UTC)
        age_secs = int((now - last_emit).total_seconds())
        if age_secs > MAX_AGE_SECS:
            failures.append(FailureDetail(
                ticker="<lane>",
                reason="lane_stale",
                expected=(
                    f"DATA_OPERATIONS_COMPLETE within last "
                    f"{MAX_AGE_SECS // 3600}h"
                ),
                observed=(
                    f"last DATA_OPERATIONS_COMPLETE at "
                    f"{last_emit.isoformat()} "
                    f"({age_secs // 3600}h ago, total emits={total}). "
                    "The data lane has not reached 100% green within "
                    "the daily cadence window — investigate."
                ),
            ))

    if failures:
        logger.warning(
            "tpcore.validation.data_operations_complete_cadence.stale",
            last_emit=last_emit.isoformat() if last_emit else None,
            total_emits=total,
        )
    else:
        logger.info(
            "tpcore.validation.data_operations_complete_cadence.ok",
            last_emit=last_emit.isoformat() if last_emit else None,
        )

    duration_ms = int((time.perf_counter() - started) * 1000)
    return CheckResult(
        name=CHECK_NAME,
        passed=len(failures) == 0,
        total=1,
        failed=len(failures),
        duration_ms=duration_ms,
        failures=failures,
    )


__all__ = [
    "CHECK_NAME",
    "MAX_AGE_SECS",
    "check_data_operations_complete_cadence",
]
