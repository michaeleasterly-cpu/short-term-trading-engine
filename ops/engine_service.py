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

Consolidated topology (DA-3): one engine daemon co-hosts (a) the
``DATA_OPERATIONS_COMPLETE`` / green-``DATA_REPAIR_COMPLETE`` sweep
poll-loop, (b) the ``TradeMonitor.run_forever()`` stream (Tier-2 OCO
cascade), and (c) a deterministic UTC-day-rollover ``python -m
ops.weekly_digest emit`` subprocess trigger. The two long-lived tasks
run under a per-task supervisor that restarts a crashed task without
killing its sibling (defense-in-depth atop launchd ``KeepAlive``); a
single shared asyncpg pool backs all of them, with clean
signal-driven shutdown.
"""
from __future__ import annotations

import asyncio
import collections
import contextlib
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from datetime import UTC, date, datetime, timedelta

import structlog

from ops.engine_supervisor import _emit_escalated
from ops.weekly_digest import DIGEST_EVENT, _iso_week
from tpcore.aar.writer import AARWriter
from tpcore.alpaca import AlpacaPaperBrokerAdapter
from tpcore.calendar import is_trading_day
from tpcore.db import build_asyncpg_pool
from tpcore.trade_monitor import TradeMonitor

logger = structlog.get_logger(__name__)

POLL_INTERVAL_SEC = 60
INITIAL_CURSOR_LOOKBACK = timedelta(hours=1)
TRIGGER_EVENT_TYPES: tuple[str, ...] = ("DATA_OPERATIONS_COMPLETE", "DATA_REPAIR_COMPLETE")
SWEEP_SCRIPT = "scripts/run_all_engines.sh"
POOL_MAX_SIZE = 6  # sweep-poll (1) + co-hosted monitor (~4) + headroom (H-8)

# Engine-service daemon observability (2026-05-21 fix). The daemon
# previously emitted ONLY to structlog/stderr → file
# (~/Library/Logs/short-term-trading-engine/engine-service.log), so its
# heartbeat / triggers / sweep results were INVISIBLE to any
# platform.application_log query. The dormant-engines discovery
# (docs/passovers/2026-05-21-paper-trading-dormant.md) surfaced this as
# the §3 "Daemon observability gap" — operator can't tell whether the
# daemon is healthy-and-waiting (correct safety behavior when data_ops
# is red) or stuck/crashed. We now ALSO emit a small set of structured
# events to application_log under engine='engine_service' so the
# canonical event substrate carries the daemon's liveness story.
# Emits are crash-isolated (a failed write must never break the loop)
# and use json.dumps(default=str)/::jsonb in mirror of engine_supervisor
# ._emit (same INSERT_SQL shape; no new mechanism). The engine label is
# the DAEMON name 'engine_service' — DISTINCT from per-engine STARTUP
# rows (engine ∈ {reversion, vector, ...}) DA-1 already consumes; the
# new rows do not feed any DA-1 detector.
DAEMON_ENGINE_LABEL = "engine_service"
DAEMON_STARTUP_EVENT = "ENGINE_SERVICE_STARTED"
DAEMON_SHUTDOWN_EVENT = "ENGINE_SERVICE_STOPPED"
DAEMON_TRIGGER_EVENT = "ENGINE_SERVICE_TRIGGER_SEEN"
DAEMON_SWEEP_START_EVENT = "ENGINE_SERVICE_SWEEP_START"
DAEMON_SWEEP_DONE_EVENT = "ENGINE_SERVICE_SWEEP_DONE"
DAEMON_POLL_FAILED_EVENT = "ENGINE_SERVICE_POLL_FAILED"

_ENGINE_SERVICE_INSERT_SQL = """
    INSERT INTO platform.application_log
        (engine, run_id, event_type, severity, message, data)
    VALUES ($1, $2, $3, $4, $5, $6::jsonb)
