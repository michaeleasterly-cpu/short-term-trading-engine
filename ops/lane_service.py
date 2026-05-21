"""Consolidated data-lane + advisory-lane daemon — the 2-daemon Railway
budget fix (2026-05-21).

Background: Railway's 2-daemon limit clashes with the local
``install_all_daemons.sh`` whitelist that installs THREE long-lived
daemons:

  1. engine-service                 (sweep + monitor + digest)
  2. data-repair-service            (DATA lane — ENGINE_DATA_REQUEST)
  3. llm-triage-service             (advisory: data/engine/lab co-tasks)

This module fuses (2) and (3) into ONE daemon process running FOUR
co-tasks under one ``asyncio.gather()`` — the canonical sibling of
``engine_service``'s sweep/monitor pair:

  * ``data_repair`` — the deterministic data-repair responder
    (``ops.data_repair_service._main_loop`` polls ``ENGINE_DATA_REQUEST``
    and emits exactly one terminal reply per request_id).
  * ``triage_data`` — the AUTONOMOUS data-recovery co-task
    (``ops.llm_triage_service._main_loop``).
  * ``triage_engine`` — the advisory engine triage co-task
    (``ops.llm_triage_service._engine_loop``).
  * ``triage_lab_emitter`` — the SP-G lab-emitter co-task
    (``ops.llm_triage_service._lab_emitter_loop``).

Both source modules (``ops.data_repair_service`` /
``ops.llm_triage_service``) remain intact as importable libraries —
this daemon is a thin orchestrator that imports + supervises their
existing main loops. NO behavioural rewrite of the lanes: the
autonomous-data-recovery surface (frozen whitelist + deterministic
validator + bounded subprocess in ``ops.llm_data_recovery``), the
engine-lane PR-gated advisory module, and the SP-G operator-command
path are ALL preserved unchanged.

Why one process: the four co-tasks are I/O-bound poll loops on
``platform.application_log`` with non-overlapping event-type filters.
They never compete for shared mutable state. The combined steady-state
pool footprint is ≤ 6 connections; the asyncpg pool max is set to that
ceiling here.

Locks: the data-repair lane keeps its OWN ``ste-data-operations.lock``
(serializes vs ``run_data_operations.sh`` Step-4 self-heal). The three
triage lanes share the ``ste-llm-triage-service.lock`` (serializes vs
ad-hoc ``python -m ops.llm_triage_service``). The two lock names stay
distinct on purpose — different mutual-exclusion domains.

Crash isolation: each co-task is wrapped in a ``_run_supervised``
restart-on-error loop (mirrors engine_service / the prior llm_triage
container). A single lane crashing logs + restarts in-process; never
brings down a sibling or the daemon.

Two-daemon invariant: after this PR ``install_all_daemons.sh``'s closed
whitelist is ``{engine-service, lane-service, data-operations}`` — two
long-lived daemons (engine + lane) plus one cron (data-operations) →
fits Railway's 2-daemon budget. The retired installers
(``install_launchd_data_repair_service.sh``,
``install_launchd_llm_triage_service.sh``) are deleted in the same PR.
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
from ops.llm_triage_service import (
    DEFAULT_LOCK_DIR as TRIAGE_LOCK_DIR,
)
from ops.llm_triage_service import (
    _engine_loop as _triage_engine_loop,
)
from ops.llm_triage_service import (
    _lab_emitter_loop as _triage_lab_emitter_loop,
)
from ops.llm_triage_service import (
    _main_loop as _triage_data_main_loop,
)
from tpcore.db import build_asyncpg_pool

logger = structlog.get_logger(__name__)

# Two-daemon-budget rollup pool size: one acquire per co-task's poll
# tick + each lane's run_triage / heal can take an extra acquire +
# headroom for the deterministic self-heal acquire-while-poll. Sized
# loosely; asyncpg reuses connections so the actual concurrent count
# is far below this ceiling.
POOL_MAX_SIZE = 6

LANE_NAMES = (
    "data_repair",
    "triage_data",
    "triage_engine",
    "triage_lab_emitter",
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
    ``engine_service._run_supervised`` / ``llm_triage_service._run_supervised``.
    CancelledError propagates (clean shutdown)."""
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

    # The two locks stay distinct — the data-lane lock serializes vs
    # run_data_operations.sh Step-4 self-heal; the triage lock
    # serializes vs ad-hoc `python -m ops.llm_triage_service`.
    data_repair_lock_dir = os.environ.get(
        "STE_DATA_OPS_LOCK_DIR", DATA_REPAIR_LOCK_DIR
    )
    triage_lock_dir = os.environ.get(
        "STE_LLM_TRIAGE_LOCK_DIR", TRIAGE_LOCK_DIR
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
        triage_lock_dir=triage_lock_dir,
    )

    # Lane factories — each is a 0-arg coroutine that delegates into
    # the source module's main loop. NO behavioural rewrite: the
    # daemon is a thin orchestrator.
    async def _data_repair_factory():
        await _data_repair_main_loop(pool, stop_event, data_repair_lock_dir)

    async def _triage_data_factory():
        await _triage_data_main_loop(pool, stop_event, triage_lock_dir)

    async def _triage_engine_factory():
        await _triage_engine_loop(pool, stop_event, triage_lock_dir)

    async def _triage_lab_emitter_factory():
        await _triage_lab_emitter_loop(pool, stop_event, triage_lock_dir)

    tasks = {
        "data_repair": asyncio.create_task(
            _run_supervised("data_repair", _data_repair_factory, stop_event)
        ),
        "triage_data": asyncio.create_task(
            _run_supervised("triage_data", _triage_data_factory, stop_event)
        ),
        "triage_engine": asyncio.create_task(
            _run_supervised("triage_engine", _triage_engine_factory, stop_event)
        ),
        "triage_lab_emitter": asyncio.create_task(
            _run_supervised(
                "triage_lab_emitter",
                _triage_lab_emitter_factory,
                stop_event,
            )
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
