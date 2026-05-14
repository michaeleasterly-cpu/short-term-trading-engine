"""Engine-service daemon — fires the engine sweep on ``DATA_OPERATIONS_COMPLETE``.

Phase 5 of engine standardization (2026-05-14). Decouples engine
execution from the data-operations workflow:

    Before: ``scripts/run_data_operations.sh`` Step 6 called
            ``scripts/run_all_engines.sh`` synchronously.
    After:  ``run_data_operations.sh`` writes a single
            ``DATA_OPERATIONS_COMPLETE`` row to
            ``platform.application_log`` on success; this daemon polls
            for that event every 60s and shells out to
            ``scripts/run_all_engines.sh`` when one appears.

Why split them: data-ops latency was bleeding into the trade-submit
window, and any engine failure (rare but possible) would mark the
whole nightly workflow red even though the data layer was fine. With
the daemon, the operator sees data ops succeed / fail on its own
notification, and engine failures are isolated to ``engine-service.log``.

Idempotence: tracks the latest ``recorded_at`` seen and only fires on
strictly-newer events. On first start the cursor initializes to
``now() - 1h`` so a freshly-restarted daemon doesn't replay events
older than the typical data-ops window.

KeepAlive=true at the launchd layer restarts the process on crash;
this loop has no internal restart, just clean exits + reconnection.
"""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import structlog

from tpcore.db import build_asyncpg_pool

logger = structlog.get_logger(__name__)

POLL_INTERVAL_SEC = 60
INITIAL_CURSOR_LOOKBACK = timedelta(hours=1)
TRIGGER_EVENT_TYPE = "DATA_OPERATIONS_COMPLETE"
SWEEP_SCRIPT = "scripts/run_all_engines.sh"


async def _find_new_trigger(pool, cursor: datetime) -> datetime | None:
    """Return the recorded_at of the newest DATA_OPERATIONS_COMPLETE > cursor.

    Returns None if no new event since ``cursor``.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT recorded_at
            FROM platform.application_log
            WHERE event_type = $1
              AND recorded_at > $2
            ORDER BY recorded_at DESC
            LIMIT 1
            """,
            TRIGGER_EVENT_TYPE,
            cursor,
        )
        return row["recorded_at"] if row else None


def _run_engine_sweep() -> int:
    """Shell out to ``scripts/run_all_engines.sh`` and return its exit code."""
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cmd = [os.path.join(repo_root, SWEEP_SCRIPT)]
    logger.info("engine_service.sweep_start", cmd=cmd)
    result = subprocess.run(cmd, cwd=repo_root, check=False)
    logger.info("engine_service.sweep_done", returncode=result.returncode)
    return result.returncode


async def _main_loop(pool, stop_event: asyncio.Event) -> None:
    cursor = datetime.now(UTC) - INITIAL_CURSOR_LOOKBACK
    logger.info(
        "engine_service.started",
        trigger=TRIGGER_EVENT_TYPE,
        poll_interval_sec=POLL_INTERVAL_SEC,
        initial_cursor=cursor.isoformat(),
    )

    while not stop_event.is_set():
        try:
            newest = await _find_new_trigger(pool, cursor)
        except Exception as exc:
            logger.error("engine_service.poll_failed", error=str(exc))
            newest = None

        if newest is not None and newest > cursor:
            logger.info("engine_service.trigger_seen", recorded_at=newest.isoformat())
            cursor = newest
            # Run the sweep synchronously — we don't want to fire
            # overlapping sweeps if data-ops emits two events close
            # together. The next poll picks up any newer trigger.
            await asyncio.get_event_loop().run_in_executor(None, _run_engine_sweep)

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=POLL_INTERVAL_SEC)
        except TimeoutError:
            pass


async def _amain() -> int:
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_URL_IPV4")
    if not dsn:
        logger.error("engine_service.no_dsn", note="set DATABASE_URL or DATABASE_URL_IPV4")
        return 1

    pool = await build_asyncpg_pool(dsn)
    stop_event = asyncio.Event()

    def _handle_signal(signum):
        logger.info("engine_service.signal_received", signum=signum)
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal, sig)

    try:
        await _main_loop(pool, stop_event)
    finally:
        await pool.close()
        logger.info("engine_service.stopped")
    return 0


def main() -> None:  # pragma: no cover - CLI shim
    sys.exit(asyncio.run(_amain()))


if __name__ == "__main__":  # pragma: no cover
    main()