"""


async def _emit_engine_service_event(pool, run_id: uuid.UUID,
                                     event_type: str, severity: str,
                                     message: str,
                                     data: dict | None = None) -> None:
    """Emit one application_log row under engine='engine_service'.

    Crash-isolated: a write failure logs to structlog and is swallowed
    — daemon-lifecycle observability is best-effort and must NEVER
    abort the supervisor loop, mirroring the
    tpcore.logging.db_handler.DBLogHandler.log contract. ``run_id`` is
    the stable per-daemon-process UUID assigned in ``_amain`` so every
    row in one daemon lifetime shares one row family (the same
    convention as the per-engine schedulers' DBLogHandler run_id)."""
    if pool is None:
        return
    payload = json.dumps(data, default=str) if data is not None else None
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                _ENGINE_SERVICE_INSERT_SQL,
                DAEMON_ENGINE_LABEL, run_id, event_type, severity,
                message, payload,
            )
    except Exception as exc:  # noqa: BLE001 — observability is best-effort
        logger.warning("engine_service.observability_emit_failed",
                       event_type=event_type, error=str(exc))

# Epic E Phase-0: engine-daemon co-hosted platform-service failures
# escalate (advisory) into the engine Ladder via ENGINE_ESCALATED. Two
# frozen classes (engine_supervisor.PLATFORM_SERVICE_FAILURE_CLASSES);
# escalate-only (NO ENGINE_HELD). Crash-loop budget: 3 crashes in a
# rolling 600s window, per co-task.
_CRASHLOOP_CLASS = "engine_service_task_crashloop"
_DIGEST_FAILED_CLASS = "engine_service_digest_failed"
_CRASHLOOP_WINDOW_SEC = 600.0
_CRASHLOOP_BUDGET = 3

# #243 Phase 1: deterministic silent-absence detector classes (daemon
# alive but a co-hosted service is silently not doing its job). Both
# escalate-only (NO ENGINE_HELD), mirroring the Phase-0 emit pattern.
_SWEEP_SILENT_CLASS = "engine_service_sweep_silent"
_DIGEST_STALLED_CLASS = "engine_service_digest_stalled"

# A qualifying trigger older than this with no sweep ⇒ silent dispatch.
# Must exceed the longest legitimate sweep so an in-flight long sweep is
# never flagged — the synchronous-sweep + cursor-only-advances-after-
# return invariant already guarantees a slow sweep advances the cursor
# (so _find_new_trigger returns None) before this bound elapses.
SWEEP_SILENT_SEC: int = 2 * POLL_INTERVAL_SEC + 300  # 420s

# The weekly digest is due at the ISO-week's Monday-00:00 UTC rollover;
# if it has not completed > this many seconds past that rollover on a
# trading day, the trigger never advanced (distinct from a rc≠0 failed
# digest). ~6h: comfortably past the day-rollover kick + any startup
# catch-up, well inside a trading day so it always surfaces same-week.
DIGEST_STALE_SEC: int = 6 * 60 * 60  # 21600s (~6h)


def _engsvc_hold_id(failure_class: str, task_name: str) -> str:
    """Deterministic (NOT uuid4) hold_id so a re-escalation of the same
    (class, task) is stable across restarts — ``engsvc-<sha256[:16]>``."""
    return "engsvc-" + hashlib.sha256(
        f"{failure_class}|{task_name}".encode()).hexdigest()[:16]


async def _safe_emit_escalated(pool, *, engine: str, hold_id: str,
                               failure_class: str, reason: str,
                               attempts: int) -> None:
    """Wrap ``engine_supervisor._emit_escalated`` so an emit failure
    (DB down etc.) can NEVER break the 'one crashed co-task must never
    kill its sibling' invariant — escalation is advisory."""
    try:
        await _emit_escalated(pool, engine, hold_id, failure_class,
                              reason, attempts)
    except Exception as exc:  # noqa: BLE001 — advisory; never abort the daemon
        logger.error("engine_service.escalate_emit_failed",
                     failure_class=failure_class, engine=engine,
                     error=str(exc))


async def _find_new_trigger(pool, cursor: datetime) -> datetime | None:
    """Return the recorded_at of the newest trigger event > cursor.

    Triggers on either ``DATA_OPERATIONS_COMPLETE`` (nightly data-ops
    finished) or ``DATA_REPAIR_COMPLETE`` (the data lane healed an
    engine's blocked data — re-run the sweep so the now-unblocked
    engine doesn't miss its window). A ``DATA_REPAIR_COMPLETE`` only
    counts when it is *green* (``data->>'green'`` true): a red repair
    didn't unblock anything, so re-firing would be a no-op sweep.

    Returns None if no new qualifying event since ``cursor``.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT recorded_at
            FROM platform.application_log
            WHERE event_type = ANY($1::text[])
              AND recorded_at > $2
              AND (event_type <> 'DATA_REPAIR_COMPLETE'
                   OR (data->>'green')::bool IS TRUE)
            ORDER BY recorded_at DESC
            LIMIT 1
            """,
            list(TRIGGER_EVENT_TYPES),
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


