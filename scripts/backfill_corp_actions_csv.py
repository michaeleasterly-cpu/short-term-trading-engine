"""Phase 1: Alpaca corporate actions → CSV.

Mirrors the Alpaca-bar backfill pattern. Pulls forward-splits +
cash-dividends from ``/v1/corporate-actions`` for tier ≤ 2 tickers (or
``--tickers`` override) over the given date range, filters each event
through the same physical-truth predicate the validation suite
enforces, writes to ``data/corp_actions_backfill/corp_actions_<ts>.csv``.

Phase 2 (``load_corp_actions_csv.py``) re-validates each row and upserts.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import os
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx

from tpcore.data.ingest_corporate_actions import (
    DEFAULT_TYPES,
    fetch_corporate_actions,
)
from tpcore.db import build_asyncpg_pool

logger = logging.getLogger("scripts.backfill_corp_actions_csv")

OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "corp_actions_backfill"

CSV_COLS = ("ticker", "action_date", "action_type", "ratio", "raw_data")
BATCH_SIZE = 50  # Alpaca corporate-actions accepts smaller batches than bars


def _physical_ok(record: dict) -> bool:
    """Same predicate as ``corporate_actions_integrity``."""
    if not record.get("ticker") or not record.get("action_date") or not record.get("action_type"):
        return False
    ratio = record.get("ratio")
    if ratio is None or ratio <= 0 or ratio > 1000:
        return False
    if record["action_date"] > datetime.now(UTC).date() + timedelta(days=365):
        return False
    return True


async def _list_universe(pool, tickers_override: list[str] | None) -> list[str]:
    if tickers_override:
        return sorted({t.strip().upper() for t in tickers_override if t.strip()})
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT ticker FROM platform.liquidity_tiers WHERE tier <= 2 ORDER BY ticker"
        )
    return [r["ticker"] for r in rows]


async def amain(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db_url = os.getenv("DATABASE_URL") or os.getenv("DATABASE_URL_IPV4")
    if not db_url:
        print("FAILED — DATABASE_URL not set", file=sys.stderr)
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    out_path = OUT_DIR / f"corp_actions_{ts}.csv"
    logger.info("writing to %s", out_path)

    start_d = date.fromisoformat(args.since) if args.since else date(2012, 1, 1)
    end_d = date.today()  # noqa: DTZ011

    pool = await build_asyncpg_pool(db_url, max_size=2)
    try:
        tickers_override = [t.strip() for t in args.tickers.split(",") if t.strip()] if args.tickers else None
        tickers = await _list_universe(pool, tickers_override)
    finally:
        await pool.close()
    if not tickers:
        logger.error("no tickers to process")
        return 1
    logger.info("%d tickers, %s to %s", len(tickers), start_d, end_d)

    kept = dropped = 0
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_COLS)
        w.writeheader()
        async with httpx.AsyncClient(
            base_url="https://data.alpaca.markets",
            headers={
                "APCA-API-KEY-ID": os.environ.get("ALPACA_KEY", ""),
                "APCA-API-SECRET-KEY": os.environ.get("ALPACA_SECRET", ""),
            },
            timeout=60.0,
        ) as client:
            for i in range(0, len(tickers), BATCH_SIZE):
                batch = tickers[i:i + BATCH_SIZE]
                try:
                    events = await fetch_corporate_actions(
                        client,
                        symbols=batch,
                        start=start_d,
                        end=end_d,
                        types=list(DEFAULT_TYPES),
                    )
                except httpx.HTTPStatusError as exc:
                    logger.warning("batch failed: status=%s body=%s", exc.response.status_code, exc.response.text[:120])
                    continue
                for e in events:
                    if not _physical_ok(e):
                        dropped += 1
                        continue
                    w.writerow({
                        "ticker": e["ticker"],
                        "action_date": e["action_date"].isoformat(),
                        "action_type": e["action_type"],
                        "ratio": str(e["ratio"]),
                        "raw_data": json.dumps(e.get("raw_data") or {}, default=str),
                    })
                    kept += 1
                logger.info(
                    "batch %d/%d: %d tickers, kept_so_far=%d dropped_so_far=%d",
                    (i // BATCH_SIZE) + 1,
                    (len(tickers) + BATCH_SIZE - 1) // BATCH_SIZE,
                    len(batch), kept, dropped,
                )
                fh.flush()
                await asyncio.sleep(0.3)

    logger.info("done: kept=%d dropped=%d -> %s", kept, dropped, out_path)
    print(out_path)
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--tickers", help="comma-separated tickers; default = liquidity_tiers tier ≤ 2")
    p.add_argument("--since", help="ISO start date; default 2012-01-01")
    return p.parse_args(argv)


def main() -> None:  # pragma: no cover
    raise SystemExit(asyncio.run(amain(_parse_args())))


if __name__ == "__main__":
    main()
