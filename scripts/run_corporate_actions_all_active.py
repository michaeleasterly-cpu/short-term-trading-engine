"""One-shot local driver for the ``corporate_actions`` handler in ``all_active`` mode.

Mirror of ``scripts/run_daily_bars_all_active.py``. Bypasses the (paused)
Railway ingestion engine and invokes ``handle_corporate_actions`` directly
with ``{"universe": "all_active"}`` so Alpaca corporate-action events are
pulled for every distinct ticker in ``platform.prices_daily`` and
``apply_all_splits`` then runs across the full table.

Run::

    DATABASE_URL=$DATABASE_URL_IPV4 \\
      ALPACA_KEY=... ALPACA_SECRET=... ALPACA_PAPER=true \\
      python scripts/run_corporate_actions_all_active.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time

from tpcore.db import build_asyncpg_pool
from tpcore.ingestion.handlers import handle_corporate_actions


async def amain() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    db_url = os.getenv("DATABASE_URL") or os.getenv("DATABASE_URL_IPV4")
    if not db_url:
        print("DATABASE_URL not set", file=sys.stderr)
        return 2

    cfg = {"universe": "all_active", "ingest_start": "2018-01-01"}
    pool = await build_asyncpg_pool(db_url, max_size=8)
    started = time.monotonic()
    try:
        actions = await handle_corporate_actions(pool, cfg)
    finally:
        await pool.close()
    duration = time.monotonic() - started
    print(f"\ncorporate_actions all_active complete in {duration:.1f}s — actions={actions}")
    return 0


def main() -> None:  # pragma: no cover
    raise SystemExit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
