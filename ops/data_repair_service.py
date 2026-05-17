"""Data-repair-service daemon — the DATA side of the engine/data handshake.

The engine-side daemon (``ops/engine_service.py``) reacts to
``DATA_OPERATIONS_COMPLETE``. This daemon serves the *inverse* path:
an engine that finds its required data red can emit a single
``ENGINE_DATA_REQUEST`` onto ``platform.application_log`` and block on
a terminal reply. This daemon is the deterministic responder — NO LLM,
no broker, no new framework. The bus is the same
``platform.application_log`` table; the healer is the existing
``tpcore.selfheal`` orchestrator (the one canonical bounded repair).

Event contract (LOCKED — see the module's request/response payloads):

    consume  ENGINE_DATA_REQUEST   {schema, request_id, engine, sources, reason}
    emit     DATA_REPAIR_COMPLETE  {schema, request_id, sources_healed,
                                     sources_still_red, green}
    emit     DATA_REPAIR_ESCALATED {schema, request_id, sources_unhealed,
                                     reason, attempts}

``request_id`` (engine-generated uuid4 str) is the SOLE correlation
key. ``green = set(requested sources) ⊆ set(sources_healed)``.

LIVENESS GUARANTEE (the contract's teeth): for every
``ENGINE_DATA_REQUEST`` this daemon emits EXACTLY ONE terminal event
(``DATA_REPAIR_COMPLETE`` XOR ``DATA_REPAIR_ESCALATED``) carrying the
same ``request_id`` — never zero, never two, crash-safe across daemon
restarts. The durable exactly-once guard is the terminal-event row in
``application_log`` itself: before doing any work for a request the
daemon checks whether a terminal already exists for that
``request_id`` and skips if so. There is no cursor file — the DB row
*is* the state.

Concurrency safety: this daemon serializes against
``scripts/run_data_operations.sh`` Step-4 self-heal using the SAME
``${TMPDIR:-/tmp}/ste-data-operations.lock`` directory and the same
``mkdir``-atomic / dead-pid-reclaim protocol. If the lock is held by a
live pid the request is DEFERRED (cursor not advanced past it; retried
on the next 60s tick). The two never heal concurrently — concurrent
``daily_bars`` backfills contend on the Supabase pooler.

KeepAlive=true at the launchd layer restarts the process on crash;
this loop has no internal restart, just clean exits + reconnection.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import sys
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from tpcore.db import build_asyncpg_pool
from tpcore.selfheal.orchestrator import VALIDATION_STAGE, run_self_heal
from tpcore.selfheal.registry import HEAL_SPECS
from tpcore.selfheal.runner import make_canonical_runner

logger = structlog.get_logger(__name__)

POLL_INTERVAL_SEC = 60
INITIAL_CURSOR_LOOKBACK = timedelta(hours=1)
REQUEST_EVENT_TYPE = "ENGINE_DATA_REQUEST"
COMPLETE_EVENT_TYPE = "DATA_REPAIR_COMPLETE"
ESCALATED_EVENT_TYPE = "DATA_REPAIR_ESCALATED"
TERMINAL_EVENT_TYPES = (COMPLETE_EVENT_TYPE, ESCALATED_EVENT_TYPE)
SCHEMA_VERSION = 1
DAEMON_ENGINE_TAG = "data-repair-service"

# Mirrors scripts/run_data_operations.sh's self-exclusion lock so this
# daemon and Step-4 self-heal never heal concurrently.
DEFAULT_LOCK_DIR = os.path.join(os.environ.get("TMPDIR", "/tmp"), "ste-data-operations.lock")

# Mirror of tpcore.selfheal.orchestrator._RED_SQL: latest validation.*
# rows that are stale or below confidence 1.0. Self-contained here on
# purpose — the contract forbids extending the shared orchestrator.
_RED_SQL = """
    WITH latest AS (
        SELECT source, MAX(timestamp) AS t
        FROM platform.data_quality_log
        WHERE source LIKE 'validation.%'
        GROUP BY source
    )
    SELECT q.source
    FROM platform.data_quality_log q
    JOIN latest l ON l.source = q.source AND l.t = q.timestamp
    WHERE q.stale OR (q.confidence IS NOT NULL AND q.confidence < 1.0)
    ORDER BY q.source
"""

# Same shape as tpcore/logging/db_handler.py _INSERT_SQL — data is a
# JSON string cast to jsonb; recorded_at is DB-assigned (never sent).
_INSERT_SQL = """
INSERT INTO platform.application_log
    (engine, run_id, event_type, severity, message, data)
