"""Phase 1: FMP fundamentals → CSV.

Mirrors the Alpaca-bar backfill pattern: pull from FMP per ticker, filter
each row through the same physical-truth predicate the validation suite
enforces, write to ``data/fmp_backfill/fmp_fundamentals_<ts>.csv``.

Phase 2 (``load_fmp_csv.py``) re-validates each row and upserts.

Default scope: every ticker in ``platform.liquidity_tiers`` tier ≤ 2.
Override with ``--tickers``.

Re-runnable: tickers whose newest cache row is younger than
``--skip-if-fresh-hours`` (default 24h) are skipped — partial runs
resume cleanly.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from tpcore.db import build_asyncpg_pool
from tpcore.fmp import FMPFundamentalsAdapter
from tpcore.outage import DataProviderOutage

logger = logging.getLogger("scripts.backfill_fmp_csv")

OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "fmp_backfill"

CSV_COLS = (
    "ticker", "filing_date", "period_end_date", "period_label",
    "net_income", "fcf", "operating_cash_flow", "capex", "revenue",
    "total_assets", "total_liabilities", "current_assets", "current_liabilities",
    "receivables", "cash_and_equivalents", "shares_outstanding",
)


def _physical_ok(row: dict) -> bool:
    """Same predicate as ``fundamentals_integrity``: filing date present
    and not future, period_end <= filing_date, shares > 0 (or NULL)."""
    filing = row.get("filing_date")
    period_end = row.get("period_end_date")
    if filing is None:
        return False
    if filing > datetime.now(UTC).date():
        return False
    if period_end is not None and period_end > filing:
        return False
    shares = row.get("shares_outstanding")
    if shares is not None and shares <= 0:
        return False
    return True


def _flatten(period: dict, symbol: str) -> dict:
    """Convert one FMP adapter period dict to the CSV row shape."""
    return {
        "ticker": symbol.upper(),
        "filing_date": period.get("filing_date"),
        "period_end_date": period.get("period_end_date") or period.get("filing_date"),
        "period_label": period.get("period"),
        "net_income": period.get("net_income"),
        "fcf": period.get("fcf"),
        "operating_cash_flow": period.get("operating_cash_flow"),
        "capex": period.get("capex"),
        "revenue": period.get("revenue"),
        "total_assets": period.get("total_assets"),
        "total_liabilities": period.get("total_liabilities"),
        "current_assets": period.get("current_assets"),
        "current_liabilities": period.get("current_liabilities"),
        "receivables": period.get("receivables"),
        "cash_and_equivalents": period.get("cash_and_equivalents"),
        "shares_outstanding": period.get("shares_outstanding"),
    }


async def _list_universe(pool, tickers_override: list[str] | None) -> list[str]:
    if tickers_override:
        return sorted({t.strip().upper() for t in tickers_override if t.strip()})
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT ticker FROM platform.liquidity_tiers WHERE tier <= 2 ORDER BY ticker"
        )
    return [r["ticker"] for r in rows]


async def _already_fresh(pool, tickers: list[str], hours: float) -> set[str]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT ticker
            FROM platform.fundamentals_quarterly
            WHERE ticker = ANY($1::text[])
            GROUP BY ticker
            HAVING MAX(recorded_at) > now() - ($2::float * INTERVAL '1 hour')
            """,
            [t.upper() for t in tickers], hours,
        )
    return {r["ticker"] for r in rows}


async def amain(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db_url = os.getenv("DATABASE_URL") or os.getenv("DATABASE_URL_IPV4")
    if not db_url:
        print("FAILED — DATABASE_URL not set", file=sys.stderr)
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    out_path = OUT_DIR / f"fmp_fundamentals_{ts}.csv"
    logger.info("writing to %s", out_path)

    pool = await build_asyncpg_pool(db_url, max_size=2)
    try:
        tickers_override = [t.strip() for t in args.tickers.split(",") if t.strip()] if args.tickers else None
        tickers = await _list_universe(pool, tickers_override)
        if not tickers:
            logger.error("no tickers to process")
            return 1
        logger.info("%d tickers", len(tickers))

        already_fresh: set[str] = set()
        if args.skip_if_fresh_hours is not None and not tickers_override:
            already_fresh = await _already_fresh(pool, tickers, args.skip_if_fresh_hours)
            logger.info("skipping %d tickers refreshed in last %.0fh", len(already_fresh), args.skip_if_fresh_hours)
    finally:
        await pool.close()

    kept = dropped = no_data = failed = 0
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_COLS)
        w.writeheader()
        async with FMPFundamentalsAdapter() as adapter:
            for i, symbol in enumerate(tickers, start=1):
                if symbol.upper() in already_fresh:
                    continue
                try:
                    payload = await adapter.get_quarterly_fundamentals(symbol)
                except DataProviderOutage as exc:
                    msg = str(exc)
                    if "no usable fundamentals" in msg:
                        no_data += 1
                    else:
                        failed += 1
                        logger.warning("fmp.failed symbol=%s err=%s", symbol, msg[:120])
                    continue
                periods = [{k: v for k, v in payload.items() if k != "history"}]
                periods.extend(payload.get("history") or [])
                for p in periods:
                    row = _flatten(p, symbol)
                    if not _physical_ok(row):
                        dropped += 1
                        continue
                    w.writerow(row)
                    kept += 1
                if i % 100 == 0:
                    logger.info("progress %d/%d kept=%d dropped=%d no_data=%d failed=%d", i, len(tickers), kept, dropped, no_data, failed)
                fh.flush()
                # courtesy delay — FMP Starter handles 0.3s comfortably
                await asyncio.sleep(args.inter_symbol_sleep_sec)

    logger.info("done: kept=%d dropped=%d no_data=%d failed=%d -> %s", kept, dropped, no_data, failed, out_path)
    print(out_path)
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--tickers", help="comma-separated tickers; default = liquidity_tiers tier ≤ 2")
    p.add_argument("--skip-if-fresh-hours", type=float, default=24.0, help="skip tickers refreshed within N hours; default 24")
    p.add_argument("--inter-symbol-sleep-sec", type=float, default=0.3, help="courtesy delay between FMP symbols; default 0.3s")
    return p.parse_args(argv)


def main() -> None:  # pragma: no cover
    raise SystemExit(asyncio.run(amain(_parse_args())))


if __name__ == "__main__":
    main()
