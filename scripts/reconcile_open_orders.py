"""One-shot reconciler for ``platform.open_orders``.

Reads every ``pending`` row, queries Alpaca for the authoritative state,
mirrors the result into our DB. Same code path the trade monitor uses on
startup (``TradeMonitor.reconcile_pending_on_startup``) — invoked outside
the long-running daemon so it can run as a cron / ad-hoc cleanup.

Per the expert's 2026-05-13 directive (re: YUMC orphan): cleanest
audit trail is mirror-from-broker, not local DELETE. ``decision_data``
(the Sigma engine's full Tier1/Tier2 spec) is preserved either way.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import uuid

from tpcore.aar.writer import AARWriter
from tpcore.alpaca import AlpacaPaperBrokerAdapter
from tpcore.db import build_asyncpg_pool
from tpcore.trade_monitor import TradeMonitor

logger = logging.getLogger("scripts.reconcile_open_orders")


async def amain() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db_url = os.getenv("DATABASE_URL") or os.getenv("DATABASE_URL_IPV4")
    if not db_url:
        print("FAILED — DATABASE_URL not set", file=sys.stderr)
        return 1

    pool = await build_asyncpg_pool(db_url, max_size=4)
    try:
        broker = AlpacaPaperBrokerAdapter()
        aar_writer = AARWriter(pool)
        monitor = TradeMonitor(
            pool=pool, broker=broker, aar_writer=aar_writer,
            run_id=uuid.uuid4(),
        )
        n = await monitor.reconcile_pending_on_startup()
        logger.info("reconciled %s pending orders", n)
        return 0
    finally:
        await pool.close()


def main() -> None:  # pragma: no cover
    raise SystemExit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