VALUES
    ($1, $2, $3, $4, $5, $6::jsonb)
"""

_NEW_REQUESTS_SQL = """
    SELECT recorded_at, data
    FROM platform.application_log
    WHERE event_type = $1
      AND recorded_at > $2
    ORDER BY recorded_at ASC
"""

# Has a terminal already been emitted for this request_id? The durable
# exactly-once guard — true means the request already terminated.
_TERMINAL_EXISTS_SQL = """
    SELECT 1
    FROM platform.application_log
    WHERE event_type = ANY($1::text[])
      AND data->>'request_id' = $2
    LIMIT 1
"""


def _checks_for_source(source: str) -> list[str]:
    """Validation check names whose HealSpec.source == ``source``."""
    return [spec.check_name for spec in HEAL_SPECS.values() if spec.source == source]


def _sources_still_red(requested: list[str], red_checks: set[str]) -> list[str]:
    """A requested source is still red iff ANY of its checks is red.

    Source→check mapping is HEAL_SPECS (the registry); ``red_checks``
    is the bare set of currently-red validation check names.
    """
    still_red: list[str] = []
    for source in requested:
        checks = _checks_for_source(source)
        if any(c in red_checks for c in checks):
            still_red.append(source)
    return still_red


async def _red_checks(pool: Any) -> set[str]:
    """Bare validation check names currently red (suite-written)."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(_RED_SQL)
    return {r["source"].removeprefix("validation.") for r in rows}


def _parse_event_data(raw: Any) -> dict[str, Any] | None:
    """``data`` may already be a dict (jsonb decoded) or a JSON string."""
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


async def _terminal_exists(pool: Any, request_id: str) -> bool:
    """Durable exactly-once guard: has THIS request_id already
    terminated (a COMPLETE or ESCALATED row exists)?"""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            _TERMINAL_EXISTS_SQL, list(TERMINAL_EVENT_TYPES), request_id
        )
    return row is not None


async def _emit(
    pool: Any,
    event_type: str,
    message: str,
    data: dict[str, Any],
    *,
    severity: str = "INFO",
) -> None:
    """Insert one terminal/operational event. ``data`` is json.dumps'd
    to a string and cast to jsonb DB-side (db_handler convention)."""
    async with pool.acquire() as conn:
        await conn.execute(
            _INSERT_SQL,
            DAEMON_ENGINE_TAG,
            uuid.uuid4(),
            event_type,
            severity,
            message,
            json.dumps(data, default=str),
        )


async def _emit_complete(
    pool: Any, request_id: str, healed: list[str], still_red: list[str]
) -> None:
    green = len(still_red) == 0
    await _emit(
        pool,
        COMPLETE_EVENT_TYPE,
        f"data repair complete for {request_id} (green={green})",
        {
            "schema": SCHEMA_VERSION,
            "request_id": request_id,
            "sources_healed": healed,
            "sources_still_red": still_red,
            "green": green,
        },
        severity="INFO" if green else "WARNING",
    )


async def _emit_escalated(
    pool: Any, request_id: str, unhealed: list[str], reason: str, attempts: int
) -> None:
    await _emit(
        pool,
        ESCALATED_EVENT_TYPE,
        f"data repair escalated for {request_id} ({len(unhealed)} unhealed)",
        {
            "schema": SCHEMA_VERSION,
            "request_id": request_id,
            "sources_unhealed": unhealed,
            "reason": reason,
            "attempts": attempts,
        },
        severity="ERROR",
    )


# ────────────────────────────────────────────────────────────────────────
# Self-exclusion lock — identical protocol to run_data_operations.sh.
# ────────────────────────────────────────────────────────────────────────


