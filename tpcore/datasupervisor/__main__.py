"""Thin CLI — ``python -m tpcore.datasupervisor``. Wired as a Step in
run_data_operations.sh AFTER Step 4/4c. Exit 0 ALWAYS (state-tracking,
NOT a gate — never decides DATA_OPERATIONS_COMPLETE). Only a missing
DSN returns 1 (parity with selfheal __main__)."""
from __future__ import annotations

import asyncio
import os
import sys
import uuid

import structlog

from tpcore.datasupervisor.supervisor import datasupervise
from tpcore.db import build_asyncpg_pool

logger = structlog.get_logger(__name__)


async def _amain() -> int:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("datasupervisor: DATABASE_URL not set", file=sys.stderr)
        return 1
    pool = await build_asyncpg_pool(db_url)
    try:
        out = await datasupervise(pool, str(uuid.uuid4()))
    finally:
        await pool.close()
    print("=" * 64)
    print(f"DATA-SUPERVISOR opened={out.opened} cleared={out.cleared} "
          f"escalated={out.escalated} error={out.error}")
    print("=" * 64)
    return 0


def main() -> None:  # pragma: no cover
    raise SystemExit(asyncio.run(_amain()))


if __name__ == "__main__":  # pragma: no cover
    main()
