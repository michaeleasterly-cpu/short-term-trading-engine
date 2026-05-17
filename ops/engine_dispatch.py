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
import os
import sys
from datetime import UTC, datetime

import structlog

from tpcore.db import build_asyncpg_pool
from tpcore.engine_profile import should_fire

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


async def dispatch_once(pool, now: datetime) -> None:
    for engine in ROSTER:
        decision = await should_fire(engine, now, pool)
        if decision.fire:
            logger.info("engine_dispatch.dispatched", engine=engine)
            await _invoke_scheduler(engine)
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
