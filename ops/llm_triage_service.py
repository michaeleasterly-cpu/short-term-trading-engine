"""triage-service daemon — event-driven advisory triage (data + engine
lanes; LT-P3 §4 + Epic E Phase 3 / FORK B = B1).

Structural sibling of ``ops/engine_service.py`` / ``ops/data_repair_service.py``,
serving the *advisory* path. It co-hosts BOTH lanes' triage loops as
two independent ``_run_supervised`` co-tasks on the ONE advisory pool:

  * DATA lane — when the data lane gives up on a data problem and emits
    a ``DATA_REPAIR_ESCALATED`` (bounded self-heal exhausted) or a
    ``DATA_SOURCE_ESCALATED`` (a source stuck ≥3 held cycles by the
    datasupervisor), this fires one ``ops.llm_data_triage.run_triage``.
  * ENGINE lane — when the deterministic engine lane (Phase-0
    detection: DA-1/DA-2/engine-daemon platform-service crash-loop /
    swallowed-digest) emits an ``ENGINE_ESCALATED`` that the Ladder
    leaves open + undispositioned, this fires one
    ``ops.engine_llm_triage.run_triage``.

Why B1 (Epic E spec §8): the advisory daemon ALREADY exists, is
ALREADY in the closed 4-token installer whitelist, and is ALREADY
process-isolated from the live-trading ``engine_service``. A slow/hung
LLM call or a ``git worktree``/``gh`` subprocess must never share the
event loop / asyncpg pool / signal-handler set as the live trade-submit
sweep — so the engine triage is a SECOND co-task HERE, NOT inside
``engine_service`` and NOT a 5th daemon. The installer name, launchd
label, and 4-token whitelist are UNCHANGED — ``test_two_daemon_
invariant.py`` requires zero edits (B1 placement proof). Both loops are
crash-isolated from each other (independent ``_run_supervised``): one
crashing lane never kills the other lane or the daemon.

Why event-driven (v2.1): triage is human-review fuel — it must follow
the escalation that produced it, not a cron tick or a linear step.
There is NO data-ops ordering coupling: each lane's ``run_triage``
re-checks its own open set, so a same-cycle deterministic self-heal /
auto-clear that already resolved the escalation makes triage a safe
no-op. The daemon never blocks anything and is fully crash-isolated.

Safety boundary: this daemon imports ONLY
``ops.llm_data_triage.run_triage`` + ``ops.engine_llm_triage.run_triage``
+ stdlib/asyncpg/structlog — NO actor/mutation path (asserted by the
import-isolation AST test; both triage modules are themselves
no-mutation advisory modules — never repair/trade/dispose/merge;
restoration only ever happens via the deterministic path).

Idempotence: each lane tracks the latest ``recorded_at`` seen and only
fires on strictly-newer events (mirrors engine_service). On first start
each cursor initializes to ``now() - 1h`` so a restart doesn't replay
old escalations. KeepAlive=true at the launchd layer restarts the
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

from ops.engine_llm_triage import run_triage as engine_run_triage
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
# The two DATA-lane escalation classes: the deterministic lane gave up
# (DATA_REPAIR_ESCALATED — bounded self-heal exhausted) or a source is
# stuck held (DATA_SOURCE_ESCALATED — datasupervisor ≥3 held cycles).
TRIGGER_EVENT_TYPES: tuple[str, ...] = (
    "DATA_REPAIR_ESCALATED",
    "DATA_SOURCE_ESCALATED",
)
# The ENGINE-lane escalation class (Epic E Phase 3). DA-1/DA-2 +
# engine-daemon platform-service detection (Phase 0) emit a single
# ``ENGINE_ESCALATED``; the engine Ladder
# (``engine_ladder.list_undispositioned``) decides which are open +
# undispositioned. The engine co-task polls this; ``engine_run_triage``
# re-checks the Ladder open set itself (no ordering coupling).
ENGINE_TRIGGER_EVENT_TYPES: tuple[str, ...] = ("ENGINE_ESCALATED",)
# poll (1 per lane) + each lane's run_triage acquires + headroom; with
# two co-hosted lanes sharing the one advisory pool we widen the cap.
POOL_MAX_SIZE = 4


async def _find_new_trigger(
    pool,
    cursor: datetime,
    event_types: tuple[str, ...] = TRIGGER_EVENT_TYPES,
) -> datetime | None:
    """Return the recorded_at of the newest trigger event > cursor.

    Mirrors ``engine_service._find_new_trigger`` exactly: filters
    ``event_type = ANY($1) AND recorded_at > cursor`` and returns the
    newest ``recorded_at`` (or ``None`` if none). ``event_types``
    defaults to the DATA-lane set (so the existing 2-arg callers /
    tests are byte-unchanged); the engine co-task passes
    ``ENGINE_TRIGGER_EVENT_TYPES``. ONE poll idiom, lane-agnostic by
    parameter — not re-authored per lane.
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


