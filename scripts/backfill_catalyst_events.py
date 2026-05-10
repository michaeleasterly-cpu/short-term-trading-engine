"""Backfill ``platform.catalyst_events`` with FMP earnings beats.

MVP catalyst proxy for Vector's Gate 2: an earnings report where
``actual_eps > estimated_eps × 1.05`` becomes an EARNINGS_BEAT row with
``magnitude_pct = (actual − estimated) / estimated``. The full catalyst
NLP pipeline (contract awards, raised guidance, etc.) is Phase 3.

Run::

    python scripts/backfill_catalyst_events.py
    python scripts/backfill_catalyst_events.py --start 2018-01-01 --end 2025-12-31

FMP endpoint (verified 2026-05-11): ``GET /stable/earnings?symbol={t}``
returns the full historical + future earnings calendar for one symbol
in a single response. We filter client-side to the requested window.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import date
from decimal import Decimal

import httpx

from tpcore.db import build_asyncpg_pool

logger = logging.getLogger("scripts.backfill_catalyst_events")

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

INTER_SYMBOL_SLEEP_S = 0.4

EARNINGS_URL = "https://financialmodelingprep.com/stable/earnings"
BEAT_THRESHOLD = Decimal("0.05")  # > 5% beat


_INSERT_SQL = """
    INSERT INTO platform.catalyst_events
        (ticker, event_date, event_type, magnitude_pct, source, recorded_at)
    VALUES ($1, $2, $3, $4, $5, now())
    ON CONFLICT (ticker, event_date, event_type) DO NOTHING
"""


async def fetch_earnings(client: httpx.AsyncClient, symbol: str, api_key: str) -> list[dict]:
    """Pull the full /stable/earnings history for one symbol."""
    try:
        resp = await client.get(EARNINGS_URL, params={"symbol": symbol, "apikey": api_key})
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("fmp.earnings_fetch_failed symbol=%s err=%s", symbol, exc)
        return []
    body = resp.json()
    return body if isinstance(body, list) else []


def _classify_beat(row: dict) -> tuple[bool, Decimal | None]:
    """True iff actual EPS > estimate × (1 + threshold). Returns (is_beat, magnitude).

    Negative-estimate beats are treated as "not a catalyst" — actual − estimate > 0
    can mean "less of a loss than expected" which doesn't carry the same momentum.
    Zero-estimate beats with a positive actual surface as a sentinel magnitude.
    """
    actual = row.get("epsActual")
    estimated = row.get("epsEstimated")
    if actual is None or estimated is None:
        return False, None
    try:
        a = Decimal(str(actual))
        e = Decimal(str(estimated))
    except Exception:
        return False, None
    if e == 0:
        if a > 0:
            return True, Decimal("999999")
        return False, None
    if e <= 0:
        return False, None
    pct = (a - e) / e
    if pct > BEAT_THRESHOLD:
        return True, pct.quantize(Decimal("0.000001"))
    return False, None


async def amain(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    api_key = os.getenv("FMP_API_KEY")
    if not api_key:
        print("FMP_API_KEY not set", file=sys.stderr)
        return 2
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL not set", file=sys.stderr)
        return 2

    universe = args.universe or DEFAULT_UNIVERSE
    pool = await build_asyncpg_pool(db_url)
    inserted_total = 0
    skipped_no_data: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            for i, symbol in enumerate(universe, 1):
                rows = await fetch_earnings(client, symbol, api_key)
                if not rows:
                    skipped_no_data.append(symbol)
                    await asyncio.sleep(INTER_SYMBOL_SLEEP_S)
                    continue

                beats: list[tuple] = []
                for r in rows:
                    raw_date = r.get("date")
                    if not raw_date:
                        continue
                    try:
                        ev_date = date.fromisoformat(raw_date)
                    except ValueError:
                        continue
                    if ev_date < args.start or ev_date > args.end:
                        continue
                    is_beat, magnitude = _classify_beat(r)
                    if not is_beat:
                        continue
                    beats.append((symbol, ev_date, "EARNINGS_BEAT", magnitude, "fmp"))

                if beats:
                    async with pool.acquire() as conn:
                        await conn.executemany(_INSERT_SQL, beats)
                    inserted_total += len(beats)
                logger.info(
                    "[%d/%d] %s earnings_rows=%d beats_in_window=%d",
                    i, len(universe), symbol, len(rows), len(beats),
                )
                await asyncio.sleep(INTER_SYMBOL_SLEEP_S)

        async with pool.acquire() as conn:
            r = await conn.fetchrow(
                "SELECT COUNT(*) AS n, COUNT(DISTINCT ticker) AS t, "
                "MIN(event_date) AS mn, MAX(event_date) AS mx "
                "FROM platform.catalyst_events WHERE event_type='EARNINGS_BEAT'"
            )
    finally:
        await pool.close()

    print(f"\nbackfill complete  symbols={len(universe)}  beats_inserted_this_run={inserted_total}")
    print(
        f"catalyst_events EARNINGS_BEAT total: {r['n']:,} rows / "
        f"{r['t']} tickers / span {r['mn']}..{r['mx']}"
    )
    if skipped_no_data:
        print(f"no FMP data for: {skipped_no_data}")
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--start", type=date.fromisoformat, default=date(2018, 1, 1))
    p.add_argument("--end", type=date.fromisoformat, default=date(2025, 12, 31))
    p.add_argument(
        "--universe",
        type=lambda s: tuple(t.strip().upper() for t in s.split(",") if t.strip()),
        default=None,
    )
    return p.parse_args(argv)


def main() -> None:  # pragma: no cover
    raise SystemExit(asyncio.run(amain(_parse_args())))


if __name__ == "__main__":
    main()
