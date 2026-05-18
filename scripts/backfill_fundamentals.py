"""One-shot backfill: cache FMP quarterly fundamentals for the 50-name
backtest universe into ``platform.fundamentals_quarterly``.

FMP plan reality:
    * Free tier silently caps ``limit`` at 5 (~1.25 years of quarters)
      and ignores ``from`` / ``to`` filters.
    * Starter and above honor the adapter's ``DEFAULT_LIMIT`` (40 quarters
      ≈ 10 years). ``--start`` / ``--end`` are still mostly cosmetic; the
      adapter pulls the most-recent ``DEFAULT_LIMIT`` quarters and uses
      ``end`` only as the PIT cutoff.

Usage::

    python scripts/backfill_fundamentals.py
    python scripts/backfill_fundamentals.py --universe AAPL,MSFT
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import date

from tpcore.db import build_asyncpg_pool
from tpcore.fmp import FMPFundamentalsAdapter
from tpcore.fundamentals.cache import FundamentalsCache
from tpcore.outage import DataProviderOutage

logger = logging.getLogger("scripts.backfill_fundamentals")

DEFAULT_UNIVERSE: tuple[str, ...] = (
    "SPY", "QQQ", "IWM",
    "AAPL", "MSFT", "AMZN", "GOOGL", "META", "TSLA", "NVDA",
    "JPM", "V", "WMT", "DIS", "NFLX", "BA", "CAT", "GE", "GM", "F",
    "XOM", "CVX", "PFE", "JNJ", "MRK", "ABBV", "PG", "KO", "PEP",
    "MCD", "SBUX", "HD", "LOW", "TGT", "COST",
    "LMT", "RTX", "NOC", "GD",
    "SO", "DUK", "NEE",
    "PLTR", "UBER", "ABNB", "SNAP", "RBLX", "RIVN", "LCID", "FSLR",
)
INTER_SYMBOL_SLEEP_SEC = 1.0  # FMP free tier rate-limit cushion.


async def amain(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print(
            "DATABASE_URL not set. Locally: export DATABASE_URL=$DATABASE_URL_IPV4 "
            "(see project memory on Supabase's dual URL setup).",
            file=sys.stderr,
        )
        return 2

    pool = await build_asyncpg_pool(db_url)
    try:
        async with FMPFundamentalsAdapter() as adapter:
            cache = FundamentalsCache(pool, adapter=adapter)
            if args.all_active:
                # One-shot, hours-long backfill across the full active
                # prices_daily universe. Delegates to cache.backfill_all
                # which already handles per-symbol pacing, no-data ETF
                # skips, and outage classification.
                total_rows, no_data, failures, skipped = await cache.backfill_all(tickers=None)
            else:
                total_rows = 0
                skipped = 0  # per-symbol path has no skip-if-fresh concept
                no_data: list[tuple[str, str]] = []
                failures: list[tuple[str, str]] = []
                for i, symbol in enumerate(args.universe, start=1):
                    try:
                        n = await cache.backfill(symbol, start_date=args.start, end_date=args.end)
                    except DataProviderOutage as exc:
                        failures.append((symbol, str(exc)[:160]))
                        logger.warning("fundamentals.backfill_failed symbol=%s error=%s", symbol, exc)
                        await asyncio.sleep(INTER_SYMBOL_SLEEP_SEC)
                        continue
                    total_rows += n
                    logger.info("[%d/%d] %s rows=%d", i, len(args.universe), symbol, n)
                    await asyncio.sleep(INTER_SYMBOL_SLEEP_SEC)
    finally:
        await pool.close()

    print()
    label = "active universe" if args.all_active else f"{len(args.universe)} symbols"
    print(
        f"backfill complete  scope={label}  "
        f"rows_upserted={total_rows}  skipped_fresh={skipped}"
    )
    if no_data:
        print(f"no_data ({len(no_data)}): ETFs/shells with no usable fundamentals (expected)")
    if failures:
        print(f"failures ({len(failures)}):")
        for sym, why in failures:
            print(f"  {sym}: {why}")
    return 0 if not failures else 1


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--start", type=date.fromisoformat, default=date(2018, 1, 1))
    p.add_argument("--end", type=date.fromisoformat, default=date(2025, 12, 31))
    p.add_argument(
        "--universe",
        type=lambda s: tuple(t.strip().upper() for t in s.split(",") if t.strip()),
        default=DEFAULT_UNIVERSE,
    )
    p.add_argument(
        "--all-active",
        action="store_true",
        help=(
            "Ignore --universe and back-fill every distinct ticker in "
            "platform.prices_daily (delisted=false, last 90d). Delegates to "
            "FundamentalsCache.backfill_all. ~7.7k tickers, several hours."
        ),
    )
    return p.parse_args(argv)


def main() -> None:  # pragma: no cover - CLI shim
    raise SystemExit(asyncio.run(amain(_parse_args())))


if __name__ == "__main__":
    main()
