"""Operator-trigger lane — polls platform.application_log for
OPERATOR_RUN_REQUESTED rows and shells out to the canonical script.

Sibling of ``ops.data_repair_service`` (the deterministic data-repair
responder). Same poll-loop shape, same crash-isolation contract via
``ops.lane_service._run_supervised``. Mounted as a second co-task on
the deployed ``lane_service`` daemon under the
2026-05-29 ``build_real_data_pipeline_operations_console`` task.

Architecture invariant alignment:

  * ``daemons.md`` rule: data-lane is ``lane_service`` (deployed
    deterministic) + ``data_operations`` (cron). The operator-trigger
    lane is deterministic: it dispatches the EXACT same script the
    cron runs (``scripts/run_data_operations.sh``) or a single
    canonical stage (``python -m scripts.ops --stage <name>``). NO
    LLM. NO autonomous fallback. NO new daemon.
  * ``application_log`` is the event bus.
  * Concurrency: ``scripts/run_data_operations.sh`` acquires
    ``pg_try_advisory_lock(hashtext('data_ops_run'))`` at entry so
    cron + operator-trigger arbitrate via the same Postgres mutex.

Event lifecycle (durable in application_log, same run_id throughout):

    OPERATOR_RUN_REQUESTED       written by console-api on click
    OPERATOR_RUN_STARTED         written here when subprocess spawns
    OPERATOR_RUN_COMPLETED       written here on exit_code == 0
    OPERATOR_RUN_FAILED          written here on exit_code != 0
    OPERATOR_RUN_ABORTED         written by console-api; SIGTERM here
    OPERATOR_RUN_REJECTED_LOCK   written here when advisory lock held
                                 by another process (cron in flight)

Idempotence: cursor-based — we advance only on rows we've terminally
resolved. A daemon restart re-scans within the cursor window; rows
that already have a COMPLETED/FAILED/ABORTED partner are skipped.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import asyncpg
import structlog

logger = structlog.get_logger(__name__)


# Cursor lookback on a fresh daemon start. 30 min is enough to pick
# up a row written just before a crash but not so wide that we replay
# old history.
INITIAL_CURSOR_LOOKBACK = timedelta(minutes=30)


# Time between polls. 5 s is responsive for an operator click (the
# UI sees the QUEUED → RUNNING transition within one poll). The
# Supabase Pro plan is fine with this cadence given a single
# lightweight SELECT per tick.
POLL_INTERVAL_SEC = 5.0


# Subprocess hard timeout. The data-ops script normally completes in
# ~25 min; this is a watchdog. After this, SIGTERM the process and
# write OPERATOR_RUN_FAILED.
SUBPROCESS_TIMEOUT_SEC = 90 * 60  # 90 minutes


# Stuck-queue watchdog. F-005 fix (2026-05-29 expert review): a
# QUEUED row that ages past this threshold without an
# OPERATOR_RUN_STARTED partner indicates the daemon was down when
# the request landed, the row got skipped by cursor advancement, or
# the upstream DB-connection sequence interrupted dispatch. In all
# cases the row would never reach a terminal state, and the UI
# poller would hit jobStatus → RUNNING forever. Write a synthetic
# OPERATOR_RUN_FAILED row so the contract "every QUEUED has a
# terminal partner" holds.
STUCK_QUEUE_WATCHDOG_MINUTES = 90


# Engine identity for the rows we write.
ENGINE_NAME = "operator_trigger_lane"


# Repo root for spawning scripts.
def _repo_root() -> Path:
    """Repo root for spawning ``scripts/run_data_operations.sh``.
    Defaults to ``${STE_REPO_ROOT}`` (set on Railway) → the package's
    grandparent (when installed editable in lane-service container)
    → cwd as last resort."""
    env = os.environ.get("STE_REPO_ROOT")
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    # ops/operator_trigger_lane.py → repo_root
    return here.parent.parent


async def _main_loop(
    pool: asyncpg.Pool,
    stop_event: asyncio.Event,
) -> None:
    """Poll loop for OPERATOR_RUN_REQUESTED events. Mirrors
    ``ops.data_repair_service._main_loop`` shape so
    ``lane_service._run_supervised`` can wrap it identically."""
    cursor = datetime.now(UTC) - INITIAL_CURSOR_LOOKBACK
    logger.info(
        "operator_trigger_lane.started",
        cursor=cursor.isoformat(),
        poll_interval_sec=POLL_INTERVAL_SEC,
    )

    while not stop_event.is_set():
        try:
            new_cursor = await _process_tick(pool, cursor)
            cursor = new_cursor
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — log + continue
            logger.error(
                "operator_trigger_lane.poll_error",
                error=str(exc),
                exc_type=type(exc).__name__,
            )
        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=POLL_INTERVAL_SEC,
            )
            return  # stop_event set during sleep — clean shutdown
        except TimeoutError:
            continue


async def _process_tick(
    pool: asyncpg.Pool, cursor: datetime,
) -> datetime:
    """Find any new OPERATOR_RUN_REQUESTED rows past cursor that don't
    yet have a terminal partner. Dispatch each in sequence (we do NOT
    run two operator triggers in parallel — the pg_advisory_lock
    would serialize them anyway, and we want one operator dispatch
    in flight at a time)."""
    # F-005 fix: before fetching new rows, sweep for stuck QUEUED
    # rows from prior poll cycles (daemon-down or DB-interrupted
    # cases) and write OPERATOR_RUN_FAILED with reason='watchdog'.
    await _watchdog_terminate_stuck(pool)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT recorded_at, run_id, message, data
            FROM platform.application_log
            WHERE event_type = 'OPERATOR_RUN_REQUESTED'
              AND recorded_at > $1
              AND NOT EXISTS (
                  SELECT 1 FROM platform.application_log t
                  WHERE t.run_id = platform.application_log.run_id
                    AND t.event_type IN (
                        'OPERATOR_RUN_STARTED',
                        'OPERATOR_RUN_COMPLETED',
                        'OPERATOR_RUN_FAILED',
                        'OPERATOR_RUN_ABORTED',
                        'OPERATOR_RUN_REJECTED_LOCK'
                    )
              )
            ORDER BY recorded_at ASC
            """,
            cursor,
        )

    if not rows:
        return cursor

    latest = cursor
    for row in rows:
        await _dispatch_request(pool, row)
        if row["recorded_at"] > latest:
            latest = row["recorded_at"]
    return latest


