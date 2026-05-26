"""Allocator heartbeat — safety-net cron when ``engine_service`` is down.

The allocator is event-driven on ``DATA_OPERATIONS_COMPLETE`` via
``ops/engine_dispatch.py`` (Sub-project C, PR #17, 2026-05-17 — see
``scripts/install_all_daemons.sh`` L20-22 + L43-44). The cron path was
retired then; this module re-introduces a *thin* heartbeat:

* Consults :func:`tpcore.engine_profile.should_fire` for the allocator
  (the SAME canonical gate ``_dispatch_allocator`` uses — single source
  of truth for "should the allocator run *right now*").
* If ``should_fire`` returns ``fire=False``, the heartbeat exits clean
  and surfaces the reason — covers every no-op case structurally:

  * not a cadence boundary (Tue–Fri or a non-first-trading-day Monday)
  * already ran this cycle (daemon path landed earlier today)
  * supervisor hold / data not ready / unprofiled / lifecycle

* If ``should_fire`` returns ``fire=True``, the heartbeat spawns
  ``python scripts/ops.py --allocate`` as an isolated subprocess (the
  SAME canonical command ``_invoke_allocator`` uses, reused not
  duplicated). The ``(engine, allocation_date)`` unique constraint is
  the last-line backstop; a race with the daemon can't double-allocate.

Why route the cadence/already-ran check through ``should_fire`` and not
re-derive it: the gate ladder (profiled → cadence → market-closed →
supervisor hold → data ready → not already ran) is the dispatcher's SoT;
duplicating any of those checks in the cron would be a drift hazard
exactly like the engine-manifest sentinel-fence pattern guards against.

Crash-isolated end-to-end: a spawn failure, a non-zero exit, or a
should_fire raise logs and returns — NEVER raises. The heartbeat is a
safety net; an erroring heartbeat must not page the operator at 3am.

Invariants:
* No new daemon. This is a cron-fired one-shot.
* No engine_profile mutation.
* Two-daemon invariant preserved (cron LaunchAgent is NOT in the
  ``install_all_daemons.sh`` for-loop closed whitelist; lives as a
  sibling installer call — ``scripts/tests/test_two_daemon_invariant.py``
  pins the loop tokens explicitly).
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


ALLOCATOR_ENGINE = "allocator"


async def fire_allocator_subprocess() -> int:
    """Spawn the canonical allocator invocation as a child subprocess.

    Mirrors :func:`ops.engine_dispatch._invoke_allocator` (the
    operator's stated "EXACT canonical command the retired launchd cron
    ran" — ``python scripts/ops.py --allocate``, spec C §3b / D-C2).
    Distinction from ``_invoke_allocator``: that helper is reused inside
    ``_dispatch_allocator`` (event-driven path); this helper is the
    cron-fired safety-net entry. Both routes converge on the same
    process spawn so the allocator's instrumentation, gating, and
    persistence semantics are byte-identical regardless of trigger.

    Returns the subprocess exit code, or ``-1`` on a spawn-time
    exception. NEVER raises.
    """
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "scripts/ops.py", "--allocate", cwd=repo,
        )
        rc = await proc.wait()
    except Exception as exc:  # noqa: BLE001 — isolate; heartbeat must not raise
        logger.error("allocator_heartbeat.spawn_failed", error=str(exc))
        return -1
    if rc == 0:
        logger.info("allocator_heartbeat.fired_ok")
    else:
        logger.error("allocator_heartbeat.fired_failed", returncode=rc)
    return rc


async def _write_daemon_heartbeat(pool, status: str) -> None:
    """UPSERT the allocator row in platform.daemon_heartbeats.

    Writer side of the daemon_freshness check (added 2026-05-26).
    Same UPSERT shape as tpcore/trade_monitor.py + engine_service +
    data_operations writers. allocator is a cron (daily 6:30 local =
    22:30 UTC); daemon_freshness tolerates 6h for this daemon. Status
    captures the outcome: 'healthy' on gate_closed/fired_inline,
    'degraded' on check_failed.
    """
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO platform.daemon_heartbeats
                    (daemon_name, last_heartbeat, status)
                VALUES ('allocator', now(), $1)
                ON CONFLICT (daemon_name) DO UPDATE
                    SET last_heartbeat = EXCLUDED.last_heartbeat,
                        status = EXCLUDED.status
                """,
                status,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("allocator_heartbeat.heartbeat_write_failed", error=str(exc))


async def heartbeat(pool, now: datetime | None = None) -> str:
    """One heartbeat decision. Returns a short outcome tag:

    * ``"gate_closed"`` — ``should_fire`` returned ``fire=False`` (any
      reason — not a cadence boundary, already ran, data not ready,
      supervisor hold, etc.). Reason is logged.
    * ``"fired_inline"`` — ``should_fire`` returned ``fire=True`` and
      the subprocess was spawned. Spawn rc is logged separately.
    * ``"check_failed"`` — ``should_fire`` raised (defense-in-depth;
      ``should_fire`` is itself fail-CLOSED so this is rare).

    The outcome tag is the observable surface; the caller logs it.
    Always writes the daemon_heartbeats row regardless of outcome.
    """
    now = now or datetime.now(UTC)
    try:
        decision = await should_fire(ALLOCATOR_ENGINE, now, pool)
    except Exception as exc:  # noqa: BLE001 — isolate; never abort the heartbeat
        logger.error("allocator_heartbeat.check_failed", error=str(exc))
        await _write_daemon_heartbeat(pool, "degraded")
        return "check_failed"
    if not decision.fire:
        logger.info("allocator_heartbeat.gate_closed",
                    reason=decision.reason, checks=dict(decision.checks))
        await _write_daemon_heartbeat(pool, "healthy")
        return "gate_closed"
    logger.warning("allocator_heartbeat.daemon_silent_firing_inline",
                   now=now.isoformat())
    await fire_allocator_subprocess()
    await _write_daemon_heartbeat(pool, "healthy")
    return "fired_inline"


async def _amain() -> int:
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_URL_IPV4")
    if not dsn:
        logger.error("allocator_heartbeat.no_dsn",
                     note="set DATABASE_URL or DATABASE_URL_IPV4")
        return 1
    pool = await build_asyncpg_pool(dsn, max_size=2)
    try:
        outcome = await heartbeat(pool)
        logger.info("allocator_heartbeat.outcome", outcome=outcome)
        return 0
    finally:
        await pool.close()


def main() -> None:  # pragma: no cover — CLI shim
    sys.exit(asyncio.run(_amain()))


if __name__ == "__main__":  # pragma: no cover
    main()
