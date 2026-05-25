"""Daemon-freshness check — catches a silently-stopped daemon.

The 2026-05-22→25 incident (the trust-audit P0 finding): the data lane
was broken for 3+ days because the ``data_operations`` cron stopped
writing its heartbeat; the operator only noticed because trades stopped.
``trade_monitor`` heartbeats stayed fresh (engine-side polling alive),
masking the fact that the data-side lane had collapsed.

This check enforces the per-daemon liveness contract: every tracked
daemon must have written a heartbeat within its declared maximum age.
ONE missing → check fails.

Healable=False on the HealSpec side — no canonical ``ops.py`` stage
restarts a daemon. The correct action on RED is operator restart of
the named daemon (via launchd / systemd). Hardcoded thresholds reflect
the daemon's own heartbeat cadence + 1 grace cycle: a 60s-poll daemon
gets 1h tolerance; a 24h-cron daemon gets 26h tolerance.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import structlog

from tpcore.quality.validation.models import CheckResult, FailureDetail

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)

CHECK_NAME = "daemon_freshness"

# Per-daemon maximum heartbeat age (seconds). Keyed on
# ``platform.daemon_heartbeats.daemon_name``. The cadence in the
# comment is the design contract from the daemon's own loop; the
# threshold is contract-cadence plus 1 grace cycle so a single missed
# tick doesn't red-flag a healthy daemon.
DAEMON_MAX_AGE_SECS: dict[str, int] = {
    # data_operations: 1×/day cron (run_data_operations.sh). 24h + 2h grace.
    "data_operations": 26 * 3600,
    # engine_service: 60s poll loop. 1h grace = 60 missed ticks.
    "engine_service": 3600,
    # allocator: fires on ENGINE-DISPATCH events; idle gap can be hours
    # legitimately, so 6h tolerance.
    "allocator": 6 * 3600,
    # trade_monitor: ~6min heartbeat. 1h grace = 10 missed ticks.
    "trade_monitor": 3600,
}

_LIVE_HEARTBEATS_SQL = (
    "SELECT daemon_name, last_heartbeat, "
    "EXTRACT(EPOCH FROM (NOW() - last_heartbeat))::bigint AS age_secs "
    "FROM platform.daemon_heartbeats"
)


async def check_daemon_freshness(
    pool: asyncpg.Pool,
    source: Any = None,
) -> CheckResult:
    """Verify every tracked daemon's heartbeat is within its declared
    max age. Missing-row counts as STALE (the daemon has never written)."""
    del source
    started = time.perf_counter()
    failures: list[FailureDetail] = []

    async with pool.acquire() as conn:
        rows = await conn.fetch(_LIVE_HEARTBEATS_SQL)
    live: dict[str, tuple[Any, int]] = {
        r["daemon_name"]: (r["last_heartbeat"], int(r["age_secs"] or 0))
        for r in rows
    }

    for daemon, max_age in DAEMON_MAX_AGE_SECS.items():
        if daemon not in live:
            failures.append(FailureDetail(
                ticker=daemon,
                reason="daemon_missing",
                expected=f"heartbeat within {max_age // 3600}h",
                observed=(
                    f"platform.daemon_heartbeats has no row for "
                    f"daemon_name='{daemon}' — daemon has never written "
                    "or row was deleted; restart the daemon"
                ),
            ))
            continue
        last_hb, age_secs = live[daemon]
        if age_secs > max_age:
            failures.append(FailureDetail(
                ticker=daemon,
                reason="daemon_stale",
                expected=f"heartbeat within {max_age // 3600}h ({max_age}s)",
                observed=(
                    f"last_heartbeat={last_hb.isoformat()} "
                    f"({age_secs // 3600}h ago, age_secs={age_secs}); "
                    "daemon process is dead or stuck — restart"
                ),
            ))

    if failures:
        logger.warning(
            "tpcore.validation.daemon_freshness.stale",
            stale_daemons=[f.ticker for f in failures],
        )
    else:
        logger.info(
            "tpcore.validation.daemon_freshness.ok",
            tracked=len(DAEMON_MAX_AGE_SECS),
        )

    duration_ms = int((time.perf_counter() - started) * 1000)
    return CheckResult(
        name=CHECK_NAME,
        passed=len(failures) == 0,
        total=len(DAEMON_MAX_AGE_SECS),
        failed=len(failures),
        duration_ms=duration_ms,
        failures=failures,
    )


__all__ = [
    "CHECK_NAME",
    "DAEMON_MAX_AGE_SECS",
    "check_daemon_freshness",
]
