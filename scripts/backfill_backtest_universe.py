"""One-shot backfill: pull 2018-01-01 → 2025-12-31 daily bars for the
50-name backtest universe and upsert into ``platform.prices_daily``.

Usage::

    python scripts/backfill_backtest_universe.py
    python scripts/backfill_backtest_universe.py --start 2020-01-01 --universe AAPL,MSFT

Reads ``ALPACA_KEY`` / ``ALPACA_SECRET`` and ``DATABASE_URL`` from the
environment (use ``DATABASE_URL_IPV4`` locally — see project memory on
Supabase's dual URL setup). Sleeps ~0.3s between symbols to stay under
the free tier's 200 rpm cap. Names that didn't exist for the full window
(IPOs after the start date — PLTR, UBER, ABNB, RBLX, RIVN, LCID) ingest
whatever Alpaca returns; partial coverage is fine, the backtest skips
warm-up windows automatically.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import date

import httpx

from tpcore.data.ingest_alpaca_bars import (
    _alpaca_headers,
    _upsert_bars,
    fetch_daily_bars,
)
from tpcore.db import build_asyncpg_pool

logger = logging.getLogger("scripts.backfill_backtest_universe")

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
INTER_SYMBOL_SLEEP_SEC = 0.3


async def amain(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print(
            "DATABASE_URL not set. Locally, export DATABASE_URL=$DATABASE_URL_IPV4 "
            "(see project memory on Supabase's dual URL setup).",
            file=sys.stderr,
        )
        return 2

    headers = _alpaca_headers()
    pool = await build_asyncpg_pool(db_url)

    total_rows = 0
    failures: list[tuple[str, str]] = []
    try:
        async with httpx.AsyncClient(headers=headers, timeout=60.0) as client:
            for i, symbol in enumerate(args.universe, start=1):
                try:
                    bars = await fetch_daily_bars(
                        client,
                        symbol,
                        args.start,
                        args.end,
                        feed=args.feed,
                        adjustment=args.adjustment,
                    )
                except httpx.HTTPStatusError as exc:
                    failures.append((symbol, f"HTTP {exc.response.status_code}"))
                    logger.warning(
                        "fetch_failed symbol=%s status=%s body=%s",
                        symbol,
                        exc.response.status_code,
                        exc.response.text[:200],
                    )
                    await asyncio.sleep(INTER_SYMBOL_SLEEP_SEC)
                    continue
                except Exception as exc:  # noqa: BLE001 - one-off backfill
                    failures.append((symbol, str(exc)[:120]))
                    logger.warning("fetch_error symbol=%s error=%s", symbol, exc)
                    await asyncio.sleep(INTER_SYMBOL_SLEEP_SEC)
                    continue

                rows = await _upsert_bars(pool, symbol, bars, delisted=False)
                total_rows += rows
                first = bars[0]["t"][:10] if bars else "—"
                last = bars[-1]["t"][:10] if bars else "—"
                logger.info(
                    "[%d/%d] %s rows=%d span=%s..%s",
                    i,
                    len(args.universe),
                    symbol,
                    rows,
                    first,
                    last,
                )
                await asyncio.sleep(INTER_SYMBOL_SLEEP_SEC)
    finally:
        await pool.close()

    print()
    print(f"backfill complete  symbols={len(args.universe)}  rows_upserted={total_rows}")
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
        help="Comma-separated tickers (default: 50-name backtest universe).",
    )
    p.add_argument("--feed", default="iex", help="Alpaca data feed (default iex / free tier).")
    p.add_argument(
        "--adjustment",
        default="all",
        choices=("raw", "split", "dividend", "all"),
        help='Price adjustment; "all" gives split- + dividend-adjusted bars.',
    )
    return p.parse_args(argv)


def main() -> None:  # pragma: no cover - CLI shim
    raise SystemExit(asyncio.run(amain(_parse_args())))


if __name__ == "__main__":
    main()