async def _run_sweep_with_observability(pool, run_id: uuid.UUID,
                                        trigger_at: datetime) -> int:
    """Run the engine sweep with a SWEEP_START/SWEEP_DONE application_log
    pair so the daemon's dispatch is durably observable. The sweep itself
    runs in an executor (sync subprocess) — UNCHANGED from the original
    ``await asyncio.get_event_loop().run_in_executor(None,
    _run_engine_sweep)`` call shape — the wrapper only adds emit rows
    around it. Both emits are crash-isolated by
    ``_emit_engine_service_event``."""
    started = datetime.now(UTC)
    await _emit_engine_service_event(
        pool, run_id, DAEMON_SWEEP_START_EVENT, "INFO",
        f"engine sweep starting (trigger recorded_at={trigger_at.isoformat()})",
        {"trigger_recorded_at": trigger_at.isoformat(),
         "sweep_script": SWEEP_SCRIPT})
    rc = await asyncio.get_event_loop().run_in_executor(
        None, _run_engine_sweep)
    duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
    severity = "INFO" if rc == 0 else "ERROR"
    await _emit_engine_service_event(
        pool, run_id, DAEMON_SWEEP_DONE_EVENT, severity,
        f"engine sweep finished (returncode={rc}, duration_ms={duration_ms})",
        {"returncode": rc, "duration_ms": duration_ms,
         "trigger_recorded_at": trigger_at.isoformat()})
    return rc


async def _maybe_fire_weekly_digest(state: dict, pool=None,
                                    today: date | None = None) -> None:
    """Deterministic day-rollover trigger for the (idempotent-per-ISO-week)
    weekly digest — relocated from the retired launchd cron. Fires
    ``python -m ops.weekly_digest emit`` as a crash-isolated subprocess
    (the Sub-project-C ``_invoke_allocator`` seam). NEVER raises.

    Epic E Phase-0: a swallowed digest failure (spawn exception OR
    non-zero rc) now escalates (advisory, escalate-only) into the engine
    Ladder via ``ENGINE_ESCALATED`` — the digest is the
    state-comprehension floor, so a silent failure must surface. A
    clean ``rc==0`` emits nothing. ``pool`` may be None (no escalation
    if so — keeps the historical call-shape working)."""
    today = today or datetime.now(UTC).date()
    if state.get("last") == today:
        return
    state["last"] = today
    digest_engine = "engine_service:weekly_digest"
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "ops.weekly_digest", "emit",
        )
        rc = await proc.wait()
    except Exception as exc:  # noqa: BLE001 — isolate: never abort the daemon
        logger.error("engine_service.weekly_digest_failed", error=str(exc))
        if pool is not None:
            await _safe_emit_escalated(
                pool, engine=digest_engine,
                hold_id=_engsvc_hold_id(_DIGEST_FAILED_CLASS,
                                        "weekly_digest"),
                failure_class=_DIGEST_FAILED_CLASS,
                reason=f"weekly_digest spawn failed: {exc}", attempts=1)
        return
    if rc == 0:
        logger.info("engine_service.weekly_digest_done")
    else:
        logger.error("engine_service.weekly_digest_failed", returncode=rc)
        if pool is not None:
            await _safe_emit_escalated(
                pool, engine=digest_engine,
                hold_id=_engsvc_hold_id(_DIGEST_FAILED_CLASS,
                                        "weekly_digest"),
                failure_class=_DIGEST_FAILED_CLASS,
                reason=f"weekly_digest subprocess rc={rc}", attempts=1)


