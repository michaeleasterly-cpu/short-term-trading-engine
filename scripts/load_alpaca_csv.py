"""Phase 2 of the historical backfill — CSV → prices_daily.

Reads an ``alpaca_bars_backfill_*.csv`` produced by
``backfill_alpaca_csv.py``, re-applies the same physical-truth predicate
(belt-and-suspenders against any drift between phases), and upserts the
clean rows into ``platform.prices_daily``.

Idempotent — uses ``INSERT … ON CONFLICT (ticker, date) DO UPDATE`` so
re-running on the same CSV is a no-op. Bars already in the table get
updated; missing bars get inserted.

Usage::

    scripts/run_load_alpaca_csv.sh                       # newest CSV
    scripts/run_load_alpaca_csv.sh path/to/file.csv      # specific
    scripts/run_load_alpaca_csv.sh --dry-run             # parse only, no DB writes
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import os
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

from tpcore.db import build_asyncpg_pool

logger = logging.getLogger("scripts.load_alpaca_csv")

BACKFILL_DIR = Path(__file__).resolve().parent.parent / "data" / "alpaca_backfill"


def _passes_integrity(o: Decimal, h: Decimal, l_: Decimal, c: Decimal, v: int) -> bool:
    """Same predicate as ``tpcore.quality.validation.checks.row_integrity``."""
    if c <= 0 or c > 100_000_000:
        return False
    if h < max(o, c, l_):
        return False
    if l_ > min(o, c, h):
        return False
    return v >= 0


_UPSERT_SQL = """
    INSERT INTO platform.prices_daily
        (ticker, date, open, high, low, close, volume, adjusted_close, source, delisted)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $6, 'alpaca', false)
    ON CONFLICT (ticker, date) DO UPDATE SET
        open = EXCLUDED.open,
        high = EXCLUDED.high,
        low = EXCLUDED.low,
        close = EXCLUDED.close,
        volume = EXCLUDED.volume,
        adjusted_close = EXCLUDED.adjusted_close,
        source = 'alpaca'
"""


async def amain(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    csv_path = Path(args.csv) if args.csv else _newest_csv()
    if csv_path is None or not csv_path.exists():
        print(f"FAILED — CSV not found: {csv_path}", file=sys.stderr)
        return 1
    logger.info("loading from %s", csv_path)

    rows: list[tuple] = []
    parsed = 0
    rejected = 0
    with csv_path.open("r", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for r in reader:
            parsed += 1
            try:
                ticker = r["ticker"].strip().upper()
                d = date.fromisoformat(r["date"])
                o = Decimal(r["open"])
                h = Decimal(r["high"])
                l_ = Decimal(r["low"])
                c = Decimal(r["close"])
                v = int(r["volume"])
            except (KeyError, ValueError) as exc:
                rejected += 1
                logger.debug("row rejected (parse): %s — %s", r, exc)
                continue
            if not _passes_integrity(o, h, l_, c, v):
                rejected += 1
                logger.debug("row rejected (integrity): %s %s", ticker, d)
                continue
            rows.append((ticker, d, o, h, l_, c, v))

    logger.info("parsed=%d rejected=%d ready_to_upsert=%d", parsed, rejected, len(rows))
    if args.dry_run:
        logger.info("dry-run; not writing")
        return 0
    if not rows:
        logger.info("no rows to load")
        return 0

    db_url = os.getenv("DATABASE_URL") or os.getenv("DATABASE_URL_IPV4")
    if not db_url:
        print("FAILED — DATABASE_URL not set", file=sys.stderr)
        return 1
    pool = await build_asyncpg_pool(db_url, max_size=2)
    try:
        async with pool.acquire() as conn:
            # Batch upserts in chunks to avoid mega-parameter-counts.
            CHUNK = 5000
            written = 0
            for i in range(0, len(rows), CHUNK):
                chunk = rows[i:i + CHUNK]
                await conn.executemany(_UPSERT_SQL, chunk)
                written += len(chunk)
                logger.info("upserted %d / %d", written, len(rows))
        logger.info("done; csv=%s rows_upserted=%d", csv_path.name, written)
        return 0
    finally:
        await pool.close()


def _newest_csv() -> Path | None:
    if not BACKFILL_DIR.exists():
        return None
    candidates = sorted(BACKFILL_DIR.glob("alpaca_bars_backfill_*.csv"))
    return candidates[-1] if candidates else None


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("csv", nargs="?", help="path to CSV; default = newest in data/alpaca_backfill/")
    p.add_argument("--dry-run", action="store_true", help="parse only, no DB writes")
    return p.parse_args(argv)


def main() -> None:  # pragma: no cover
    raise SystemExit(asyncio.run(amain(_parse_args())))


if __name__ == "__main__":
    main()
