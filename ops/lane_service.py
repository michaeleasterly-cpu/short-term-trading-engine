"""Deployed data-lane daemon — DETERMINISTIC SELF-HEAL ONLY.

Operator directive 2026-05-21 ("we wont be deploying the llm data triage
it will run locally with my max account") + the audit in
``docs/audits/2026-05-22-llm-triage-removal-from-deployed-daemon.md``:
the deployed ``lane-service`` daemon hosts ONLY the deterministic data-
repair responder. ANY code path that calls Anthropic at runtime has been
REMOVED from this daemon — the LLM-side invocations now run OPERATOR-
LOCALLY (operator's Claude Max account) via the slash skills
``/triage-data-failures`` / ``/triage-engine-failures`` /
``/lab-spec-emit``.

Background (predecessor PR #236): the previous lane_service hosted FOUR
co-tasks (``data_repair`` + the three LLM-invoking triage co-tasks
``triage_data`` / ``triage_engine`` / ``triage_lab_emitter``). All three
LLM co-tasks pulled the Anthropic SDK into the deployed process at
module-load time (transitively via ``ops.llm_data_recovery`` /
``ops.engine_llm_triage`` / ``ops.llm_lab_emitter``). This PR removes
them — the LLM-invoking modules stay in the repo as importable
libraries, but the DEPLOYED daemon never loads them.

Architecture:

  * DEPLOYED daemon (this file): ONE co-task — ``data_repair`` — runs the
    deterministic self-heal responder
    (``ops.data_repair_service._main_loop`` polls
    ``ENGINE_DATA_REQUEST`` and emits exactly one terminal reply per
    request_id). NO Anthropic. NO LLM. NO triage.

  * OPERATOR-LOCAL (NOT this file): the operator's machine runs the
    LLM-side via slash skills that read recent escalations from
    ``platform.application_log`` and fire the respective recovery /
    triage / emitter — ``ops.llm_data_recovery`` (data lane),
    ``ops.engine_llm_triage`` (engine lane), ``ops.llm_lab_emitter``
    (SP-G). The deterministic cascade
    (``scripts/ops.py::_auto_cascade_*``) STILL emits the escalation
    events; the operator-local LLM-side observes them.

Two-daemon Railway invariant: PRESERVED — ``install_all_daemons.sh``
still installs exactly ``{engine-service, lane-service,
data-operations}``. lane-service is still ONE long-lived daemon; it
just hosts ONE deterministic co-task now instead of four.

Crash isolation: the single ``data_repair`` co-task is wrapped in
``_run_supervised`` (restart-on-error). With only one lane the
supervisor is a no-op safety net in practice (a deterministic-handler
crash that the lane itself does not self-heal would be a hard bug, not
an everyday event).
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
from datetime import UTC, datetime, timedelta

import structlog

from ops.data_repair_service import (
    DEFAULT_LOCK_DIR as DATA_REPAIR_LOCK_DIR,
)
from ops.data_repair_service import (
    INITIAL_CURSOR_LOOKBACK,
    POLL_INTERVAL_SEC,
)
from ops.data_repair_service import (
    _main_loop as _data_repair_main_loop,
)
from tpcore.db import build_asyncpg_pool

logger = structlog.get_logger(__name__)

# One co-task in the deployed daemon (data_repair). One acquire per poll
# tick + headroom for the in-flight self-heal acquire-while-poll. Sized
# loosely; asyncpg reuses connections so the actual concurrent count is
# well below this ceiling.
POOL_MAX_SIZE = 3

LANE_NAMES = (
    "data_repair",
)


async def _run_supervised(
    name: str,
    factory,
    stop_event: asyncio.Event,
    backoff: float = 5.0,
) -> None:
    """Run ``factory()`` (a 0-arg coroutine fn) until stop_event. A
    non-Cancelled exception is logged and the lane restarted after
    ``backoff`` seconds — mirrors the crash-isolation contract of
    ``engine_service._run_supervised``. CancelledError propagates
    (clean shutdown)."""
    while not stop_event.is_set():
        try:
            await factory()
            return  # clean completion
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — restart, don't propagate
            logger.error(
                "lane_service.lane_crashed", lane=name, error=str(exc)
            )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=backoff)
            except TimeoutError:
                pass


async def _amain() -> int:
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_URL_IPV4")
    if not dsn:
        logger.error(
            "lane_service.no_dsn",
            note="set DATABASE_URL or DATABASE_URL_IPV4",
        )
        return 1

    # Data-repair lock serializes vs run_data_operations.sh Step-4
    # self-heal (operator's data-ops cron path); the deployed daemon
    # only holds this one lock now (the triage lock is operator-local
    # responsibility).
    data_repair_lock_dir = os.environ.get(
        "STE_DATA_OPS_LOCK_DIR", DATA_REPAIR_LOCK_DIR
    )

    pool = await build_asyncpg_pool(dsn, max_size=POOL_MAX_SIZE)
    stop_event = asyncio.Event()

    def _handle_signal(signum: int) -> None:
        logger.info("lane_service.signal_received", signum=signum)
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal, sig)

    logger.info(
        "lane_service.started",
        lanes=list(LANE_NAMES),
        pool_max_size=POOL_MAX_SIZE,
        poll_interval_sec=POLL_INTERVAL_SEC,
        initial_cursor_lookback_sec=int(
            INITIAL_CURSOR_LOOKBACK.total_seconds()
        ),
        data_repair_lock_dir=data_repair_lock_dir,
    )

    async def _data_repair_factory():
        await _data_repair_main_loop(pool, stop_event, data_repair_lock_dir)

    tasks = {
        "data_repair": asyncio.create_task(
            _run_supervised("data_repair", _data_repair_factory, stop_event)
        ),
    }

    try:
        # Exit on signal (stop_event) OR if every lane has exited.
        stop_waiter = asyncio.ensure_future(stop_event.wait())
        all_done = asyncio.gather(*tasks.values())
        done, _pending = await asyncio.wait(
            {stop_waiter, all_done}, return_when=asyncio.FIRST_COMPLETED
        )
        stop_waiter.cancel()
        all_done.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stop_waiter
        with contextlib.suppress(BaseException):
            await all_done
    finally:
        for t in tasks.values():
            t.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
        await pool.close()
        logger.info("lane_service.stopped")

    return 0


def main() -> None:  # pragma: no cover - CLI shim
    sys.exit(asyncio.run(_amain()))


if __name__ == "__main__":  # pragma: no cover
    main()


# ────────────────────────────────────────────────────────────────────────
# Re-exports used by the launchd installer + the two-daemon invariant
# test (so the daemon is structurally discoverable from the same
# constants its source modules expose).
# ────────────────────────────────────────────────────────────────────────
__all__ = [
    "LANE_NAMES",
    "POOL_MAX_SIZE",
    "main",
]


# Silence "unused" for the constants imported only to make the
# module surface (parity with the source modules) — they're part of
# the documented import contract for downstream introspection.
_ = (datetime, timedelta, UTC)