async def _lane_loop(
    pool,
    stop_event: asyncio.Event,
    lock_dir: str,
    *,
    event_types: tuple[str, ...],
    triage_fn,
    lane: str,
) -> None:
    """ONE cursor-poll triage loop, lane-agnostic by parameter (the
    DATA + ENGINE co-tasks both delegate here — the idiom is reused
    verbatim, never re-authored per lane).

    Cursor-polls ``platform.application_log`` for ``event_types`` >
    cursor; on a strictly-newer trigger acquires the SHARED mkdir-atomic
    self-exclusion lock (so a data pass and an engine pass — or an
    ad-hoc concurrent ``python -m ops.llm_triage_service`` — can never
    race ``git worktree add``) and fires ONE ``triage_fn(pool)``.
    Advisory + crash-isolated: a triage failure is logged and the loop
    continues. ``triage_fn`` re-checks its own open set, so a same-cycle
    deterministic resolution makes the pass a safe no-op.
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
            # Acquire the SHARED sibling-parity self-exclusion lock so a
            # concurrent pass (the OTHER lane's co-task, or an ad-hoc
            # `python -m ops.llm_triage_service`) cannot run a triage
            # pass on top of this one. Held only for the duration of
            # triage_fn; released in finally.
            try:
                _acquire_lock(lock_dir)
            except LockHeldByLiveProcess as exc:
                logger.info(
                    "llm_triage_service.lock_skip",
                    lane=lane,
                    holder=str(exc),
                )
            else:
                # Advisory + crash-isolated: a triage failure is logged
                # and the loop continues — NEVER block or crash the
                # daemon. triage_fn itself re-checks the open set, so a
                # same-cycle self-heal / auto-clear is a safe no-op (no
                # data-ops / Ladder ordering coupling).
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


async def _main_loop(
    pool, stop_event: asyncio.Event, lock_dir: str = DEFAULT_LOCK_DIR
) -> None:
    """DATA-lane co-task. Defensive, best-effort, ONCE at startup before
    any work: reclaim a hard-crashed prior cycle's leaked worktree admin
    entry (fully crash-isolated). The data lane owns the single
    process-global startup prune; the engine lane shares the same repo
    and must NOT double-prune. Then runs the shared poll loop on the
    DATA escalation set."""
    _startup_worktree_prune()
    await _lane_loop(
        pool,
        stop_event,
        lock_dir,
        event_types=TRIGGER_EVENT_TYPES,
        triage_fn=run_triage,
        lane="data",
    )


async def _engine_loop(
    pool, stop_event: asyncio.Event, lock_dir: str = DEFAULT_LOCK_DIR
) -> None:
    """ENGINE-lane co-task (Epic E Phase 3 / FORK B = B1). Independent
    of the data lane (separate ``_run_supervised`` wrapper → crash-
    isolated). Reuses the SHARED lock + the SAME poll idiom verbatim;
    does NOT re-run the startup prune (the data lane already did the
    one process-global prune). Polls ``ENGINE_ESCALATED`` and fires
    ``engine_run_triage`` (the Phase-2 engine agent — itself a
    no-mutation advisory module)."""
    await _lane_loop(
        pool,
        stop_event,
        lock_dir,
        event_types=ENGINE_TRIGGER_EVENT_TYPES,
        triage_fn=engine_run_triage,
        lane="engine",
    )


async def _run_supervised(
    name: str, factory, stop_event: asyncio.Event, backoff: float = 5.0
) -> None:
    """Run ``factory()`` (a 0-arg coroutine fn) until stop_event; an
    Exception is logged and the lane restarted after ``backoff`` —
    mirrors the crash-isolation CONTRACT of ``engine_service._run_supervised``
    (CancelledError propagates; non-Cancelled Exception is logged +
    backoff-restarted, never propagated), but deliberately OMITS the
    engine-lane crash-loop Ladder escalation (advisory daemon must not
    feed the engine Ladder). CancelledError propagates (clean shutdown)."""
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

    # FORK B = B1: TWO independent _run_supervised co-tasks on the ONE
    # advisory pool — the DATA lane and the ENGINE lane. Each is crash-
    # isolated from the other (a crash in one is logged + restarted by
    # its own _run_supervised and never propagates to the sibling or the
    # daemon). Both share this ``pool`` and the single ``lock_dir``
    # self-exclusion lock. Mirrors engine_service._amain's two-co-task
    # gather/shutdown shape verbatim.
    async def _data_factory():
        await _main_loop(pool, stop_event, lock_dir)

    async def _engine_factory():
        await _engine_loop(pool, stop_event, lock_dir)

    data_task = asyncio.create_task(
        _run_supervised("data", _data_factory, stop_event))
    engine_task = asyncio.create_task(
        _run_supervised("engine", _engine_factory, stop_event))
    try:
        # Exit on signal (stop_event) OR if both lanes have exited
        # (nothing left to supervise — don't zombie the process).
        stop_waiter = asyncio.ensure_future(stop_event.wait())
        both_done = asyncio.gather(data_task, engine_task)
        done, _pending = await asyncio.wait(
            {stop_waiter, both_done},
            return_when=asyncio.FIRST_COMPLETED)
        stop_waiter.cancel()
        both_done.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stop_waiter
        with contextlib.suppress(BaseException):
            await both_done
    finally:
        for t in (data_task, engine_task):
            t.cancel()
        await asyncio.gather(data_task, engine_task,
                             return_exceptions=True)
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