class LockHeldByLiveProcess(Exception):
    """The data-operations lock is held by a live, different pid —
    DEFER (do not heal, do not advance the cursor past this request)."""


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
    that pid is alive raise :class:`LockHeldByLiveProcess` (DEFER);
    if dead, reclaim (rmtree) and retry the acquire once. On success
    write our pid to ``<lock_dir>/pid``."""
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
        logger.info("data_repair_service.lock_reclaim", stale_pid=holder or "?")
        shutil.rmtree(lock_dir, ignore_errors=True)
        os.mkdir(lock_dir)  # reclaim retry once; a 2nd race is a real error
    with open(os.path.join(lock_dir, "pid"), "w", encoding="utf-8") as fh:
        fh.write(str(os.getpid()))


def _owns_lock(lock_dir: str) -> bool:
    """True iff the lock's pid file names THIS process — so shutdown
    cleanup never removes a lock held by run_data_operations.sh."""
    try:
        with open(os.path.join(lock_dir, "pid"), encoding="utf-8") as fh:
            return fh.read().strip() == str(os.getpid())
    except OSError:
        return False


def _release_lock(lock_dir: str, *, only_if_owned: bool = False) -> None:
    if only_if_owned and not _owns_lock(lock_dir):
        return
    shutil.rmtree(lock_dir, ignore_errors=True)


# ────────────────────────────────────────────────────────────────────────
# Per-request handling.
# ────────────────────────────────────────────────────────────────────────


async def _handle_request(
    pool: Any, request: dict[str, Any], lock_dir: str
) -> bool:
    """Process ONE ENGINE_DATA_REQUEST under the data-operations lock.

    Returns True iff a terminal event now exists for this request
    (emitted by us or already present) — i.e. the cursor may advance
    past it. Raises :class:`LockHeldByLiveProcess` to DEFER (cursor
    must NOT advance past this request; retry next tick).
    """
    request_id = request.get("request_id")
    requested = request.get("sources")
    if not request_id:
        # No correlation key — re-processing can't help and a reply we
        # can't correlate is worse. Drop (advance past).
        logger.error(
            "data_repair_service.malformed_request_no_id", request=request
        )
        return True
    if not isinstance(requested, list):
        # request_id IS present → the engine is waiting on it. The
        # liveness guarantee requires a terminal for every well-formed
        # request_id; a 90-min engine-side timeout on a payload bug is
        # worse than a definite ESCALATED now.
        logger.error(
            "data_repair_service.malformed_sources",
            request_id=request_id,
            request=request,
        )
        if not await _terminal_exists(pool, request_id):
            await _emit_escalated(
                pool, request_id, [],
                "malformed ENGINE_DATA_REQUEST: 'sources' missing or not a list",
                0,
            )
        return True

    # Durable exactly-once guard: terminal already in the DB → skip,
    # never emit twice (crash-safe across restarts).
    if await _terminal_exists(pool, request_id):
        logger.info(
            "data_repair_service.already_terminated", request_id=request_id
        )
        return True

    # Serialize vs Step-4 self-heal. Lock held only while healing.
    # NOTE: the acquire is OUTSIDE the try/finally on purpose — if it
    # raises LockHeldByLiveProcess we must NOT enter the finally that
    # rmtree's the lock (it belongs to run_data_operations.sh).
    try:
        _acquire_lock(lock_dir)
    except LockHeldByLiveProcess:
        logger.info(
            "data_repair_service.lock_deferred", request_id=request_id
        )
        raise

    run_id = uuid.uuid4()
    try:
        runner = make_canonical_runner(str(run_id))

        # (a) Validate-first fast path: refresh data_quality_log via
        # the canonical validation stage, then read reds ourselves.
        rc = await runner(VALIDATION_STAGE, {})
        if rc != 0:
            logger.error(
                "data_repair_service.validation_stage_failed",
                request_id=request_id,
                rc=rc,
            )
            await _emit_escalated(
                pool,
                request_id,
                requested,
                f"validation stage exited {rc} — cannot assess data layer",
                0,
            )
            return True

        red = await _red_checks(pool)
        still_red = _sources_still_red(requested, red)
        if not still_red:
            logger.info(
                "data_repair_service.fast_path_green",
                request_id=request_id,
                sources=requested,
            )
            await _emit_complete(pool, request_id, requested, [])
            return True

        # (b) Bounded canonical repair (whole data layer; no
        # source-subset arg — verified). Do NOT reimplement repair.
        logger.info(
            "data_repair_service.healing",
            request_id=request_id,
            still_red=still_red,
        )
        outcome = await run_self_heal(pool, make_canonical_runner(str(run_id)))

        # (c) Re-compute reds for the REQUESTED sources only.
        red_after = await _red_checks(pool)
        still_red_after = _sources_still_red(requested, red_after)
        healed = [s for s in requested if s not in still_red_after]

        if not still_red_after:
            logger.info(
                "data_repair_service.healed_green", request_id=request_id
            )
            await _emit_complete(pool, request_id, requested, [])
            return True

        # Some requested source remains red. ESCALATED iff that red set
        # is attributable to outcome.escalated (unhealable/exhausted);
        # otherwise it's a transient partial → COMPLETE green=false.
        escalated_sources = {src for src, _ in outcome.escalated}
        escalated_reasons = {
            src: reason for src, reason in outcome.escalated
        }
        attributable = [s for s in still_red_after if s in escalated_sources]
        if attributable:
            reason = "; ".join(
                escalated_reasons[s] for s in attributable
            )
            logger.warning(
                "data_repair_service.escalating",
                request_id=request_id,
                unhealed=still_red_after,
                attempts=outcome.iterations,
            )
            await _emit_escalated(
                pool,
                request_id,
                still_red_after,
                reason,
                outcome.iterations,
            )
            return True

        logger.warning(
            "data_repair_service.partial",
            request_id=request_id,
            healed=healed,
            still_red=still_red_after,
        )
        await _emit_complete(pool, request_id, healed, still_red_after)
        return True
    finally:
        _release_lock(lock_dir)


async def _poll_new_requests(
    pool: Any, cursor: datetime
) -> list[tuple[datetime, dict[str, Any]]]:
    """All ENGINE_DATA_REQUEST rows with recorded_at > cursor, oldest
    first. Returns (recorded_at, parsed_data) pairs (every new
    request, not just the newest)."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(_NEW_REQUESTS_SQL, REQUEST_EVENT_TYPE, cursor)
    out: list[tuple[datetime, dict[str, Any]]] = []
    for row in rows:
        parsed = _parse_event_data(row["data"])
        if parsed is None:
            logger.error(
                "data_repair_service.unparseable_request",
                recorded_at=row["recorded_at"].isoformat(),
            )
            # Unparseable → still advance past it (no request_id to
            # correlate a reply); record the recorded_at so the cursor
            # can move and we don't spin on it forever.
            parsed = {}
        out.append((row["recorded_at"], parsed))
    return out


