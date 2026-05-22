"""Local LLM-agent service daemon — lab-emitter + edge-finder +
outcome-monitor lanes (SP-G + Task #25).

2026-05-22 — Operator directive ("we aren't going to use the llm triage
... take it out") removed the entire LLM-TRIAGE stack from the repo.
The data-triage and engine-triage co-tasks that previously lived here
have been DELETED. This daemon now hosts ONLY the surviving Lab-side
LLM agents:

  * LAB-EMITTER lane (SP-G) — the SP-G spec emitter (engine-spec
    generation, NOT triage). v1 trigger is the operator-command path
    (``/lab-spec-emit`` skill); the trigger event-type tuple is empty
    by design (operator Q6 deferral).
  * EDGE-FINDER lane (Task #25 T10) — the autonomous edge-discovery
    finder. v1 trigger is the operator slash-skill
    (``/lab-edge-find``); the trigger event-type tuple is empty in v1
    by design.
  * OUTCOME-MONITOR lane (Task #25 Phase E/F) — paper-engine outcome
    tracking + auto-retire on bleed-cap / inactivity / failure.

This daemon is NOT a deployed daemon — the deployed surface is
``ops/lane_service.py`` (deterministic self-heal only, no Anthropic
SDK). This file is for OPERATOR-LOCAL invocation only (operator's
Claude Max account). The Anthropic SDK is imported transitively via
the lane modules — exclusively by intent.

Safety boundary: this daemon imports ONLY
``ops.llm_lab_emitter`` (SP-G), ``ops.llm_edge_finder`` (Task #25),
``ops.llm_finder_outcome_monitor`` (Task #25), and stdlib /
asyncpg / structlog. The data-triage / engine-triage modules
(``ops.llm_data_recovery``, ``ops.llm_data_triage``,
``ops.engine_llm_triage``) and their tpcore packages have been
DELETED — any future re-import attempt would be a regression of the
2026-05-22 architectural decision.

Idempotence: each lane tracks the latest ``recorded_at`` seen and only
fires on strictly-newer events. On first start each cursor initializes
to ``now() - 1h`` so a restart doesn't replay old events. With all
three KEEP lanes carrying empty trigger tuples in v1, the daemon
currently polls and does nothing — the operator-command path is the
v1 invocation route. KeepAlive=true at the launchd layer restarts the
process on crash; ``_run_supervised`` restarts a crashed lane in-process.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import structlog

from ops.llm_edge_finder import (
    EDGE_FINDER_TRIGGER_EVENT_TYPES,
    run_edge_finder_cotask,
)
from ops.llm_finder_outcome_monitor import (
    OUTCOME_MONITOR_TRIGGER_EVENT_TYPES,
    run_outcome_monitor_cotask,
)
from ops.llm_lab_emitter import (
    LAB_EMITTER_TRIGGER_EVENT_TYPES,
    run_lab_emitter_cotask,
)
from tpcore.db import build_asyncpg_pool

logger = structlog.get_logger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent

POLL_INTERVAL_SEC = 60
INITIAL_CURSOR_LOOKBACK = timedelta(hours=1)
# Sibling-parity self-exclusion lock (mirrors
# ops/data_repair_service.py's mkdir-atomic / dead-pid-reclaim
# protocol). It guards an ad-hoc concurrent ``python -m
# ops.llm_triage_service`` invocation from racing itself — two passes
# racing the same ``git worktree add`` would contend.
DEFAULT_LOCK_DIR = os.path.join(
    os.environ.get("TMPDIR", "/tmp"), "ste-llm-triage-service.lock"
)
# poll (1 per lane) + each lane's cotask acquires + headroom; THREE
# KEEP lanes (lab_emitter / edge_finder / outcome_monitor) share the
# one pool so we cap at 5.
POOL_MAX_SIZE = 5


async def _find_new_trigger(
    pool,
    cursor: datetime,
    event_types: tuple[str, ...],
) -> datetime | None:
    """Return the recorded_at of the newest trigger event > cursor.

    Filters ``event_type = ANY($1) AND recorded_at > cursor`` and
    returns the newest ``recorded_at`` (or ``None`` if none). ONE poll
    idiom, lane-agnostic by parameter.
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
            list(event_types),
            cursor,
        )
        return row["recorded_at"] if row else None


# ────────────────────────────────────────────────────────────────────────
# Self-exclusion lock — mirrors ops/data_repair_service.py verbatim
# (mkdir-atomic acquire, dead-pid reclaim, owned-only release).
# ────────────────────────────────────────────────────────────────────────


class LockHeldByLiveProcess(Exception):
    """The local-agents lock is held by a live, different pid — SKIP this
    invocation (a pass is already running; advisory, no defer queue —
    the launchd daemon will catch the trigger on its next tick)."""


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but owned by another user — treat as alive.
        return True
    return True


