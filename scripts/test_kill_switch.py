"""End-to-end kill-switch verification.

Flips ``platform.risk_state.kill_switch_active`` to ``true`` for one engine,
runs that engine's scheduler against the production pool, and asserts the
scheduler short-circuits at the new kill-switch check (zero candidates
scanned, zero trades submitted, no FMP / Alpaca API calls past
``register_engine``). Resets the kill switch to ``false`` on exit, even on
failure, so the live engine isn't left frozen.

Usage::

    python scripts/test_kill_switch.py --engine reversion
    python scripts/test_kill_switch.py --engine vector

Reads ``DATABASE_URL`` (or ``DATABASE_URL_IPV4`` for the local Supabase
pooler). Idempotent — safe to re-run.

This script is a verification harness, not part of the runtime cron path.
It uses the same Postgres-backed risk store the schedulers use, so the
flip is real — make sure no operator is going to be confused by a brief
``kill_switch_active=true`` window.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import UTC, date, datetime
from decimal import Decimal

import structlog

from tpcore.db import build_asyncpg_pool
from tpcore.risk.persistent_store import PostgresRiskStateStore

logger = structlog.get_logger(__name__)


async def _set_kill_switch(pool, engine: str, *, active: bool, reason: str | None) -> None:
    sql = """
        UPDATE platform.risk_state
           SET kill_switch_active = $2,
               kill_switch_reason = $3,
               updated_at         = now()
         WHERE engine = $1
    """
    async with pool.acquire() as conn:
        await conn.execute(sql, engine, active, reason)


async def _ensure_engine_row(pool, engine: str, *, equity: Decimal) -> None:
    """Create a baseline risk_state row if the engine has never run."""
    sql = """
        INSERT INTO platform.risk_state (
            engine, engine_equity, daily_pnl, weekly_pnl, open_positions,
            daily_reset_at, weekly_reset_at, kill_switch_active, updated_at
        )
        VALUES ($1, $2, 0, 0, 0, now() + interval '1 day', now() + interval '7 days',
                false, now())
        ON CONFLICT (engine) DO NOTHING
    """
    async with pool.acquire() as conn:
        await conn.execute(sql, engine, equity)


async def _run_engine(engine: str, as_of: date) -> object:
    """Invoke the engine's scheduler.run_once() with default config.

    The return type is the engine-specific RunSummary class (each engine
    declares its own), so we type as ``object`` and duck-type the
    ``n_candidates`` / ``n_submitted`` attrs at the call site.
    """
    if engine == "reversion":
        from reversion.scheduler import ReversionScheduler
        return await ReversionScheduler().run_once(as_of=as_of)
    if engine == "vector":
        from vector.scheduler import VectorScheduler
        return await VectorScheduler().run_once(as_of=as_of)
    raise SystemExit(f"unknown engine: {engine!r}")


async def _amain(engine: str) -> int:
    db_url = os.getenv("DATABASE_URL") or os.getenv("DATABASE_URL_IPV4")
    if not db_url:
        print("DATABASE_URL not set", file=sys.stderr)
        return 2

    pool = await build_asyncpg_pool(db_url)
    try:
        # Baseline the row in case the engine has never registered itself.
        await _ensure_engine_row(pool, engine, equity=Decimal("10000"))

        # 1. Flip kill switch ON.
        await _set_kill_switch(
            pool, engine, active=True, reason="test_kill_switch.py harness"
        )
        store = PostgresRiskStateStore(pool)
        before = await store.get(engine)
        assert before is not None and before.kill_switch_active, (
            f"failed to set kill switch on {engine}; got {before}"
        )
        logger.info("test_kill_switch.set_active", engine=engine)

        # 2. Run the scheduler. The startup kill-switch check should fire.
        as_of = datetime.now(UTC).date()
        summary = await _run_engine(engine, as_of)

        # 3. Verify zero work done.
        assert summary.n_candidates == 0, (
            f"{engine}: scheduler scanned {summary.n_candidates} candidates "
            f"despite kill switch — startup check missing or broken"
        )
        assert summary.n_submitted == 0, (
            f"{engine}: scheduler submitted {summary.n_submitted} trades "
            f"despite kill switch"
        )
        logger.info(
            "test_kill_switch.short_circuited",
            engine=engine,
            n_candidates=summary.n_candidates,
            n_submitted=summary.n_submitted,
        )
        print(f"PASS: {engine} scheduler short-circuited under kill switch.")
        return 0
    finally:
        # Always reset, even on failure — never leave a live engine frozen.
        await _set_kill_switch(pool, engine, active=False, reason=None)
        logger.info("test_kill_switch.reset", engine=engine)
        await pool.close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument(
        "--engine",
        choices=("reversion", "vector"),
        required=True,
        help="Which engine's kill switch to flip + verify.",
    )
    args = p.parse_args(argv)
    return asyncio.run(_amain(args.engine))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
