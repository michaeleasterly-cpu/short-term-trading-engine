"""Refresh platform.tradier_options_chains from Tradier API.

One-shot: for every ticker currently in the table, fetch all expirations
+ all chains, UPSERT into platform.tradier_options_chains with current
bid/ask/last/volume/open_interest/retrieved_at.

Idempotent — re-run safely. Streams progress to stdout. Crash mid-run
keeps everything already-written.

Usage::

    set -a && source .env && set +a
    DATABASE_URL="$DATABASE_URL_IPV4" .venv/bin/python scripts/refresh_tradier_options.py

Env::
    TRADIER_PRODUCTION_TOKEN  required
    DATABASE_URL              required (asyncpg URL)
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import UTC, datetime, timedelta

import asyncpg
import httpx


TRADIER_BASE = "https://api.tradier.com"
RATE_LIMIT_SLEEP_S = 0.6  # ≈100 req/min ceiling, polite vs Tradier's 120/min market-data
BACKOFF_S = 5.0
PK_COLUMNS = ("ticker", "expiration_date", "strike", "option_type")

UPSERT_SQL = """
INSERT INTO platform.tradier_options_chains
  (ticker, expiration_date, strike, option_type, bid, ask, last, volume, open_interest, retrieved_at)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
ON CONFLICT (ticker, expiration_date, strike, option_type) DO UPDATE SET
  bid = EXCLUDED.bid,
  ask = EXCLUDED.ask,
  last = EXCLUDED.last,
  volume = EXCLUDED.volume,
  open_interest = EXCLUDED.open_interest,
  retrieved_at = EXCLUDED.retrieved_at
"""


def headers() -> dict[str, str]:
    token = os.environ.get("TRADIER_PRODUCTION_TOKEN") or os.environ.get("TRADIER_TOKEN")
    if not token:
        raise SystemExit("TRADIER_PRODUCTION_TOKEN (or TRADIER_TOKEN) not set")
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


async def fetch_with_retry(client: httpx.AsyncClient, url: str, params: dict) -> dict:
    for attempt in range(4):
        try:
            r = await client.get(url, params=params, timeout=30.0)
            if r.status_code == 429:
                print(f"  429 backoff (attempt {attempt+1})", flush=True)
                await asyncio.sleep(BACKOFF_S * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as e:
            if attempt == 3:
                raise
            print(f"  HTTP error {e} (attempt {attempt+1}), retrying...", flush=True)
            await asyncio.sleep(BACKOFF_S)
    raise RuntimeError("unreachable")


async def get_expirations(client: httpx.AsyncClient, symbol: str) -> list[str]:
    data = await fetch_with_retry(
        client,
        f"{TRADIER_BASE}/v1/markets/options/expirations",
        {"symbol": symbol, "includeAllRoots": "true"},
    )
    if not data.get("expirations") or not data["expirations"].get("date"):
        return []
    dates = data["expirations"]["date"]
    return [dates] if isinstance(dates, str) else list(dates)


async def get_chain(
    client: httpx.AsyncClient, symbol: str, expiration: str
) -> list[dict]:
    data = await fetch_with_retry(
        client,
        f"{TRADIER_BASE}/v1/markets/options/chains",
        {"symbol": symbol, "expiration": expiration},
    )
    if not data.get("options") or not data["options"].get("option"):
        return []
    options = data["options"]["option"]
    return [options] if isinstance(options, dict) else list(options)


async def refresh_ticker(
    client: httpx.AsyncClient, pool: asyncpg.Pool, ticker: str
) -> tuple[int, int]:
    """Returns (expirations_fetched, rows_upserted)."""
    expirations = await get_expirations(client, ticker)
    if not expirations:
        return 0, 0
    rows_upserted = 0
    now = datetime.now(UTC)
    for exp in expirations:
        await asyncio.sleep(RATE_LIMIT_SLEEP_S)
        try:
            options = await get_chain(client, ticker, exp)
        except Exception as e:
            print(f"  {ticker} exp={exp} chain fetch failed: {e}", flush=True)
            continue
        if not options:
            continue
        batch = []
        exp_date = datetime.fromisoformat(exp).date()
        for o in options:
            strike = o.get("strike")
            option_type = (o.get("option_type") or "").upper()
            if strike is None or option_type not in ("CALL", "PUT"):
                continue
            batch.append((
                ticker, exp_date,
                float(strike), option_type,
                float(o.get("bid") or 0),
                float(o.get("ask") or 0),
                float(o.get("last") or 0),
                int(o.get("volume") or 0),
                int(o.get("open_interest") or 0),
                now,
            ))
        if batch:
            async with pool.acquire() as conn:
                await conn.executemany(UPSERT_SQL, batch)
            rows_upserted += len(batch)
    return len(expirations), rows_upserted


async def refresh_one(
    sem: asyncio.Semaphore,
    client: httpx.AsyncClient,
    pool: asyncpg.Pool,
    ticker: str,
    idx: int,
    total: int,
    start: float,
    counters: dict,
) -> None:
    async with sem:
        t0 = time.time()
        try:
            exps, rows = await refresh_ticker(client, pool, ticker)
        except Exception as e:
            print(f"[{idx}/{total}] {ticker:<8} FAILED: {e}", flush=True)
            return
        counters["exps"] += exps
        counters["rows"] += rows
        counters["done"] += 1
        elapsed = time.time() - start
        eta = elapsed / counters["done"] * (total - counters["done"])
        print(
            f"[{counters['done']:>4}/{total}] {ticker:<8} exps={exps:>3} rows={rows:>5,} "
            f"({time.time()-t0:.1f}s) | total={counters['rows']:,} elapsed={elapsed:.0f}s eta={eta:.0f}s",
            flush=True,
        )


async def main() -> None:
    db_url = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_URL_IPV4")
    if not db_url:
        raise SystemExit("DATABASE_URL not set")
    tier_max = int(os.environ.get("TIER_MAX", "2"))  # T1+T2 by default
    concurrency = int(os.environ.get("CONCURRENCY", "3"))
    pool = await asyncpg.create_pool(db_url, min_size=2, max_size=4, command_timeout=60)
    try:
        tickers_rows = await pool.fetch("""
            SELECT lt.ticker
            FROM platform.liquidity_tiers lt
            JOIN platform.ticker_classifications tc USING(ticker)
            WHERE lt.tier <= $1 AND tc.asset_class IN ('stock','etf')
            ORDER BY lt.tier, lt.ticker
        """, tier_max)
        tickers = [r["ticker"] for r in tickers_rows]
        print(f"=== Refreshing {len(tickers)} tickers (T1..T{tier_max} stock+etf, concurrency={concurrency}) ===", flush=True)
        print(f"Est. wall time: {len(tickers) * 18 / concurrency / 60:.0f}min ({len(tickers) * 18 / concurrency / 3600:.1f}h)", flush=True)
        start = time.time()
        sem = asyncio.Semaphore(concurrency)
        counters = {"exps": 0, "rows": 0, "done": 0}
        async with httpx.AsyncClient(headers=headers()) as client:
            tasks = [
                refresh_one(sem, client, pool, t, i+1, len(tickers), start, counters)
                for i, t in enumerate(tickers)
            ]
            await asyncio.gather(*tasks)
        print(f"=== DONE: {counters['exps']} expirations, {counters['rows']:,} rows in {time.time()-start:.0f}s ===", flush=True)
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