async def _run_supervised(name: str, factory, stop_event: asyncio.Event,
                          pool=None, backoff: float = 5.0,
                          _monotonic=None) -> None:
    """Run ``factory()`` (a 0-arg coroutine fn) until stop_event; an
    Exception is logged and the task restarted after ``backoff`` (one
    crashed co-task must NEVER kill its sibling — H-6). CancelledError
    propagates (clean shutdown).

    Epic E Phase-0: a co-task that crash-loops past the budget (>=3
    crashes within a rolling 600s window) escalates ONCE (advisory,
    escalate-only) into the engine Ladder via ``ENGINE_ESCALATED`` — the
    log+backoff+restart behavior is UNCHANGED (it keeps restarting; the
    escalation is purely a surface). ``escalated`` latches so we emit
    exactly once per crossing; it resets to False when the rolling
    window empties, so a recovered task that later re-loops re-escalates.
    ``pool`` may be None (no escalation if so). ``_monotonic`` defaults
    to ``time.monotonic``; injected in tests for deterministic control."""
    _mono = _monotonic if _monotonic is not None else time.monotonic
    crashes: collections.deque[float] = collections.deque()
    escalated = False
    while not stop_event.is_set():
        try:
            await factory()
            return  # clean completion
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — restart, don't propagate
            logger.error("engine_service.task_crashed", task=name,
                         error=str(exc))
            now_mono = _mono()
            # Drop stale entries FIRST (relative to this crash) so the
            # deque can genuinely empty after a quiet period; THEN record
            # this crash. The latch resets the moment the rolling window
            # is empty — a recovered task that later re-loops re-escalates.
            while crashes and (
                    now_mono - crashes[0]) > _CRASHLOOP_WINDOW_SEC:
                crashes.popleft()
            if not crashes:
                escalated = False
            crashes.append(now_mono)
            if len(crashes) >= _CRASHLOOP_BUDGET and not escalated:
                escalated = True
                if pool is not None:
                    await _safe_emit_escalated(
                        pool, engine=f"engine_service:{name}",
                        hold_id=_engsvc_hold_id(_CRASHLOOP_CLASS, name),
                        failure_class=_CRASHLOOP_CLASS,
                        reason=(f"co-task {name!r} crash-looped: "
                                f">={_CRASHLOOP_BUDGET} crashes within "
                                f"{int(_CRASHLOOP_WINDOW_SEC)}s "
                                f"(last: {exc})"),
                        attempts=len(crashes))
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=backoff)
            except TimeoutError:
                pass


async def _maybe_escalate_sweep_silent(
        pool, newest: datetime | None, now: datetime,
        emitted: set[datetime]) -> None:
    """#243 Phase 1 (a): escalate (advisory, escalate-only) when a
    qualifying trigger landed but no sweep ran for it within
    ``SWEEP_SILENT_SEC``. Deterministic; consumes the EXISTING
    ``_find_new_trigger`` result (``newest``) — the green-``DATA_REPAIR_
    COMPLETE`` SQL filter and the data calendar are NOT re-derived: a
    quiet weekend / non-trading day / red repair emits no qualifying
    trigger so ``newest`` is None and there is nothing to be late for. A
    sweep that ran advanced the cursor past the trigger, so
    ``_find_new_trigger`` returns None (no false positive). One-shot per
    trigger ``recorded_at`` via ``emitted``. NEVER raises (the
    ``_safe_emit_escalated`` wrapper isolates an emit failure)."""
    if newest is None or newest in emitted:
        return
    if (now - newest).total_seconds() < SWEEP_SILENT_SEC:
        return  # in-flight grace — sweep may still be running
    emitted.add(newest)
    await _safe_emit_escalated(
        pool, engine="engine_service:sweep",
        hold_id=_engsvc_hold_id(_SWEEP_SILENT_CLASS, "sweep"),
        failure_class=_SWEEP_SILENT_CLASS,
        reason=(f"qualifying trigger at {newest.isoformat()} produced no "
                f"sweep within {SWEEP_SILENT_SEC}s"),
        attempts=1)


async def _digest_completed_this_week(pool, iso_week: str) -> bool:
    """True iff a WEEKLY_DIGEST completion row for ``iso_week`` exists —
    the SAME (event_type, data->>'iso_week') marker ops.weekly_digest
    writes on a successful ``emit`` (the format is reused, NOT
    re-derived: ``_iso_week`` is imported from that module)."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT 1 FROM platform.application_log
            WHERE event_type = $1 AND data->>'iso_week' = $2
            LIMIT 1
            """,
            DIGEST_EVENT, iso_week,
        )
    return row is not None