async def _process_batch(
    pool: Any, cursor: datetime, lock_dir: str
) -> datetime:
    """Process every new request in recorded_at order. Advance the
    cursor only past requests that now have a terminal event. On a
    lock-defer, stop advancing at (and including) the deferred request
    so it is retried next tick — and so we never skip a still-pending
    earlier request behind a deferred one."""
    batch = await _poll_new_requests(pool, cursor)
    new_cursor = cursor
    for recorded_at, request in batch:
        try:
            terminated = await _handle_request(pool, request, lock_dir)
        except LockHeldByLiveProcess:
            # DEFER: do not advance past this request; retry next tick.
            break
        if terminated:
            new_cursor = recorded_at
        else:  # pragma: no cover - _handle_request returns True or raises
            break
    return new_cursor


async def _main_loop(pool: Any, stop_event: asyncio.Event, lock_dir: str) -> None:
    cursor = datetime.now(UTC) - INITIAL_CURSOR_LOOKBACK
    logger.info(
        "data_repair_service.started",
        consume=REQUEST_EVENT_TYPE,
        emits=list(TERMINAL_EVENT_TYPES),
        poll_interval_sec=POLL_INTERVAL_SEC,
        initial_cursor=cursor.isoformat(),
        lock_dir=lock_dir,
    )
    while not stop_event.is_set():
        try:
            cursor = await _process_batch(pool, cursor, lock_dir)
        except Exception as exc:
            # Fail loud but stay alive — KeepAlive + next tick retries.
            logger.error("data_repair_service.poll_failed", error=str(exc))
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=POLL_INTERVAL_SEC)
        except TimeoutError:
            pass


async def _amain() -> int:
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_URL_IPV4")
    if not dsn:
        logger.error(
            "data_repair_service.no_dsn",
            note="set DATABASE_URL or DATABASE_URL_IPV4",
        )
        return 1

    lock_dir = os.environ.get("STE_DATA_OPS_LOCK_DIR", DEFAULT_LOCK_DIR)
    pool = await build_asyncpg_pool(dsn)
    stop_event = asyncio.Event()

    def _handle_signal(signum: int) -> None:
        logger.info("data_repair_service.signal_received", signum=signum)
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal, sig)

    try:
        await _main_loop(pool, stop_event, lock_dir)
    finally:
        # Defensive: never leave the shared lock held on shutdown if a
        # heal was interrupted mid-flight (the per-request finally
        # already releases on the normal path). Only release if WE own
        # it — never rmtree a lock held by run_data_operations.sh.
        _release_lock(lock_dir, only_if_owned=True)
        await pool.close()
        logger.info("data_repair_service.stopped")
    return 0


def main() -> None:  # pragma: no cover - CLI shim
    sys.exit(asyncio.run(_amain()))


if __name__ == "__main__":  # pragma: no cover
    main()
