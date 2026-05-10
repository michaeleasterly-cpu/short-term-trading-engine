"""``IngestionEngine`` — drives ``platform.ingestion_jobs``.

Operational model:

1. ``tick()`` selects rows where ``enabled = true`` and ``next_run <=
   now()`` AND ``(last_status IS DISTINCT FROM 'running' OR last_run_at
   < now() - 30 minutes)`` — the staleness clause is the recovery path
   for a process crash that left a job stuck in 'running'.
2. For each row, it issues a guarded UPDATE that flips ``last_status``
   to 'running' only if the precondition still holds. If the UPDATE
   returns 0 rows, another ticker raced ahead — skip.
3. The handler runs. Success and failure are both treated as terminal
   for this fire — failed jobs do NOT auto-retry inside the same tick;
   they wait for their next cron occurrence.
4. ``last_run_at``, ``last_status``, ``last_error``, ``last_duration_ms``,
   and ``next_run`` are written back in a single UPDATE.

``run_forever()`` is the persistent entry point. It loops ``tick()`` →
sleep → repeat. The sleep is interrupted only by ``asyncio.CancelledError``
(graceful shutdown on SIGTERM via ``asyncio.run``).
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Callable

import structlog

from tpcore.ingestion.cron_eval import next_run_after
from tpcore.ingestion.handlers import HANDLERS, HandlerFn

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

    from tpcore.logging import DBLogHandler

logger = structlog.get_logger(__name__)

# How long a 'running' row may sit before the engine assumes the previous
# worker died and reclaims it. 30 minutes covers the longest expected job
# (corporate_actions ingest sweeping the universe) plus generous slack.
_STALE_RUNNING_AFTER = timedelta(minutes=30)


@dataclass(frozen=True)
class JobResult:
    job_name: str
    status: str  # 'success' | 'failed' | 'skipped_no_handler' | 'skipped_lost_race'
    duration_ms: int
    rows_ingested: int | None = None
    error: str | None = None


class IngestionEngine:
    """Tick-driven dispatcher for ``platform.ingestion_jobs``.

    Args:
        pool: asyncpg pool. The engine uses but does not own it.
        handlers: mapping of ``job_name`` → async callable. Defaults to
            the module-level :data:`HANDLERS` registry.
        clock: factory returning the current ``datetime`` (UTC). Injected
            for tests; production passes ``datetime.now(UTC)``.
        db_log: optional ``DBLogHandler`` bound to a single per-engine
            ``run_id``. When set, the engine emits ``INGESTION_TICK``,
            ``INGESTION_COMPLETE``, ``INGESTION_FAILED``, and ``SHUTDOWN``
            events to ``platform.application_log``. When ``None``, the
            engine still works but only logs to structlog/stdout.
    """

    def __init__(
        self,
        pool: "asyncpg.Pool",
        *,
        handlers: dict[str, HandlerFn] | None = None,
        clock: Callable[[], datetime] | None = None,
        db_log: "DBLogHandler | None" = None,
    ) -> None:
        self._pool = pool
        self._handlers = handlers if handlers is not None else HANDLERS
        self._clock = clock or (lambda: datetime.now(UTC))
        self._db_log = db_log

    async def tick(self) -> list[JobResult]:
        """Run one iteration. Returns a result per due job.

        Emits an ``INGESTION_TICK`` event to ``application_log`` (when
        ``db_log`` is wired) regardless of whether anything was due — the
        heartbeat is the point. Per-job ``INGESTION_COMPLETE`` /
        ``INGESTION_FAILED`` events are emitted from :meth:`_run_one`.
        """
        now = self._clock()
        stale_cutoff = now - _STALE_RUNNING_AFTER
        due = await self._fetch_due(now=now, stale_cutoff=stale_cutoff)
        if self._db_log is not None:
            await self._db_log.log(
                "INGESTION_TICK",
                f"{len(due)} job(s) due",
                severity="INFO",
                data={"due_jobs": len(due), "job_names": [r["job_name"] for r in due]},
            )
        results: list[JobResult] = []
        for row in due:
            result = await self._run_one(row, now=now, stale_cutoff=stale_cutoff)
            results.append(result)
        return results

    async def run_forever(self, *, sleep_sec: float = 60.0) -> None:
        """Loop ``tick()`` forever. Catches CancelledError for clean shutdown.

        STARTUP / SHUTDOWN application_log events are owned by the entry
        point (``ops/ingestion_engine.py``) — that's the layer that knows
        the run's exit_code and total duration, mirroring the scheduler
        pattern established in commit ``ff468de``.
        """
        logger.info("ingestion.engine.start", sleep_sec=sleep_sec)
        try:
            while True:
                try:
                    results = await self.tick()
                except Exception as exc:
                    # Defensive: if tick() itself blows up (DB outage, bad
                    # schema, etc.) we don't want the worker to die — just
                    # log and back off.
                    logger.exception("ingestion.engine.tick_failed", error=str(exc))
                    results = []
                if results:
                    logger.info(
                        "ingestion.engine.tick_done",
                        jobs_run=len(results),
                        statuses=[r.status for r in results],
                    )
                await asyncio.sleep(sleep_sec)
        except asyncio.CancelledError:
            logger.info("ingestion.engine.shutdown")
            raise

    # ─── Internals ───────────────────────────────────────────────────────

    async def _fetch_due(
        self, *, now: datetime, stale_cutoff: datetime
    ) -> list[dict]:
        sql = """
            SELECT job_name, schedule, provider, config
            FROM platform.ingestion_jobs
            WHERE enabled = true
              AND next_run <= $1
              AND (
                  last_status IS DISTINCT FROM 'running'
                  OR last_run_at IS NULL
                  OR last_run_at < $2
              )
            ORDER BY next_run
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql, now, stale_cutoff)
        return [dict(r) for r in rows]

    async def _claim(
        self, job_name: str, *, now: datetime, stale_cutoff: datetime
    ) -> bool:
        """Guarded UPDATE — flip last_status to 'running' iff still claimable.

        Returns True if this caller now owns the row, False if another
        worker (or a fresh run) raced ahead.
        """
        sql = """
            UPDATE platform.ingestion_jobs
            SET last_status = 'running',
                last_run_at = $2,
                updated_at = $2
            WHERE job_name = $1
              AND enabled = true
              AND next_run <= $2
              AND (
                  last_status IS DISTINCT FROM 'running'
                  OR last_run_at IS NULL
                  OR last_run_at < $3
              )
            RETURNING job_name
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(sql, job_name, now, stale_cutoff)
        return row is not None

    async def _record_result(
        self,
        job_name: str,
        *,
        status: str,
        error: str | None,
        duration_ms: int,
        next_run: datetime,
        now: datetime,
    ) -> None:
        sql = """
            UPDATE platform.ingestion_jobs
            SET last_status = $2,
                last_error = $3,
                last_duration_ms = $4,
                next_run = $5,
                updated_at = $6
            WHERE job_name = $1
        """
        async with self._pool.acquire() as conn:
            await conn.execute(sql, job_name, status, error, duration_ms, next_run, now)

    async def _run_one(
        self, row: dict, *, now: datetime, stale_cutoff: datetime
    ) -> JobResult:
        job_name = row["job_name"]
        schedule = row["schedule"]
        config = row["config"] or {}
        # asyncpg returns jsonb as a string by default; tolerate both.
        if isinstance(config, str):
            import json

            config = json.loads(config)

        handler = self._handlers.get(job_name)
        if handler is None:
            logger.warning("ingestion.engine.no_handler", job_name=job_name)
            err = f"no handler registered for job_name={job_name!r}"
            next_run = next_run_after(schedule, now)
            await self._record_result(
                job_name,
                status="failed",
                error=err,
                duration_ms=0,
                next_run=next_run,
                now=now,
            )
            if self._db_log is not None:
                await self._db_log.log(
                    "INGESTION_FAILED",
                    f"{job_name} failed: {err}",
                    severity="ERROR",
                    data={"job_name": job_name, "error": err, "duration_ms": 0},
                )
            return JobResult(
                job_name=job_name,
                status="skipped_no_handler",
                duration_ms=0,
                error="no handler registered",
            )

        if not await self._claim(job_name, now=now, stale_cutoff=stale_cutoff):
            logger.info("ingestion.engine.lost_race", job_name=job_name)
            return JobResult(
                job_name=job_name, status="skipped_lost_race", duration_ms=0
            )

        logger.info("ingestion.engine.job_start", job_name=job_name)
        start = time.monotonic()
        error: str | None = None
        status = "success"
        rows_ingested: int | None = None
        try:
            ret = await handler(self._pool, config)
            if isinstance(ret, int):
                rows_ingested = ret
        except Exception as exc:
            status = "failed"
            error = str(exc)[:1000]
            logger.exception("ingestion.engine.job_failed", job_name=job_name, error=error)
        duration_ms = int((time.monotonic() - start) * 1000)

        next_run = next_run_after(schedule, now)
        await self._record_result(
            job_name,
            status=status,
            error=error,
            duration_ms=duration_ms,
            next_run=next_run,
            now=now,
        )
        logger.info(
            "ingestion.engine.job_done",
            job_name=job_name,
            status=status,
            duration_ms=duration_ms,
            rows_ingested=rows_ingested,
            next_run=next_run.isoformat(),
        )
        if self._db_log is not None:
            if status == "success":
                rows_str = "n/a" if rows_ingested is None else f"{rows_ingested} rows"
                await self._db_log.log(
                    "INGESTION_COMPLETE",
                    f"{job_name} completed: {rows_str}",
                    severity="INFO",
                    data={
                        "job_name": job_name,
                        "rows_ingested": rows_ingested,
                        "duration_ms": duration_ms,
                    },
                )
            else:
                await self._db_log.log(
                    "INGESTION_FAILED",
                    f"{job_name} failed: {error}",
                    severity="ERROR",
                    data={
                        "job_name": job_name,
                        "error": error,
                        "duration_ms": duration_ms,
                    },
                )
        return JobResult(
            job_name=job_name,
            status=status,
            duration_ms=duration_ms,
            rows_ingested=rows_ingested,
            error=error,
        )


__all__ = ["IngestionEngine", "JobResult"]