async def _maybe_escalate_digest_stalled(
        pool, now: datetime, emitted: set[str]) -> None:
    """#243 Phase 1 (c): escalate (advisory, escalate-only) when the
    weekly digest was never reached / never advanced this ISO-week —
    DISTINCT from the shipped ``engine_service_digest_failed`` (rc≠0,
    still emitted by ``_maybe_fire_weekly_digest`` unchanged). FIRE iff:
    ``is_trading_day(now)`` (the anti-false-positive guard — no digest is
    due on a weekend/holiday; the ONLY calendar input, not a data-lane
    re-derivation) AND this ISO-week's Monday-00:00-UTC rollover passed
    by > ``DIGEST_STALE_SEC`` AND no WEEKLY_DIGEST completion row exists
    for the current ISO-week. One-shot per ISO-week (``emitted`` — the
    loop-local dedup set the caller owns, matching
    ``_maybe_escalate_sweep_silent``). NEVER raises."""
    if not is_trading_day(now):
        return
    iso_week = _iso_week(now)
    if iso_week in emitted:
        return
    # ISO-week rollover = this week's Monday 00:00 UTC.
    week_start = (now - timedelta(days=now.isoweekday() - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    if (now - week_start).total_seconds() <= DIGEST_STALE_SEC:
        return  # within grace — the day-rollover kick may still run
    if await _digest_completed_this_week(pool, iso_week):
        return
    emitted.add(iso_week)
    await _safe_emit_escalated(
        pool, engine="engine_service:weekly_digest",
        hold_id=_engsvc_hold_id(_DIGEST_STALLED_CLASS, "weekly_digest"),
        failure_class=_DIGEST_STALLED_CLASS,
        reason=(f"weekly digest {iso_week} never advanced: "
                f">{DIGEST_STALE_SEC}s past the ISO-week rollover on a "
                f"trading day with no completion marker"),
        attempts=1)


async def _main_loop(pool, stop_event: asyncio.Event,
                     run_id: uuid.UUID | None = None) -> None:
    cursor = datetime.now(UTC) - INITIAL_CURSOR_LOOKBACK
    digest_state: dict = {"last": None}
    sweep_silent_emitted: set[datetime] = set()
    digest_stalled_emitted: set[str] = set()
    # Stable per-daemon-process run_id so all rows in this lifetime share
    # one row family (mirrors the per-engine DBLogHandler convention).
    # The ``_amain`` caller passes the same UUID it used for the STARTUP
    # row; tests that drive _main_loop directly may omit it and the
    # observability emits become structlog-only no-ops (pool gate).
    if run_id is None:
        run_id = uuid.uuid4()
    # Poll-failure observability: emit AT MOST once per failure burst —
    # a transient DB blip can produce many consecutive failed polls and
    # we don't want to flood application_log. Reset on the first
    # successful poll. (Structured logging mirrors the existing
    # logger.error breadcrumb; this only adds the DB-visible row.)
    poll_failed_emitted = False
    logger.info(
        "engine_service.started",
        triggers=list(TRIGGER_EVENT_TYPES),
        poll_interval_sec=POLL_INTERVAL_SEC,
        initial_cursor=cursor.isoformat(),
    )
    await _maybe_fire_weekly_digest(digest_state, pool=pool)  # startup kick (O-2)

    while not stop_event.is_set():
        try:
            newest = await _find_new_trigger(pool, cursor)
        except Exception as exc:
            logger.error("engine_service.poll_failed", error=str(exc))
            newest = None
            if not poll_failed_emitted:
                poll_failed_emitted = True
                await _emit_engine_service_event(
                    pool, run_id, DAEMON_POLL_FAILED_EVENT, "ERROR",
                    f"engine_service trigger poll failed: {exc}",
                    {"error": str(exc),
                     "exception_type": type(exc).__name__})
        else:
            # A clean poll closes the burst — next failure re-emits.
            poll_failed_emitted = False

        # #243 Phase 1 (a): a qualifying trigger that produced no sweep
        # within the bound is a silent dispatch defect — escalate-only,
        # advisory; the sweep below still runs (we surface that it was
        # late, we do not suppress it). Crash-isolated.
        try:
            await _maybe_escalate_sweep_silent(
                pool, newest, datetime.now(UTC), sweep_silent_emitted)
        except Exception as exc:  # noqa: BLE001 — advisory; never abort the loop
            logger.error("engine_service.sweep_silent_check_failed",
                         error=str(exc))

        if newest is not None and newest > cursor:
            logger.info("engine_service.trigger_seen", recorded_at=newest.isoformat())
            await _emit_engine_service_event(
                pool, run_id, DAEMON_TRIGGER_EVENT, "INFO",
                f"qualifying trigger seen (recorded_at={newest.isoformat()})",
                {"trigger_recorded_at": newest.isoformat(),
                 "prev_cursor": cursor.isoformat()})
            cursor = newest
            # Run the sweep synchronously — we don't want to fire
            # overlapping sweeps if data-ops emits two events close
            # together. The next poll picks up any newer trigger.
            await _run_sweep_with_observability(pool, run_id, newest)

        await _maybe_fire_weekly_digest(digest_state, pool=pool)

        # #243 Phase 1 (c): a digest that was never reached / never
        # advanced this trading ISO-week is a silent state-comprehension
        # gap — escalate-only, advisory. Crash-isolated.
        try:
            await _maybe_escalate_digest_stalled(
                pool, datetime.now(UTC), digest_stalled_emitted)
        except Exception as exc:  # noqa: BLE001 — advisory; never abort the loop
            logger.error("engine_service.digest_stalled_check_failed",
                         error=str(exc))

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=POLL_INTERVAL_SEC)
        except TimeoutError:
            pass


async def _amain() -> int:
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_URL_IPV4")
    if not dsn:
        logger.error("engine_service.no_dsn", note="set DATABASE_URL or DATABASE_URL_IPV4")
        return 1

    pool = await build_asyncpg_pool(dsn, max_size=POOL_MAX_SIZE)
    stop_event = asyncio.Event()

    # One stable run_id per daemon-process lifetime (mirrors the
    # per-engine DBLogHandler convention) so the operator can correlate
    # a STARTED → ... → STOPPED row family in application_log for one
    # daemon incarnation. The 2026-05-21 observability fix.
    daemon_run_id = uuid.uuid4()
    daemon_started_at = datetime.now(UTC)
    await _emit_engine_service_event(
        pool, daemon_run_id, DAEMON_STARTUP_EVENT, "INFO",
        "engine_service daemon started",
        {"pid": os.getpid(),
         "poll_interval_sec": POLL_INTERVAL_SEC,
         "pool_max_size": POOL_MAX_SIZE,
         "triggers": list(TRIGGER_EVENT_TYPES),
         "sweep_script": SWEEP_SCRIPT})

    def _handle_signal(signum):
        logger.info("engine_service.signal_received", signum=signum)
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal, sig)

    # H-1: construct the monitor against the SHARED pool (mirror
    # tpcore.trade_monitor.amain()'s construction block — NOT amain()).
    monitor = TradeMonitor(
        pool=pool, broker=AlpacaPaperBrokerAdapter(),
        aar_writer=AARWriter(pool))

    async def _sweep_factory():
        await _main_loop(pool, stop_event, run_id=daemon_run_id)

    async def _monitor_factory():
        await monitor.run_forever()

    sweep_task = asyncio.create_task(
        _run_supervised("sweep", _sweep_factory, stop_event, pool=pool))
    monitor_task = asyncio.create_task(
        _run_supervised("monitor", _monitor_factory, stop_event, pool=pool))
    try:
        # Exit on signal (stop_event) OR if both co-tasks have exited
        # (nothing left to supervise — don't zombie the process).
        stop_waiter = asyncio.ensure_future(stop_event.wait())
        both_done = asyncio.gather(sweep_task, monitor_task)
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
        for t in (sweep_task, monitor_task):
            t.cancel()
        await asyncio.gather(sweep_task, monitor_task,
                             return_exceptions=True)
        # Best-effort STOPPED row BEFORE pool.close() — once the pool
        # is closed, the emit helper's acquire() raises and the
        # observability row is lost. Crash-isolated either way.
        duration_ms = int(
            (datetime.now(UTC) - daemon_started_at).total_seconds() * 1000)
        await _emit_engine_service_event(
            pool, daemon_run_id, DAEMON_SHUTDOWN_EVENT, "INFO",
            f"engine_service daemon stopped (duration_ms={duration_ms})",
            {"pid": os.getpid(), "duration_ms": duration_ms})
        await pool.close()
        logger.info("engine_service.stopped")
    return 0


def main() -> None:  # pragma: no cover - CLI shim
    sys.exit(asyncio.run(_amain()))


if __name__ == "__main__":  # pragma: no cover
    main()