async def _watchdog_terminate_stuck(pool: asyncpg.Pool) -> None:
    """Write OPERATOR_RUN_FAILED for any QUEUED row older than
    STUCK_QUEUE_WATCHDOG_MINUTES that lacks a STARTED / terminal
    partner. F-005 fix — without this, stuck QUEUED rows make the UI
    poller spin forever on RUNNING."""
    cutoff = datetime.now(UTC) - timedelta(
        minutes=STUCK_QUEUE_WATCHDOG_MINUTES,
    )
    async with pool.acquire() as conn:
        stuck = await conn.fetch(
            """
            SELECT run_id, recorded_at
            FROM platform.application_log
            WHERE event_type = 'OPERATOR_RUN_REQUESTED'
              AND recorded_at < $1
              AND recorded_at > $1 - INTERVAL '7 days'
              AND NOT EXISTS (
                  SELECT 1 FROM platform.application_log t
                  WHERE t.run_id = platform.application_log.run_id
                    AND t.event_type IN (
                        'OPERATOR_RUN_STARTED',
                        'OPERATOR_RUN_COMPLETED',
                        'OPERATOR_RUN_FAILED',
                        'OPERATOR_RUN_ABORTED',
                        'OPERATOR_RUN_REJECTED_LOCK'
                    )
              )
            ORDER BY recorded_at ASC
            LIMIT 50
            """,
            cutoff,
        )
    for row in stuck:
        rid = row["run_id"]
        logger.warning(
            "operator_trigger_lane.watchdog_terminate_stuck",
            run_id=str(rid), queued_at=row["recorded_at"].isoformat(),
            stuck_minutes=STUCK_QUEUE_WATCHDOG_MINUTES,
        )
        await _emit(
            pool, rid, "OPERATOR_RUN_FAILED", "ERROR",
            "operator-trigger queued row aged past watchdog window "
            "with no STARTED partner — daemon was likely down or DB "
            "was interrupted when the request was due to dispatch",
            {
                "error": "watchdog_timeout",
                "stuck_minutes": STUCK_QUEUE_WATCHDOG_MINUTES,
                "completed_at": datetime.now(UTC).isoformat(),
            },
        )


