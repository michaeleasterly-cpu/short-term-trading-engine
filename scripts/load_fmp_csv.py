"""Phase 2: FMP CSV → fundamentals_quarterly.

Re-validates each row with the same physical-truth predicate, then
upserts ON CONFLICT (ticker, filing_date). Idempotent.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import os
import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from tpcore.db import build_asyncpg_pool

logger = logging.getLogger("scripts.load_fmp_csv")

BACKFILL_DIR = Path(__file__).resolve().parent.parent / "data" / "fmp_backfill"

_UPSERT_SQL = """
    INSERT INTO platform.fundamentals_quarterly (
        ticker, filing_date, period_end_date, period_label,
        net_income, fcf, operating_cash_flow, capex, revenue,
        total_assets, total_liabilities, current_assets, current_liabilities,
        receivables, cash_and_equivalents, shares_outstanding,
        recorded_at
    )
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17)
    ON CONFLICT (ticker, filing_date) DO UPDATE SET
        period_end_date = EXCLUDED.period_end_date,
        period_label = EXCLUDED.period_label,
        net_income = EXCLUDED.net_income,
        fcf = EXCLUDED.fcf,
        operating_cash_flow = EXCLUDED.operating_cash_flow,
        capex = EXCLUDED.capex,
        revenue = EXCLUDED.revenue,
        total_assets = EXCLUDED.total_assets,
        total_liabilities = EXCLUDED.total_liabilities,
        current_assets = EXCLUDED.current_assets,
        current_liabilities = EXCLUDED.current_liabilities,
        receivables = EXCLUDED.receivables,
        cash_and_equivalents = EXCLUDED.cash_and_equivalents,
        shares_outstanding = EXCLUDED.shares_outstanding,
        recorded_at = now()
"""


def _dec(v: str | None) -> Decimal | None:
    if v is None or v == "":
        return None
    return Decimal(v)


def _physical_ok(filing: date, period_end: date | None, shares: Decimal | None) -> bool:
    today = datetime.now(UTC).date()
    if filing > today:
        return False
    if period_end is not None and period_end > filing:
        return False
    if shares is not None and shares <= 0:
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
    now_utc = datetime.now(UTC)
    with csv_path.open("r", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            parsed += 1
            try:
                ticker = r["ticker"].strip().upper()
                filing = date.fromisoformat(r["filing_date"])
                period_end = date.fromisoformat(r["period_end_date"]) if r["period_end_date"] else None
                shares = _dec(r["shares_outstanding"])
            except (KeyError, ValueError):
                rejected += 1
                continue
            if not _physical_ok(filing, period_end, shares):
                rejected += 1
                continue
            rows.append((
                ticker, filing, period_end, r.get("period_label") or None,
                _dec(r.get("net_income")), _dec(r.get("fcf")),
                _dec(r.get("operating_cash_flow")), _dec(r.get("capex")),
                _dec(r.get("revenue")),
                _dec(r.get("total_assets")), _dec(r.get("total_liabilities")),
                _dec(r.get("current_assets")), _dec(r.get("current_liabilities")),
                _dec(r.get("receivables")), _dec(r.get("cash_and_equivalents")),
                shares,
                now_utc,
            ))

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
    candidates = sorted(BACKFILL_DIR.glob("fmp_fundamentals_*.csv"))
    return candidates[-1] if candidates else None


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("csv", nargs="?", help="path to CSV; default = newest in data/fmp_backfill/")
    p.add_argument("--dry-run", action="store_true", help="parse only, no DB writes")
    return p.parse_args(argv)


def main() -> None:  # pragma: no cover
    raise SystemExit(asyncio.run(amain(_parse_args())))


if __name__ == "__main__":
    main()
