"""CLI: print the feeds whose trigger is due, per FeedProfile.

Consumed by the EXISTING data-operations daemon (run_data_operations.sh)
— NOT a new daemon. The wrapper runs ``python -m tpcore.feeds`` to get
the due stage list, then dispatches only those via the canonical
``ops.py``. Default behaviour (no profile / empty history) selects
every schedulable stage, so a misconfig degrades to today's blanket
sweep, never to "nothing runs".

Usage:
  python -m tpcore.feeds              # due stage names, one per line
  python -m tpcore.feeds --reasons    # 'stage<TAB>reason' (audit/log)
"""
from __future__ import annotations

import asyncio
import os
import sys


async def _amain(reasons: bool) -> int:
    from tpcore.db import build_asyncpg_pool
    from tpcore.feeds.dispatcher import select_due

    url = os.getenv("DATABASE_URL") or os.getenv("DATABASE_URL_IPV4")
    if not url:
        print("tpcore.feeds: DATABASE_URL not set", file=sys.stderr)
        return 2
    pool = await build_asyncpg_pool(url, max_size=2)
    try:
        due = await select_due(pool)
    finally:
        await pool.close()

    for d in due:
        if reasons:
            print(f"{d.stage}\t{d.feed}\t{d.reason}")
        else:
            print(d.stage)
    # Honest: empty due list is valid (nothing's cadence is up). The
    # wrapper treats "no stages" as a clean no-op, not an error.
    return 0


def main() -> None:  # pragma: no cover - CLI shim
    reasons = "--reasons" in sys.argv[1:]
    raise SystemExit(asyncio.run(_amain(reasons)))


if __name__ == "__main__":  # pragma: no cover
    main()