def _acquire_lock(lock_dir: str) -> None:
    """``mkdir``-atomic acquire. On FileExistsError: read ``pid``; if
    that pid is alive raise :class:`LockHeldByLiveProcess`; if dead,
    reclaim (rmtree) and retry the acquire once. On success write our
    pid to ``<lock_dir>/pid``."""
    try:
        os.mkdir(lock_dir)
    except FileExistsError:
        pid_path = os.path.join(lock_dir, "pid")
        holder = ""
        try:
            with open(pid_path, encoding="utf-8") as fh:
                holder = fh.read().strip()
        except OSError:
            holder = ""
        if holder and holder.isdigit() and _pid_alive(int(holder)):
            raise LockHeldByLiveProcess(holder) from None
        logger.info("llm_triage_service.lock_reclaim", stale_pid=holder or "?")
        shutil.rmtree(lock_dir, ignore_errors=True)
        os.mkdir(lock_dir)  # reclaim retry once; a 2nd race is a real error
    with open(os.path.join(lock_dir, "pid"), "w", encoding="utf-8") as fh:
        fh.write(str(os.getpid()))


def _owns_lock(lock_dir: str) -> bool:
    """True iff the lock's pid file names THIS process — so cleanup
    never removes a lock held by the other (concurrent) invocation."""
    try:
        with open(os.path.join(lock_dir, "pid"), encoding="utf-8") as fh:
            return fh.read().strip() == str(os.getpid())
    except OSError:
        return False


def _release_lock(lock_dir: str, *, only_if_owned: bool = False) -> None:
    if only_if_owned and not _owns_lock(lock_dir):
        return
    shutil.rmtree(lock_dir, ignore_errors=True)


def _startup_worktree_prune() -> None:
    """Best-effort, crash-isolated `git worktree prune` at daemon
    startup. A prior cycle that hard-crashed mid `git worktree add`
    leaves an orphaned worktree admin entry; this reclaims it once
    before any work. NEVER raises — a git failure (git absent, not a
    repo, timeout, non-zero) is logged at WARNING and the daemon
    proceeds to the poll loop. No shell, list-args, cwd = repo root
    (mirrors this daemon's crash-isolation idiom)."""
    try:
        subprocess.run(  # noqa: S603 — fixed list-args, no shell, no user input
            ["git", "worktree", "prune", "-v"],
            cwd=str(_REPO_ROOT),
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        logger.info("llm_triage_service.startup_worktree_prune_ok")
    except Exception as exc:  # noqa: BLE001 — best-effort; NEVER abort startup
        logger.warning(
            "llm_triage_service.startup_worktree_prune_failed", error=str(exc)
        )


async def _lane_loop(
    pool,
    stop_event: asyncio.Event,
    lock_dir: str,
    *,
    event_types: tuple[str, ...],
    triage_fn,
    lane: str,
) -> None:
    """ONE cursor-poll lane loop, lane-agnostic by parameter (lab_emitter,
    edge_finder, outcome_monitor all delegate here — the idiom is reused
    verbatim, never re-authored per lane).

    Cursor-polls ``platform.application_log`` for ``event_types`` >
    cursor; on a strictly-newer trigger acquires the SHARED mkdir-atomic
    self-exclusion lock (so two lanes — or an ad-hoc concurrent
    ``python -m ops.llm_triage_service`` — can never race a ``git
    worktree add``) and fires ONE ``triage_fn(pool)``. Advisory +
    crash-isolated: a failure is logged and the loop continues. With
    all v1 lanes carrying empty ``event_types`` tuples this loop is a
    safe no-op (the operator-command path is the v1 trigger).
    """
    cursor = datetime.now(UTC) - INITIAL_CURSOR_LOOKBACK
    logger.info(
        "llm_triage_service.lane_started",
        lane=lane,
        triggers=list(event_types),
        poll_interval_sec=POLL_INTERVAL_SEC,
        initial_cursor=cursor.isoformat(),
        lock_dir=lock_dir,
    )
    while not stop_event.is_set():
        # Empty trigger tuple ⇒ nothing to poll for; just sleep + recheck.
        if event_types:
            try:
                newest = await _find_new_trigger(pool, cursor, event_types)
            except Exception as exc:
                logger.error(
                    "llm_triage_service.poll_failed", lane=lane, error=str(exc)
                )
                newest = None

            if newest is not None and newest > cursor:
                logger.info(
                    "llm_triage_service.trigger_seen",
                    lane=lane,
                    recorded_at=newest.isoformat(),
                )
                cursor = newest
                try:
                    _acquire_lock(lock_dir)
                except LockHeldByLiveProcess as exc:
                    logger.info(
                        "llm_triage_service.lock_skip",
                        lane=lane,
                        holder=str(exc),
                    )
                else:
                    try:
                        await triage_fn(pool)
                    except Exception as exc:  # noqa: BLE001 — isolate; advisory
                        logger.error(
                            "llm_triage_service.triage_failed",
                            lane=lane,
                            error=str(exc),
                        )
                    finally:
                        _release_lock(lock_dir, only_if_owned=True)

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=POLL_INTERVAL_SEC)
        except TimeoutError:
            pass


