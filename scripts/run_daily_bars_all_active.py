"""One-shot local driver for the ``daily_bars`` handler in ``all_active`` mode.

Plan A1. Bypasses the Railway ingestion engine (currently broken) and
invokes ``tpcore.ingestion.handlers.handle_daily_bars`` directly with
config ``{"universe":"all_active", "min_price":5.0, "min_volume":250000,
"lookback_days":7}``. Same code path the engine would run, just with the
local Python process holding the DB pool.

Run::

    DATABASE_URL=$DATABASE_URL_IPV4 \\
      ALPACA_KEY=... ALPACA_SECRET=... ALPACA_PAPER=true \\
      python scripts/run_daily_bars_all_active.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time

from tpcore.db import build_asyncpg_pool
from tpcore.ingestion.handlers import handle_daily_bars


async def amain() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    db_url = os.getenv("DATABASE_URL") or os.getenv("DATABASE_URL_IPV4")
    if not db_url:
        print("DATABASE_URL not set", file=sys.stderr)
        return 2

    cfg = {
        "universe": "all_active",
        "min_price": 5.0,
        "min_volume": 250_000,
        "lookback_days": 7,
        "batch_size": 50,
        "inter_batch_sleep_sec": 0.3,
    }
    pool = await build_asyncpg_pool(db_url, max_size=8)
    started = time.monotonic()
    try:
        rows = await handle_daily_bars(pool, cfg)
    finally:
        await pool.close()
    duration = time.monotonic() - started
    print(f"\nall_active sweep complete in {duration:.1f}s — rows_upserted={rows}")
    return 0


def main() -> None:  # pragma: no cover
    raise SystemExit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
