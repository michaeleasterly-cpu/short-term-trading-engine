"""triage-service daemon — event-driven autonomous-data + advisory-engine
+ lab-emitter lanes (LT-P3 §4 + Epic E Phase 3 / FORK B = B1 + SP-G + the
2026-05-21 autonomous-data-recovery flip).

Structural sibling of ``ops/engine_service.py`` / ``ops/data_repair_service.py``,
co-hosting THREE lanes' loops as three independent ``_run_supervised``
co-tasks on the ONE pool (SP-G added the third co-task; the daemon is
still a SINGLE daemon — the two-daemon invariant is preserved):

  * DATA lane (AUTONOMOUS — 2026-05-21 flip per operator directive
    "automate the god damn triage, no operator-task bullshit in the
    self heal"): when the in-orchestrator cascade (scripts/ops.py
    auto-cascade + the smart-feed cascade) exhausts on a data-lane
    failure and emits ``DATA_REPAIR_ESCALATED`` /
    ``DATA_SOURCE_ESCALATED`` / ``INGESTION_AUTO_RECOVERY_FAILED``,
    this fires ``ops.llm_data_recovery.run_autonomous_recovery``. The
    LLM picks ONE stage + params from a frozen whitelist
    (``_AUTONOMOUS_DATA_ACTIONS``), the deterministic validator gates
    it, the bounded subprocess runs it. NO draft PR. NO human-merge
    gate. Single-shot per cycle. Engine roster / engine-code mutations
    are NOT in scope — those stay on the engine lane's PR-gated path.
  * ENGINE lane (still PR-GATED — operator directive scopes the
    autonomous flip to the DATA lane only): when the deterministic
    engine lane (Phase-0 detection: DA-1/DA-2/engine-daemon
    platform-service crash-loop / swallowed-digest) emits an
    ``ENGINE_ESCALATED`` that the Ladder leaves open + undispositioned,
    this fires one ``ops.engine_llm_triage.run_triage`` — advisory +
    draft-PR + human-merge.
  * LAB-EMITTER lane (SP-G) — the third co-task fires on the
    ``LAB_LEDGER_CAPACITY_AVAILABLE`` event class (per operator Q6
    decision the event class is DEFERRED in v1 — the co-task is
    structurally present with an empty trigger tuple; the
    operator-command path ``/lab-spec-emit`` is the v1 trigger).

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
``ops.llm_data_recovery.run_autonomous_recovery`` (data lane —
autonomous, 2026-05-21) + ``ops.engine_llm_triage.run_triage`` (engine
lane — still advisory + PR-gated) + ``ops.llm_lab_emitter`` (SP-G) +
stdlib/asyncpg/structlog. The data-lane autonomous action surface is
NOT this daemon — it is the frozen whitelist + the deterministic
validator + the bounded subprocess in ``ops.llm_data_recovery``. The
engine-lane import is still the advisory module
(``ops.engine_llm_triage`` — never repair/trade/dispose/merge;
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
from ops.llm_data_recovery import (
    AUTONOMOUS_DATA_TRIGGER_EVENT_TYPES,
    run_autonomous_recovery,
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
# protocol). It guards an ad-hoc concurrent `python -m
# ops.llm_triage_service` invocation from overlapping the launchd
# daemon — two advisory passes racing select_novel_escalations /
# `git worktree add` would contend. Distinct lock name from the
# data-ops lock (this is the advisory lane, not the data lane).
DEFAULT_LOCK_DIR = os.path.join(
    os.environ.get("TMPDIR", "/tmp"), "ste-llm-triage-service.lock"
)
# The DATA-lane autonomous-recovery escalation set (2026-05-21 flip per
# operator directive "automate the god damn triage, no operator-task
# bullshit in the self heal"). The previous two classes
# (DATA_REPAIR_ESCALATED — bounded self-heal exhausted;
# DATA_SOURCE_ESCALATED — datasupervisor ≥3 held cycles) PLUS the
# in-orchestrator-cascade exhaustion class (INGESTION_AUTO_RECOVERY_FAILED
# — auto-cascade + smart-feed cascade gave up). All three now route
# through ``ops.llm_data_recovery.run_autonomous_recovery`` — no draft
# PR, no human-merge gate, single-shot per cycle. The set is owned by
# ``ops.llm_data_recovery``; this module's name is preserved as the
# DATA-lane trigger alias so the existing daemon tests continue to read
# the same constant.
TRIGGER_EVENT_TYPES: tuple[str, ...] = AUTONOMOUS_DATA_TRIGGER_EVENT_TYPES
# The ENGINE-lane escalation class (Epic E Phase 3). DA-1/DA-2 +
# engine-daemon platform-service detection (Phase 0) emit a single
# ``ENGINE_ESCALATED``; the engine Ladder
# (``engine_ladder.list_undispositioned``) decides which are open +
# undispositioned. The engine co-task polls this; ``engine_run_triage``
# re-checks the Ladder open set itself (no ordering coupling).
ENGINE_TRIGGER_EVENT_TYPES: tuple[str, ...] = ("ENGINE_ESCALATED",)
# poll (1 per lane) + each lane's run_triage acquires + headroom; with
# THREE co-hosted lanes (data, engine, lab_emitter — SP-G) sharing the
# one advisory pool we widen the cap once more.
POOL_MAX_SIZE = 5


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
    """DATA-lane co-task (AUTONOMOUS — 2026-05-21 flip). Defensive,
    best-effort, ONCE at startup before any work: reclaim a hard-crashed
    prior cycle's leaked worktree admin entry (fully crash-isolated).
    The data lane owns the single process-global startup prune; the
    engine lane shares the same repo and must NOT double-prune. Then
    runs the shared poll loop on the DATA escalation set —
    ``run_autonomous_recovery`` picks ONE whitelisted stage + params via
    one LLM call and runs it in a bounded credential-starved subprocess.
    No draft PR. No human-merge gate."""
    _startup_worktree_prune()
    await _lane_loop(
        pool,
        stop_event,
        lock_dir,
        event_types=TRIGGER_EVENT_TYPES,
        triage_fn=run_autonomous_recovery,
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


async def _lab_emitter_loop(
    pool, stop_event: asyncio.Event, lock_dir: str = DEFAULT_LOCK_DIR
) -> None:
    """LAB-EMITTER co-task (SP-G Phase 1; spec §4.2). The third crash-
    isolated ``_run_supervised`` co-task on the ONE advisory pool.

    Per operator Q6 decision (spec §10): the
    ``LAB_LEDGER_CAPACITY_AVAILABLE`` event class is DEFERRED in this
    PR. The co-task is structurally present (mirrors data + engine
    lanes for symmetry), but its trigger event-type set
    (``LAB_EMITTER_TRIGGER_EVENT_TYPES``) is empty by design — the
    poll runs every ``POLL_INTERVAL_SEC`` and sees nothing to do; the
    operator-command path (the ``/lab-spec-emit`` skill calling
    ``python -m ops.llm_lab_emitter``) is the v1 trigger.

    When the operator decides to populate
    ``LAB_EMITTER_TRIGGER_EVENT_TYPES`` (task #25 / a future
    event-emitter PR), this co-task starts firing
    ``run_lab_emitter_cotask`` on the trigger — zero code change here.
    The two-daemon invariant test
    (``scripts/tests/test_two_daemon_invariant.py``) MUST stay green
    UNEDITED — SP-G adds a co-task, not a daemon (the installer
    whitelist + launchd label are unchanged).
    """
    await _lane_loop(
        pool,
        stop_event,
        lock_dir,
        event_types=LAB_EMITTER_TRIGGER_EVENT_TYPES,
        triage_fn=run_lab_emitter_cotask,
        lane="lab_emitter",
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

    # FORK B = B1: THREE independent _run_supervised co-tasks on the
    # ONE advisory pool — the DATA lane, the ENGINE lane, and (SP-G)
    # the LAB-EMITTER lane. Each is crash-isolated from the others (a
    # crash in one is logged + restarted by its own _run_supervised and
    # never propagates to a sibling or the daemon). All share this
    # ``pool`` and the single ``lock_dir`` self-exclusion lock. The
    # two-daemon invariant test
    # (``scripts/tests/test_two_daemon_invariant.py``) MUST stay green
    # UNEDITED — SP-G adds a co-task, not a daemon (the installer
    # whitelist + launchd label are unchanged).
    async def _data_factory():
        await _main_loop(pool, stop_event, lock_dir)

    async def _engine_factory():
        await _engine_loop(pool, stop_event, lock_dir)

    async def _lab_emitter_factory():
        await _lab_emitter_loop(pool, stop_event, lock_dir)

    data_task = asyncio.create_task(
        _run_supervised("data", _data_factory, stop_event))
    engine_task = asyncio.create_task(
        _run_supervised("engine", _engine_factory, stop_event))
    lab_emitter_task = asyncio.create_task(
        _run_supervised("lab_emitter", _lab_emitter_factory, stop_event))
    try:
        # Exit on signal (stop_event) OR if all lanes have exited
        # (nothing left to supervise — don't zombie the process).
        stop_waiter = asyncio.ensure_future(stop_event.wait())
        all_done = asyncio.gather(data_task, engine_task, lab_emitter_task)
        done, _pending = await asyncio.wait(
            {stop_waiter, all_done},
            return_when=asyncio.FIRST_COMPLETED)
        stop_waiter.cancel()
        all_done.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stop_waiter
        with contextlib.suppress(BaseException):
            await all_done
    finally:
        for t in (data_task, engine_task, lab_emitter_task):
            t.cancel()
        await asyncio.gather(data_task, engine_task, lab_emitter_task,
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