async def _lab_emitter_loop(
    pool, stop_event: asyncio.Event, lock_dir: str = DEFAULT_LOCK_DIR
) -> None:
    """LAB-EMITTER co-task (SP-G Phase 1). One process-global startup
    prune (the data-triage lane previously owned this; now the
    lab_emitter is the first lane to start)."""
    _startup_worktree_prune()
    await _lane_loop(
        pool,
        stop_event,
        lock_dir,
        event_types=LAB_EMITTER_TRIGGER_EVENT_TYPES,
        triage_fn=run_lab_emitter_cotask,
        lane="lab_emitter",
    )


async def _edge_finder_loop(
    pool, stop_event: asyncio.Event, lock_dir: str = DEFAULT_LOCK_DIR
) -> None:
    """EDGE-FINDER co-task (Task #25 T10). Empty trigger tuple in v1;
    the operator slash-skill (``/lab-edge-find``) is the v1 invocation
    route."""
    await _lane_loop(
        pool,
        stop_event,
        lock_dir,
        event_types=EDGE_FINDER_TRIGGER_EVENT_TYPES,
        triage_fn=run_edge_finder_cotask,
        lane="edge_finder",
    )


async def _outcome_monitor_loop(
    pool, stop_event: asyncio.Event, lock_dir: str = DEFAULT_LOCK_DIR
) -> None:
    """OUTCOME-MONITOR co-task (Task #25 Phase E/F). Empty trigger tuple
    in v1; the operator slash-skill / a future ``NYSE_SESSION_CLOSE``
    event-emitter PR are the v1+ invocation routes."""
    await _lane_loop(
        pool,
        stop_event,
        lock_dir,
        event_types=OUTCOME_MONITOR_TRIGGER_EVENT_TYPES,
        triage_fn=run_outcome_monitor_cotask,
        lane="outcome_monitor",
    )


async def _run_supervised(
    name: str, factory, stop_event: asyncio.Event, backoff: float = 5.0
) -> None:
    """Run ``factory()`` (a 0-arg coroutine fn) until stop_event; an
    Exception is logged and the lane restarted after ``backoff``. Mirrors
    the crash-isolation CONTRACT of ``engine_service._run_supervised``
    (CancelledError propagates; non-Cancelled Exception is logged +
    backoff-restarted, never propagated)."""
    while not stop_event.is_set():
        try:
            await factory()
            return  # clean completion
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — restart, don't propagate
            logger.error(
                "llm_triage_service.lane_crashed", lane=name, error=str(exc)
            )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=backoff)
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

    lock_dir = os.environ.get("STE_LLM_TRIAGE_LOCK_DIR", DEFAULT_LOCK_DIR)
    pool = await build_asyncpg_pool(dsn, max_size=POOL_MAX_SIZE)
    stop_event = asyncio.Event()

    def _handle_signal(signum):
        logger.info("llm_triage_service.signal_received", signum=signum)
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal, sig)

    # THREE independent _run_supervised co-tasks on the ONE advisory pool
    # — the LAB-EMITTER, EDGE-FINDER, and OUTCOME-MONITOR lanes. Each is
    # crash-isolated from the others; all share this ``pool`` and the
    # single ``lock_dir`` self-exclusion lock.
    async def _lab_emitter_factory():
        await _lab_emitter_loop(pool, stop_event, lock_dir)

    async def _edge_finder_factory():
        await _edge_finder_loop(pool, stop_event, lock_dir)

    async def _outcome_monitor_factory():
        await _outcome_monitor_loop(pool, stop_event, lock_dir)

    lab_emitter_task = asyncio.create_task(
        _run_supervised("lab_emitter", _lab_emitter_factory, stop_event))
    edge_finder_task = asyncio.create_task(
        _run_supervised("edge_finder", _edge_finder_factory, stop_event))
    outcome_monitor_task = asyncio.create_task(
        _run_supervised("outcome_monitor", _outcome_monitor_factory, stop_event))
    try:
        # Exit on signal (stop_event) OR if all lanes have exited
        # (nothing left to supervise — don't zombie the process).
        stop_waiter = asyncio.ensure_future(stop_event.wait())
        all_done = asyncio.gather(
            lab_emitter_task, edge_finder_task, outcome_monitor_task,
        )
        _done, _pending = await asyncio.wait(
            {stop_waiter, all_done},
            return_when=asyncio.FIRST_COMPLETED)
        stop_waiter.cancel()
        all_done.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stop_waiter
        with contextlib.suppress(BaseException):
            await all_done
    finally:
        for t in (lab_emitter_task, edge_finder_task, outcome_monitor_task):
            t.cancel()
        await asyncio.gather(
            lab_emitter_task, edge_finder_task, outcome_monitor_task,
            return_exceptions=True)
        # Defensive: never leave the lock held on shutdown if a pass was
        # interrupted mid-flight. Only release if WE own it.
        _release_lock(lock_dir, only_if_owned=True)
        await pool.close()
        logger.info("llm_triage_service.stopped")
    return 0


def main() -> None:  # pragma: no cover - CLI shim
    sys.exit(asyncio.run(_amain()))


if __name__ == "__main__":  # pragma: no cover
    main()
