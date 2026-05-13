"""Probe Alpaca's SIP feed for a specific ticker set + date range.

Useful when the default IEX feed returns empty bars but we want to know
if the SIP feed (paid tier) would have them — that's the signal between
"de-facto delisted" and "IEX feed gap."

Usage::

    scripts/run_probe_alpaca_feed.sh ALOV,LPCV,PAAC,XBPEW
    scripts/run_probe_alpaca_feed.sh ALOV --since 2026-04-15
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import date, timedelta

import httpx

from tpcore.data.ingest_alpaca_bars import fetch_daily_bars_multi

logger = logging.getLogger("scripts.probe_alpaca_feed")


async def amain(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    symbols = [s.strip().upper() for s in args.tickers.split(",") if s.strip()]
    if not symbols:
        print("FAILED — no tickers", file=sys.stderr)
        return 1
    start_d = date.fromisoformat(args.since) if args.since else date.today() - timedelta(days=30)
    end_d = date.today() - timedelta(days=1)

    async with httpx.AsyncClient(
        headers={
            "APCA-API-KEY-ID": os.environ.get("ALPACA_KEY", ""),
            "APCA-API-SECRET-KEY": os.environ.get("ALPACA_SECRET", ""),
        },
        timeout=30.0,
    ) as client:
        for feed in ("iex", "sip", "otc"):
            print(f"\n=== feed={feed}  symbols={symbols}  start={start_d}  end={end_d} ===")
            try:
                bars_by_sym = await fetch_daily_bars_multi(
                    client, symbols, start_d, end_d, feed=feed,
                )
            except httpx.HTTPStatusError as exc:
                print(f"  HTTP error: {exc.response.status_code} {exc.response.text[:200]}")
                continue
            for sym in symbols:
                bars = bars_by_sym.get(sym) or []
                if not bars:
                    print(f"  {sym:8s}  no bars")
                    continue
                first = bars[0]
                last = bars[-1]
                print(
                    f"  {sym:8s}  n={len(bars):3d}  "
                    f"first={first.get('t', '?')[:10]} "
                    f"last={last.get('t', '?')[:10]} "
                    f"last_close={last.get('c')}"
                )
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("tickers", help="comma-separated tickers to probe")
    p.add_argument("--since", help="ISO date (default 30d ago)")
    return p.parse_args(argv)


def main() -> None:  # pragma: no cover
    raise SystemExit(asyncio.run(amain(_parse_args())))


if __name__ == "__main__":
    main()
