"""Thin CLI entrypoint — ``python -m tpcore.selfheal``.

This is what ``run_data_operations.sh`` calls instead of a bespoke
bash heal loop. Exit code IS the contract:

* ``0`` — data layer is 100% green (after 0+ autonomous repairs). The
  wrapper may emit ``DATA_OPERATIONS_COMPLETE``.
* ``1`` — escalation: something is red that auto-heal could not (or
  must not) fix. The wrapper must NOT emit; engines must not trade.

All per-source logic lives in the HealSpec registry; this file only
wires pool + canonical runner into the generic orchestrator.
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid

import structlog

from tpcore.db import build_asyncpg_pool

from .orchestrator import run_self_heal
from .runner import make_canonical_runner

logger = structlog.get_logger(__name__)


async def _amain() -> int:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("selfheal: DATABASE_URL not set", file=sys.stderr)
        return 1

    run_id = str(uuid.uuid4())
    pool = await build_asyncpg_pool(db_url)
    try:
        outcome = await run_self_heal(pool, make_canonical_runner(run_id))
    finally:
        await pool.close()

    print("=" * 64)
    print(f"SELF-HEAL  green={outcome.green}  iterations={outcome.iterations}")
    if outcome.healed:
        print(f"  repaired via canonical stage(s): {', '.join(outcome.healed)}")
    if outcome.escalated:
        print("  ESCALATED (operator must investigate — engines will NOT trade):")
        for source, reason in outcome.escalated:
            print(f"    - {source}: {reason}")
    print("=" * 64)
    return 0 if outcome.green else 1


def main() -> None:  # pragma: no cover — CLI shim
    raise SystemExit(asyncio.run(_amain()))


if __name__ == "__main__":  # pragma: no cover
    main()
