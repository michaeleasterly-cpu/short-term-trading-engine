"""Phase 2: corp_actions CSV → platform.corporate_actions.

Re-validates each row, upserts ON CONFLICT (ticker, action_date, action_type).
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import os
import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from tpcore.db import build_asyncpg_pool

logger = logging.getLogger("scripts.load_corp_actions_csv")

BACKFILL_DIR = Path(__file__).resolve().parent.parent / "data" / "corp_actions_backfill"

_UPSERT_SQL = """
    INSERT INTO platform.corporate_actions (ticker, action_date, action_type, ratio, raw_data)
    VALUES ($1, $2, $3, $4, $5::jsonb)
    ON CONFLICT (ticker, action_date, action_type) DO UPDATE SET
        ratio = EXCLUDED.ratio,
        raw_data = EXCLUDED.raw_data
"""


def _physical_ok(ratio: Decimal, action_date: date) -> bool:
    if ratio <= 0 or ratio > 1000:
        return False
    if action_date > datetime.now(UTC).date() + timedelta(days=365):
        return False
    return True


async def amain(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    csv_path = Path(args.csv) if args.csv else _newest_csv()
    if csv_path is None or not csv_path.exists():
        print(f"FAILED — CSV not found: {csv_path}", file=sys.stderr)
        return 1
    logger.info("loading from %s", csv_path)

    rows: list[tuple] = []
    parsed = rejected = 0
    with csv_path.open("r", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            parsed += 1
            try:
                ticker = r["ticker"].strip().upper()
                action_date = date.fromisoformat(r["action_date"])
                action_type = r["action_type"].strip()
                ratio = Decimal(r["ratio"])
                raw_data = r["raw_data"]
            except (KeyError, ValueError):
                rejected += 1
                continue
            if not action_type or not _physical_ok(ratio, action_date):
                rejected += 1
                continue
            rows.append((ticker, action_date, action_type, ratio, raw_data))

    logger.info("parsed=%d rejected=%d ready=%d", parsed, rejected, len(rows))
    if args.dry_run or not rows:
        return 0

    db_url = os.getenv("DATABASE_URL") or os.getenv("DATABASE_URL_IPV4")
    if not db_url:
        print("FAILED — DATABASE_URL not set", file=sys.stderr)
        return 1
    pool = await build_asyncpg_pool(db_url, max_size=2)
    try:
        async with pool.acquire() as conn:
            CHUNK = 2000
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
    candidates = sorted(BACKFILL_DIR.glob("corp_actions_*.csv"))
    return candidates[-1] if candidates else None


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("csv", nargs="?", help="path to CSV; default = newest")
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args(argv)


def main() -> None:  # pragma: no cover
    raise SystemExit(asyncio.run(amain(_parse_args())))


if __name__ == "__main__":
    main()
