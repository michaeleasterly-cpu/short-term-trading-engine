"""Event-driven engine dispatcher (Sub-project B).

Replaces the unconditional bash engine loop. Per engine: consult
``tpcore.engine_profile.should_fire``. Fire → invoke that engine's
scheduler. Data-blocked → emit ENGINE_DATA_REQUEST and skip (async
hand-off to the data lane; NEVER self-heal in-process — that would
couple trade latency to data-repair and contend on the pooler).
See docs/superpowers/specs/2026-05-17-sub-project-b-event-driven-dispatch-design.md.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from datetime import UTC, datetime

import structlog

from tpcore.db import build_asyncpg_pool
from tpcore.engine_profile import cadence_window_start, profile_for, should_fire
from tpcore.quality.validation.capital_gate import failing_sources_for_engine

logger = structlog.get_logger(__name__)

ROSTER: tuple[str, ...] = ("reversion", "vector", "momentum", "sentinel")


async def _invoke_scheduler(engine: str) -> int:
    """Run one engine's scheduler as an isolated subprocess.

    Per-engine crash isolation: a non-zero exit is logged and the
    sweep continues to the next engine (mirrors the old bash loop's
    ``|| continue``). Args (e.g. --force) are NOT forwarded — the
    dispatcher is the gate; manual --force is a direct-invocation path.
    """
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", f"{engine}.scheduler", cwd=repo,
    )
    rc = await proc.wait()
    logger.info("engine_dispatch.scheduler_done", engine=engine, returncode=rc)
    return rc


_REQUEST_EVENT = "ENGINE_DATA_REQUEST"
_TERMINAL_EVENTS = ("DATA_REPAIR_COMPLETE", "DATA_REPAIR_ESCALATED")


async def _has_open_request(conn, engine: str, window_start: datetime) -> bool:
    """True if an ENGINE_DATA_REQUEST for this engine in the current
    cadence window has no terminal event yet (dedupe — at most one
    open request per (engine, cadence-window))."""
    row = await conn.fetchval(
        """
        SELECT 1 FROM platform.application_log r
        WHERE r.event_type = $1 AND r.engine = $2 AND r.recorded_at >= $3
          AND NOT EXISTS (
            SELECT 1 FROM platform.application_log t
            WHERE t.event_type = ANY($4::text[])
              AND (t.data->>'request_id') = (r.data->>'request_id'))
        LIMIT 1
        """,
        _REQUEST_EVENT, engine, window_start, list(_TERMINAL_EVENTS),
    )
    return row is not None


async def _emit_data_request(conn, engine: str, sources: list[str], reason: str) -> str:
    request_id = str(uuid.uuid4())
    payload = json.dumps({
        "schema": 1, "request_id": request_id,
        "engine": engine, "sources": sources, "reason": reason,
    })
    await conn.execute(
        """
        INSERT INTO platform.application_log
            (engine, run_id, event_type, severity, message, data)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb)
        """,
        engine, uuid.uuid4(), _REQUEST_EVENT, "WARNING",
        f"{engine} data-blocked: {reason}", payload,
    )
    logger.warning("engine_dispatch.data_request", engine=engine,
                    request_id=request_id, sources=sources)
    return request_id


async def dispatch_once(pool, now: datetime) -> None:
    for engine in ROSTER:
        decision = await should_fire(engine, now, pool)
        if decision.fire:
            logger.info("engine_dispatch.dispatched", engine=engine)
            await _invoke_scheduler(engine)
        elif decision.checks.get("data_ready") is False:
            prof = profile_for(engine)
            window_start = cadence_window_start(engine, now) if prof else now
            async with pool.acquire() as conn:
                if await _has_open_request(conn, engine, window_start):
                    logger.info("engine_dispatch.request_open", engine=engine)
                    continue
                sources = await failing_sources_for_engine(pool, engine)
                await _emit_data_request(conn, engine, sources, decision.reason)
        else:
            logger.info(
                "engine_dispatch.skipped", engine=engine,
                reason=decision.reason,
                data_ready=decision.checks.get("data_ready"),
            )


async def _amain() -> int:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL not set", file=sys.stderr)
        return 1
    pool = await build_asyncpg_pool(db_url)
    try:
        await dispatch_once(pool, now=datetime.now(UTC))
        return 0
    finally:
        await pool.close()


def main() -> None:  # pragma: no cover — CLI shim
    raise SystemExit(asyncio.run(_amain()))


if __name__ == "__main__":  # pragma: no cover
    main()
