"""LLM-triage-service daemon — event-driven advisory triage (LT-P3 §4).

Structural sibling of ``ops/engine_service.py`` / ``ops/data_repair_service.py``,
serving the *advisory* path: when the data lane gives up on a data
problem and emits a ``DATA_REPAIR_ESCALATED`` (the request/response
handshake exhausted its bounded repair) or a ``DATA_SOURCE_ESCALATED``
(a source stuck ≥3 held cycles by the datasupervisor), this daemon
polls ``platform.application_log`` for that event and fires one
advisory ``ops.llm_data_triage.run_triage`` pass.

Why event-driven (v2.1): triage is human-review fuel — it must follow
the escalation that produced it, not a cron tick or a linear
data-operations step. There is NO data-ops ordering coupling:
``run_triage`` re-checks the open set itself (``select_novel_escalations``
re-derives novelty from the bus), so a same-cycle deterministic
self-heal that already resolved the escalation makes triage a safe
no-op. The daemon never blocks anything and is fully crash-isolated:
a triage failure is logged and the loop continues — the advisory layer
must never wedge the data or engine lanes.

Safety boundary: this daemon imports ONLY
``ops.llm_data_triage.run_triage`` + stdlib/asyncpg/structlog — NO
actor/mutation path (asserted by the import-isolation AST test). The
LLM/agent NEVER repairs data, runs a stage, mutates a table, trades,
or merges; restoration only ever happens via the deterministic path.

Idempotence: tracks the latest ``recorded_at`` seen and only fires on
strictly-newer events (mirrors engine_service). On first start the
cursor initializes to ``now() - 1h`` so a restart doesn't replay old
escalations. KeepAlive=true at the launchd layer restarts on crash;
this loop has no internal restart, just clean exits + reconnection.
"""
from __future__ import annotations

import asyncio
import os
import signal
import sys
from datetime import UTC, datetime, timedelta

import structlog

from ops.llm_data_triage import run_triage
from tpcore.db import build_asyncpg_pool

logger = structlog.get_logger(__name__)

POLL_INTERVAL_SEC = 60
INITIAL_CURSOR_LOOKBACK = timedelta(hours=1)
# The two data-lane escalation classes: the deterministic lane gave up
# (DATA_REPAIR_ESCALATED — bounded self-heal exhausted) or a source is
# stuck held (DATA_SOURCE_ESCALATED — datasupervisor ≥3 held cycles).
TRIGGER_EVENT_TYPES: tuple[str, ...] = (
    "DATA_REPAIR_ESCALATED",
    "DATA_SOURCE_ESCALATED",
)
POOL_MAX_SIZE = 2  # poll (1) + run_triage's own acquires + headroom


async def _find_new_trigger(pool, cursor: datetime) -> datetime | None:
    """Return the recorded_at of the newest trigger event > cursor.

    Mirrors ``engine_service._find_new_trigger`` exactly: filters
    ``event_type = ANY(TRIGGER_EVENT_TYPES) AND recorded_at > cursor``
    and returns the newest ``recorded_at`` (or ``None`` if none).
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT recorded_at
            FROM platform.application_log
            WHERE event_type = ANY($1::text[])
              AND recorded_at > $2
            ORDER BY recorded_at DESC
            LIMIT 1
            """,
            list(TRIGGER_EVENT_TYPES),
            cursor,
        )
        return row["recorded_at"] if row else None


async def _main_loop(pool, stop_event: asyncio.Event) -> None:
    cursor = datetime.now(UTC) - INITIAL_CURSOR_LOOKBACK
    logger.info(
        "llm_triage_service.started",
        triggers=list(TRIGGER_EVENT_TYPES),
        poll_interval_sec=POLL_INTERVAL_SEC,
        initial_cursor=cursor.isoformat(),
    )
    while not stop_event.is_set():
        try:
            newest = await _find_new_trigger(pool, cursor)
        except Exception as exc:
            logger.error("llm_triage_service.poll_failed", error=str(exc))
            newest = None

        if newest is not None and newest > cursor:
            logger.info(
                "llm_triage_service.trigger_seen",
                recorded_at=newest.isoformat(),
            )
            cursor = newest
            # Advisory + crash-isolated: a triage failure is logged and
            # the loop continues — NEVER block or crash the daemon.
            # run_triage itself re-checks the open set, so a same-cycle
            # self-heal is a safe no-op (no data-ops ordering coupling).
            try:
                await run_triage(pool)
            except Exception as exc:  # noqa: BLE001 — isolate; advisory only
                logger.error(
                    "llm_triage_service.triage_failed", error=str(exc)
                )

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=POLL_INTERVAL_SEC)
        except TimeoutError:
            pass


async def _amain() -> int:
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_URL_IPV4")
    if not dsn:
        logger.error(
            "llm_triage_service.no_dsn",
            note="set DATABASE_URL or DATABASE_URL_IPV4",
        )
        return 1

    pool = await build_asyncpg_pool(dsn, max_size=POOL_MAX_SIZE)
    stop_event = asyncio.Event()

    def _handle_signal(signum):
        logger.info("llm_triage_service.signal_received", signum=signum)
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal, sig)

    try:
        await _main_loop(pool, stop_event)
    finally:
        await pool.close()
        logger.info("llm_triage_service.stopped")
    return 0


def main() -> None:  # pragma: no cover - CLI shim
    sys.exit(asyncio.run(_amain()))


if __name__ == "__main__":  # pragma: no cover
    main()