async def _dispatch_request(
    pool: asyncpg.Pool, row: asyncpg.Record,
) -> None:
    """Run one operator trigger to completion. Writes lifecycle events
    on application_log throughout."""
    run_id: uuid.UUID = row["run_id"]
    raw_data = row["data"]
    payload: dict[str, Any]
    if isinstance(raw_data, dict):
        payload = raw_data
    elif isinstance(raw_data, str) and raw_data:
        try:
            payload = json.loads(raw_data)
        except (ValueError, TypeError):
            payload = {}
    else:
        payload = {}
    action = payload.get("action") or "run_update"
    stage = payload.get("stage")
    actor = payload.get("actor") or "unknown"
    # Scoped repair payload (REQ-002): comma-list of tickers to
    # restrict the dispatched stage to. Only honored by stages that
    # accept ``--param tickers=A,B,C`` (see CHECK_REMEDIATION
    # scope_kind classification in console-api/data_pipeline.py).
    tickers: list[str] | None = payload.get("tickers") or None
    extra_params: dict[str, Any] = dict(payload.get("params") or {})

    logger.info(
        "operator_trigger_lane.dispatch_start",
        run_id=str(run_id), action=action, stage=stage, actor=actor,
        scoped_ticker_count=len(tickers) if tickers else 0,
    )

    # Mark STARTED — this is what flips the UI from QUEUED to RUNNING.
    await _emit(
        pool, run_id, "OPERATOR_RUN_STARTED", "INFO",
        f"operator-trigger run started (action={action}, "
        f"stage={stage or 'all'}"
        f"{', scoped to ' + str(len(tickers)) + ' tickers' if tickers else ''})",
        {
            "action": action,
            "stage": stage,
            "actor": actor,
            "tickers_count": len(tickers) if tickers else 0,
            "started_at": datetime.now(UTC).isoformat(),
            "host": os.environ.get("RAILWAY_REPLICA_ID") or "local",
        },
    )

    # Run the subprocess and watch for OPERATOR_RUN_ABORTED.
    exit_code, error_msg = await _run_subprocess_with_abort_watch(
        pool, run_id, action, stage,
        tickers=tickers, extra_params=extra_params,
    )

    if exit_code == 0:
        await _emit(
            pool, run_id, "OPERATOR_RUN_COMPLETED", "INFO",
            f"operator-trigger run completed (action={action}, "
            f"stage={stage or 'all'})",
            {
                "action": action,
                "stage": stage,
                "exit_code": exit_code,
                "completed_at": datetime.now(UTC).isoformat(),
            },
        )
    else:
        await _emit(
            pool, run_id, "OPERATOR_RUN_FAILED", "ERROR",
            f"operator-trigger run failed (exit_code={exit_code}): "
            f"{error_msg or 'subprocess returned non-zero'}",
            {
                "action": action,
                "stage": stage,
                "exit_code": exit_code,
                "error": error_msg,
                "completed_at": datetime.now(UTC).isoformat(),
            },
        )


