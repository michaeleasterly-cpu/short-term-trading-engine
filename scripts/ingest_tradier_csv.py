"""Ingest ``tradier_bars_full.csv`` into ``platform.prices_daily``.

Plan A2. Reads the wide Tradier CSV produced by
``scripts/extract_tradier_full.py`` (22M rows, 1GB on disk), filters to
tickers that also appear in Alpaca's active asset list (NYSE/NASDAQ),
and inserts with ``source = 'tradier'`` using ``ON CONFLICT (ticker,
date) DO NOTHING``. Existing Alpaca rows are never overwritten — Tradier
fills gaps, primarily the pre-2020 history Alpaca's IEX free tier
doesn't cover.

The script is **idempotent** — re-running after a crash or partial run
is safe (DO NOTHING means already-loaded rows are skipped). It also
streams the CSV so memory stays bounded regardless of file size.

Run::

    DATABASE_URL=$DATABASE_URL_IPV4 \\
      ALPACA_KEY=... ALPACA_SECRET=... ALPACA_PAPER=true \\
      python scripts/ingest_tradier_csv.py

    # Skip the Alpaca check (load *every* CSV ticker) — useful for
    # backtest research dataset experiments:
    python scripts/ingest_tradier_csv.py --no-alpaca-filter
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import os
import sys
import time
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from tpcore.data.ingest_alpaca_bars import (
    _alpaca_broker_base,
    _alpaca_headers,
    fetch_active_us_equities,
)
from tpcore.db import build_asyncpg_pool

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = logging.getLogger("scripts.ingest_tradier_csv")

DEFAULT_CSV = Path("data/tradier_export/tradier_bars_full.csv")
COPY_BATCH = 5_000  # tune for executemany throughput vs. memory
# prices_daily.{open,high,low,close,adjusted_close} are NUMERIC(20,6) — i.e.,
# 14 integer digits + 6 fractional. Any |value| >= 10^14 overflows on insert.
# Tradier's wide export occasionally emits Inf or absurd values; reject them.
_NUMERIC_MAX = Decimal("1e14")


_INSERT_SQL = """
    INSERT INTO platform.prices_daily (
        ticker, date, open, high, low, close, volume,
        adjusted_close, delisted, delisting_date, source
    )
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, 'tradier')
    ON CONFLICT (ticker, date) DO NOTHING
"""


def _row_to_tuple(row: list[str]) -> tuple | None:
    """Coerce a CSV row into the parameter tuple ``_INSERT_SQL`` expects.

    Rejects (returns None) malformed rows — empty fields, non-numeric
    OHLCV, etc. The Tradier export has a few rows where a price is
    blank; skipping those is cheaper than aborting the load.
    """
    if len(row) < 7:
        return None
    ticker, date_str, o, h, low, c, v = row[:7]
    if not ticker or not date_str:
        return None
    try:
        d = date.fromisoformat(date_str)
        open_ = Decimal(o) if o else None
        high = Decimal(h) if h else None
        low_ = Decimal(low) if low else None
        close = Decimal(c) if c else None
        volume = int(v) if v else 0
    except (ValueError, ArithmeticError):
        return None
    if open_ is None or high is None or low_ is None or close is None:
        return None
    for x in (open_, high, low_, close):
        if not x.is_finite() or abs(x) >= _NUMERIC_MAX:
            return None
    return (
        ticker,
        d,
        open_,
        high,
        low_,
        close,
        volume,
        close,  # adjusted_close — Tradier history is split-adjusted
        False,  # delisted: unknown from CSV; assume active
        None,   # delisting_date
    )


async def _fetch_alpaca_active_set() -> set[str]:
    """Return the set of NYSE/NASDAQ tickers Alpaca currently treats as
    tradable. Used to gate the Tradier CSV so we don't pollute
    ``prices_daily`` with symbols Alpaca doesn't list."""
    headers = _alpaca_headers()
    async with httpx.AsyncClient(
        headers=headers, base_url=_alpaca_broker_base(), timeout=60.0
    ) as client:
        assets = await fetch_active_us_equities(client)
    return {a["symbol"] for a in assets}


