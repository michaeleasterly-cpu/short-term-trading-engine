"""Railway cron entry point for the corporate-actions pipeline.

Two-step run, weekly:

1. ``tpcore.data.ingest_corporate_actions.fetch_corporate_actions`` →
   ``upsert_corporate_actions`` for the 50-name backtest universe.
2. ``tpcore.data.apply_splits.apply_all_splits`` to back-adjust any raw
   bars in ``platform.prices_daily`` (most importantly AAPL, where Alpaca's
   IEX free tier returns unadjusted historical prices despite
   ``adjustment="all"``).

Operational pattern mirrors ``ops/cron_validation.py``:

* loads ``DATABASE_URL`` (or ``DATABASE_URL_IPV4`` for the local Supabase
  pooler) from the env;
* opens an asyncpg pool, runs the two steps;
* exits 0 on success, 1 on failure.

Cron schedule: weekly, Sunday 04:00 UTC (see ``railway.json``) — runs
two hours before the validation suite, so any newly-applied splits land
before the splits check looks at the data.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import UTC, date, datetime

import httpx
import structlog

from tpcore.backtest.universe import DEFAULT_BACKTEST_UNIVERSE
from tpcore.data.apply_splits import apply_all_splits
from tpcore.data.ingest_alpaca_bars import _alpaca_headers
from tpcore.data.ingest_corporate_actions import (
    DEFAULT_TYPES,
    fetch_corporate_actions,
    upsert_corporate_actions,
)
from tpcore.db import build_asyncpg_pool

logger = structlog.get_logger(__name__)

_DATA_BASE = "https://data.alpaca.markets"
_INGEST_START = date(2018, 1, 1)
_CHUNK_SIZE = 20

# 50-name backtest universe — single source of truth at
# ``tpcore.backtest.universe.DEFAULT_BACKTEST_UNIVERSE``. The previous
# duplicated-with-comment pattern (rationale: "scripts/ isn't on the
# installed package path") was retired 2026-05-20 once the constant
# moved into ``tpcore.backtest``.
UNIVERSE: tuple[str, ...] = DEFAULT_BACKTEST_UNIVERSE


async def _ingest_universe(pool, *, end: date) -> int:
    """Chunk symbols into URL-friendly groups and ingest each chunk."""
    headers = _alpaca_headers()
    total = 0
    async with httpx.AsyncClient(headers=headers, base_url=_DATA_BASE, timeout=60.0) as client:
        for i in range(0, len(UNIVERSE), _CHUNK_SIZE):
            chunk = list(UNIVERSE[i : i + _CHUNK_SIZE])
            actions = await fetch_corporate_actions(
                client,
                symbols=chunk,
                start=_INGEST_START,
                end=end,
                types=list(DEFAULT_TYPES),
            )
            if actions:
                await upsert_corporate_actions(pool, actions)
            total += len(actions)
            logger.info(
                "corporate_actions.cron.chunk_done",
                chunk_size=len(chunk),
                n_actions=len(actions),
            )
    return total


async def _amain() -> int:
    db_url = os.getenv("DATABASE_URL") or os.getenv("DATABASE_URL_IPV4")
    if not db_url:
        print("DATABASE_URL not set", file=sys.stderr)
        return 2

    today = datetime.now(UTC).date()
    try:
        pool = await build_asyncpg_pool(db_url)
        try:
            n_actions = await _ingest_universe(pool, end=today)
            split_summary = await apply_all_splits(pool, only_tickers=list(UNIVERSE))
        finally:
            await pool.close()
    except Exception as exc:
        logger.exception("corporate_actions.cron.run_failed", error=str(exc))
        return 1

    n_applied = len(split_summary["applied"])
    n_skipped = len(split_summary["skipped"])
    print(
        f"corporate-actions cron OK  ingested={n_actions}  "
        f"splits_applied={n_applied}  splits_skipped={n_skipped}"
    )
    for a in split_summary["applied"]:
        print(
            f"  APPLY {a['ticker']:6s} rows={a['n_rows_updated']} "
            f"before={a['before']} after={a['after']}"
        )
    return 0


def main() -> None:  # pragma: no cover - CLI shim
    raise SystemExit(asyncio.run(_amain()))


if __name__ == "__main__":  # pragma: no cover
    main()
