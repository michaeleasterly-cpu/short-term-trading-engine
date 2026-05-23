"""One-shot: backfill platform.ticker_classifications.country from FMP profiles.

Per v2 spec/plan §3.2 Phase 1.2 — populate country ISO2 codes for the
~13,773 rows in ticker_classifications using FMP's /stable/profile
endpoint (Alpaca's /v2/assets doesn't return country).

Strategy:
- Read every ticker from ticker_classifications WHERE country IS NULL.
- For each, fetch FMP profile (rate-limited at 300/min by default).
- Update the country column.
- Streams progress; idempotent (re-run skips already-populated rows).

Usage::

    bash scripts/run_backfill_country_from_fmp.sh
"""
from __future__ import annotations

import asyncio
import os
import time

import asyncpg
import httpx

FMP_BASE = "https://financialmodelingprep.com/stable"
RATE_LIMIT_SLEEP_S = 0.2  # ~300/min; FMP Starter ceiling
CONCURRENCY = 4
BACKOFF_S = 5.0


async def fetch_country(client: httpx.AsyncClient, symbol: str) -> str | None:
    for attempt in range(3):
        try:
            r = await client.get(f"{FMP_BASE}/profile", params={"symbol": symbol})
            if r.status_code == 429:
                await asyncio.sleep(BACKOFF_S * (attempt + 1))
                continue
            r.raise_for_status()
            data = r.json()
            if isinstance(data, list) and data:
                c = data[0].get("country")
                return c[:2].upper() if c else None
            return None
        except httpx.HTTPError:
            if attempt == 2:
                return None
            await asyncio.sleep(BACKOFF_S)
    return None


async def update_one(
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
        await asyncio.sleep(RATE_LIMIT_SLEEP_S)
        country = await fetch_country(client, ticker)
        if country and len(country) == 2:
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE platform.ticker_classifications SET country=$2 WHERE ticker=$1",
                    ticker, country,
                )
            counters["filled"] += 1
        else:
            counters["null"] += 1
        counters["done"] += 1
        if counters["done"] % 100 == 0:
            elapsed = time.time() - start
            eta = elapsed / counters["done"] * (total - counters["done"])
            print(
                f"[{counters['done']:>5}/{total}] filled={counters['filled']:>5} null={counters['null']:>4} "
                f"elapsed={elapsed:.0f}s eta={eta:.0f}s",
                flush=True,
            )


async def main() -> None:
    db_url = os.environ["DATABASE_URL_IPV4"]
    api_key = os.environ["FMP_API_KEY"]
    pool = await asyncpg.create_pool(db_url, min_size=2, max_size=4, command_timeout=60)
    try:
        rows = await pool.fetch(
            "SELECT ticker FROM platform.ticker_classifications WHERE country IS NULL ORDER BY ticker"
        )
        tickers = [r["ticker"] for r in rows]
        total = len(tickers)
        print(f"=== Backfilling country for {total} tickers via FMP /stable/profile ===", flush=True)
        if total == 0:
            print("All tickers already have country populated.", flush=True)
            return
        est = total * RATE_LIMIT_SLEEP_S / CONCURRENCY / 60
        print(f"Est. wall time: {est:.1f}min (rate-limit-sleep {RATE_LIMIT_SLEEP_S}s × concurrency {CONCURRENCY})", flush=True)

        start = time.time()
        sem = asyncio.Semaphore(CONCURRENCY)
        counters = {"filled": 0, "null": 0, "done": 0}
        async with httpx.AsyncClient(params={"apikey": api_key}, timeout=30.0) as client:
            tasks = [
                update_one(sem, client, pool, t, i+1, total, start, counters)
                for i, t in enumerate(tickers)
            ]
            await asyncio.gather(*tasks)
        elapsed = time.time() - start
        print(
            f"\n=== DONE: filled={counters['filled']:,} null={counters['null']:,} in {elapsed:.0f}s ===",
            flush=True,
        )
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
