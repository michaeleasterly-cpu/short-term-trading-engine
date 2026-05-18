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
import shutil
import signal
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import structlog

from ops.llm_data_triage import run_triage
from tpcore.db import build_asyncpg_pool

logger = structlog.get_logger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent

POLL_INTERVAL_SEC = 60
INITIAL_CURSOR_LOOKBACK = timedelta(hours=1)
# Sibling-parity self-exclusion lock (mirrors
# ops/data_repair_service.py's mkdir-atomic / dead-pid-reclaim
# protocol). It guards an ad-hoc concurrent `python -m
# ops.llm_triage_service` invocation from overlapping the launchd
# daemon — two advisory passes racing select_novel_escalations /
# `git worktree add` would contend. Distinct lock name from the
# data-ops lock (this is the advisory lane, not the data lane).
DEFAULT_LOCK_DIR = os.path.join(
    os.environ.get("TMPDIR", "/tmp"), "ste-llm-triage-service.lock"
)
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


# ────────────────────────────────────────────────────────────────────────
# Self-exclusion lock — mirrors ops/data_repair_service.py verbatim
# (mkdir-atomic acquire, dead-pid reclaim, owned-only release).
# ────────────────────────────────────────────────────────────────────────


class LockHeldByLiveProcess(Exception):
    """The llm-triage lock is held by a live, different pid — SKIP this
    invocation (a triage pass is already running; advisory, no defer
    queue — the launchd daemon will catch the trigger on its next tick)."""


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


async def _main_loop(
    pool, stop_event: asyncio.Event, lock_dir: str = DEFAULT_LOCK_DIR
) -> None:
    # Defensive, best-effort, ONCE at startup before any work: reclaim
    # a hard-crashed prior cycle's leaked worktree admin entry. Fully
    # crash-isolated — a git failure here never wedges the daemon.
    _startup_worktree_prune()
    cursor = datetime.now(UTC) - INITIAL_CURSOR_LOOKBACK
    logger.info(
        "llm_triage_service.started",
        triggers=list(TRIGGER_EVENT_TYPES),
        poll_interval_sec=POLL_INTERVAL_SEC,
        initial_cursor=cursor.isoformat(),
        lock_dir=lock_dir,
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
            # Acquire the sibling-parity self-exclusion lock so an
            # ad-hoc concurrent `python -m ops.llm_triage_service`
            # cannot run a triage pass on top of this one. Held only
            # for the duration of run_triage; released in finally.
            try:
                _acquire_lock(lock_dir)
            except LockHeldByLiveProcess as exc:
                logger.info(
                    "llm_triage_service.lock_skip", holder=str(exc)
                )
            else:
                # Advisory + crash-isolated: a triage failure is logged
                # and the loop continues — NEVER block or crash the
                # daemon. run_triage itself re-checks the open set, so a
                # same-cycle self-heal is a safe no-op (no data-ops
                # ordering coupling).
                try:
                    await run_triage(pool)
                except Exception as exc:  # noqa: BLE001 — isolate; advisory
                    logger.error(
                        "llm_triage_service.triage_failed", error=str(exc)
                    )
                finally:
                    _release_lock(lock_dir, only_if_owned=True)

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

    lock_dir = os.environ.get("STE_LLM_TRIAGE_LOCK_DIR", DEFAULT_LOCK_DIR)
    pool = await build_asyncpg_pool(dsn, max_size=POOL_MAX_SIZE)
    stop_event = asyncio.Event()

    def _handle_signal(signum):
        logger.info("llm_triage_service.signal_received", signum=signum)
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal, sig)

    try:
        await _main_loop(pool, stop_event, lock_dir)
    finally:
        # Defensive: never leave the lock held on shutdown if a triage
        # pass was interrupted mid-flight (the per-pass finally already
        # releases on the normal path). Only release if WE own it.
        _release_lock(lock_dir, only_if_owned=True)
        await pool.close()
        logger.info("llm_triage_service.stopped")
    return 0


def main() -> None:  # pragma: no cover - CLI shim
    sys.exit(asyncio.run(_amain()))


if __name__ == "__main__":  # pragma: no cover
    main()