async def _run_subprocess_with_abort_watch(
    pool: asyncpg.Pool,
    run_id: uuid.UUID,
    action: str,
    stage: str | None,
    tickers: list[str] | None = None,
    extra_params: dict[str, Any] | None = None,
) -> tuple[int, str | None]:
    """Spawn the canonical script as a subprocess. Concurrently watch
    for OPERATOR_RUN_ABORTED rows for this run_id and SIGTERM the
    process when one appears. Returns (exit_code, error_msg)."""
    cmd = _build_command(
        action, stage, tickers=tickers, extra_params=extra_params,
    )
    env = {**os.environ, "STE_OPERATOR_RUN_ID": str(run_id)}

    logger.info(
        "operator_trigger_lane.subprocess_spawn",
        run_id=str(run_id), cmd=cmd, cwd=str(_repo_root()),
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=str(_repo_root()),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError as exc:
        return 127, f"command not found: {exc}"
    except OSError as exc:
        return 1, f"subprocess spawn failed: {exc}"

    # Drain stdout in the background so the buffer never fills.
    log_lines: list[str] = []

    async def _drain():
        if proc.stdout is None:
            return
        async for line in proc.stdout:  # type: ignore[union-attr]
            try:
                decoded = line.decode("utf-8", errors="replace").rstrip()
            except UnicodeDecodeError:
                decoded = "<undecodable-line>"
            log_lines.append(decoded[:500])
            if len(log_lines) > 2000:
                log_lines.pop(0)

    async def _watch_abort():
        # F-004 fix (2026-05-29 expert review): lead with an abort
        # check BEFORE the first sleep, so an operator click within
        # the first 5 s of a run dispatches SIGTERM promptly.
        # Especially important for fast actions like run_validation
        # where the subprocess may complete in well under 5 s — a
        # post-sleep check would never run.
        while True:
            if await _has_abort_request(pool, run_id):
                logger.warning(
                    "operator_trigger_lane.abort_observed",
                    run_id=str(run_id),
                )
                with contextlib.suppress(ProcessLookupError):
                    proc.terminate()
                return
            await asyncio.sleep(5.0)

    drain_task = asyncio.create_task(_drain())
    abort_task = asyncio.create_task(_watch_abort())

    try:
        exit_code = await asyncio.wait_for(
            proc.wait(), timeout=SUBPROCESS_TIMEOUT_SEC,
        )
    except TimeoutError:
        logger.error(
            "operator_trigger_lane.subprocess_timeout",
            run_id=str(run_id), timeout_sec=SUBPROCESS_TIMEOUT_SEC,
        )
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        exit_code = -1
    finally:
        abort_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await abort_task
        with contextlib.suppress(asyncio.CancelledError):
            await drain_task

    tail = "\n".join(log_lines[-20:]) if log_lines else None
    return exit_code, (tail if exit_code != 0 else None)


def _build_command(
    action: str,
    stage: str | None,
    tickers: list[str] | None = None,
    extra_params: dict[str, Any] | None = None,
) -> list[str]:
    """Build the subprocess argv. Allowlisted forms only — never an
    arbitrary shell string from the request payload.

    REQ-002 scoping (2026-05-29): when ``tickers`` is non-empty, the
    stage runs with ``--param tickers=A,B,C`` so the repair is
    constrained to those symbols. Stages that don't honor the
    ``tickers`` config key (e.g. macro_indicators) simply ignore it.
    """
    if action == "run_update":
        return ["bash", "scripts/run_data_operations.sh"]

    # All other actions dispatch a single ops.py stage.
    feed_actions = {
        "run_validation": "data_validation",
        "run_feed": stage,
        "run_scoped_feed": stage,
        "repair_failed_scope": stage,
        "run_fallback_source": stage,
        "bootstrap_baseline": stage,
    }
    if action not in feed_actions:
        raise ValueError(f"unknown action: {action!r}")
    resolved_stage = feed_actions[action]
    if not resolved_stage:
        raise ValueError(f"{action} requires a stage")

    cmd = [sys.executable, "scripts/ops.py", "--stage", resolved_stage]
    if tickers:
        cmd.extend(["--param", "tickers=" + ",".join(tickers)])
    if extra_params:
        for k, v in extra_params.items():
            if k == "tickers":
                continue  # already handled above
            cmd.extend(["--param", f"{k}={v}"])
    return cmd


async def _has_abort_request(
    pool: asyncpg.Pool, run_id: uuid.UUID,
) -> bool:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT 1 FROM platform.application_log
            WHERE run_id = $1
              AND event_type = 'OPERATOR_RUN_ABORTED'
            LIMIT 1
            """,
            run_id,
        )
    return row is not None


async def _emit(
    pool: asyncpg.Pool,
    run_id: uuid.UUID,
    event_type: str,
    severity: str,
    message: str,
    data: dict[str, Any],
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO platform.application_log (
                engine, run_id, event_type, severity, message, data
            ) VALUES ($1, $2, $3, $4, $5, $6::jsonb)
            """,
            ENGINE_NAME, run_id, event_type, severity, message,
            json.dumps(data, default=str),
        )


# ──────────────────────── CLI entrypoint (tests) ────────────────────────


async def _amain() -> int:  # pragma: no cover — CLI shim for ad-hoc runs
    dsn = os.environ.get("DATABASE_URL") or os.environ.get(
        "DATABASE_URL_IPV4"
    )
    if not dsn:
        logger.error("operator_trigger_lane.no_dsn")
        return 1
    pool = await asyncpg.create_pool(
        dsn, min_size=1, max_size=2, statement_cache_size=0,
    )
    stop = asyncio.Event()

    def _h(signum: int) -> None:
        logger.info("operator_trigger_lane.signal", signum=signum)
        stop.set()

    loop = asyncio.get_running_loop()
    for s in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(s, _h, s)

    try:
        await _main_loop(pool, stop)
    finally:
        await pool.close()
    return 0


def main() -> None:  # pragma: no cover - CLI shim
    sys.exit(asyncio.run(_amain()))
