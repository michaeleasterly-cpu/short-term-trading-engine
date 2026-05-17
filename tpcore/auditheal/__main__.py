"""Thin CLI entrypoint — ``python -m tpcore.auditheal``.

A later phase wires this into run_data_operations.sh Step 3 instead of
the print-only run_audit_all_tables.sh. Exit code IS the contract:

* ``0`` — cross-table layer 100% green (after 0+ canonical
  remediations). Step 3 proceeds.
* ``1`` — escalation: a cross-table red auto-remediation could not (or
  must not) fix. Step 3 hard-stops; engines must not trade.

All per-check policy lives in REMEDIATION_SPECS; this file only wires
pool + the in-process structured audit + the canonical runner into the
generic orchestrator. The re-audit is the SAME in-process code path as
the detector (cannot drift).
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid

import structlog

from tpcore.audit.cross_table import run_cross_table_audit
from tpcore.db import build_asyncpg_pool
from tpcore.selfheal.runner import make_canonical_runner

from .orchestrator import run_audit_heal

logger = structlog.get_logger(__name__)


async def _amain() -> int:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("auditheal: DATABASE_URL not set", file=sys.stderr)
        return 1

    run_id = str(uuid.uuid4())
    pool = await build_asyncpg_pool(db_url)

    async def run_audit() -> int:
        await run_cross_table_audit(pool, persist=True)
        return 0

    try:
        outcome = await run_audit_heal(
            pool, make_canonical_runner(run_id), run_audit
        )
    finally:
        await pool.close()

    print("=" * 64)
    print(f"AUDIT-HEAL  green={outcome.green}  "
          f"iterations={outcome.iterations}")
    if outcome.remediated:
        print(f"  remediated via canonical stage(s): "
              f"{', '.join(outcome.remediated)}")
    if outcome.escalated:
        print("  ESCALATED (operator must investigate — engines will "
              "NOT trade):")
        for src, reason in outcome.escalated:
            print(f"    - {src}: {reason}")
    print("=" * 64)
    return 0 if outcome.green else 1


def main() -> None:  # pragma: no cover — CLI shim
    raise SystemExit(asyncio.run(_amain()))


if __name__ == "__main__":  # pragma: no cover
    main()