async def ingest(
    pool: asyncpg.Pool,
    csv_path: Path,
    *,
    allowed_tickers: set[str] | None,
) -> dict[str, int]:
    """Stream ``csv_path`` into prices_daily. Returns a counters dict."""
    counters = {
        "rows_read": 0,
        "rows_skipped_filter": 0,  # ticker not in Alpaca active set
        "rows_skipped_malformed": 0,
        "rows_attempted": 0,
        "tickers_seen": 0,
    }
    seen_tickers: set[str] = set()

    batch: list[tuple] = []
    started = time.monotonic()

    with csv_path.open(newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        try:
            next(reader)  # header
        except StopIteration:
            return counters

        async with pool.acquire() as conn:
            for row in reader:
                counters["rows_read"] += 1
                if allowed_tickers is not None and row and row[0] not in allowed_tickers:
                    counters["rows_skipped_filter"] += 1
                    continue
                tup = _row_to_tuple(row)
                if tup is None:
                    counters["rows_skipped_malformed"] += 1
                    continue
                seen_tickers.add(tup[0])
                batch.append(tup)
                if len(batch) >= COPY_BATCH:
                    await conn.executemany(_INSERT_SQL, batch)
                    counters["rows_attempted"] += len(batch)
                    batch.clear()
                    if counters["rows_attempted"] % (COPY_BATCH * 20) == 0:
                        elapsed = time.monotonic() - started
                        rate = counters["rows_attempted"] / max(elapsed, 1e-3)
                        logger.info(
                            "ingest.progress rows_attempted=%d rows_read=%d "
                            "tickers=%d rate=%.0f/s",
                            counters["rows_attempted"],
                            counters["rows_read"],
                            len(seen_tickers),
                            rate,
                        )

            if batch:
                await conn.executemany(_INSERT_SQL, batch)
                counters["rows_attempted"] += len(batch)
                batch.clear()

    counters["tickers_seen"] = len(seen_tickers)
    return counters


async def amain(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db_url = os.getenv("DATABASE_URL") or os.getenv("DATABASE_URL_IPV4")
    if not db_url:
        print(
            "DATABASE_URL not set. Locally: export DATABASE_URL=$DATABASE_URL_IPV4 "
            "(see project memory on Supabase's dual URL setup).",
            file=sys.stderr,
        )
        return 2

    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"CSV not found: {csv_path}", file=sys.stderr)
        return 3

    if args.no_alpaca_filter:
        logger.info("alpaca_filter_disabled — every CSV ticker will be loaded")
        allowed: set[str] | None = None
    else:
        logger.info("alpaca_filter.fetching_active_universe")
        allowed = await _fetch_alpaca_active_set()
        logger.info("alpaca_filter.active_count count=%d", len(allowed))

    pool = await build_asyncpg_pool(db_url)
    started = time.monotonic()
    try:
        counters = await ingest(pool, csv_path, allowed_tickers=allowed)
    finally:
        await pool.close()
    duration = time.monotonic() - started

    print()
    print(f"ingest complete in {duration:.1f}s")
    print(f"  rows_read              = {counters['rows_read']:,}")
    print(f"  rows_skipped_filter    = {counters['rows_skipped_filter']:,}")
    print(f"  rows_skipped_malformed = {counters['rows_skipped_malformed']:,}")
    print(f"  rows_attempted         = {counters['rows_attempted']:,}")
    print(f"  tickers_seen           = {counters['tickers_seen']:,}")
    print()
    print(
        "Note: rows_attempted includes ON CONFLICT DO NOTHING — actual "
        "newly-inserted rows are <= attempted. Query "
        "platform.prices_daily WHERE source='tradier' for the post-load count."
    )
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument(
        "--csv",
        default=str(DEFAULT_CSV),
        help=f"Path to the Tradier wide-export CSV (default: {DEFAULT_CSV}).",
    )
    p.add_argument(
        "--no-alpaca-filter",
        action="store_true",
        help="Load every ticker in the CSV — skip the Alpaca active-asset gate.",
    )
    return p.parse_args(argv)


def main() -> None:  # pragma: no cover
    raise SystemExit(asyncio.run(amain(_parse_args())))


if __name__ == "__main__":
    main()
