"""Bootstrap script: download daily bars for the entire US equity universe from Alpaca.

Run once to populate ``platform.prices_daily``. Later wired up as a daily cron.

* Uses Alpaca **free tier** REST API (IEX feed for free historical bars).
* Active universe via ``GET /v2/assets?status=active&class=us_equity``.
* Inactive universe via ``GET /v2/assets?status=inactive&class=us_equity``;
  for each, pulls bars covering its trading life and marks ``delisted=True``.
* Rate-limited (Alpaca free tier ≈ 200 req/min).
* All timestamps stored as UTC; ``date`` is the bar's session date.

This file deliberately depends only on ``httpx`` and ``tpcore`` — it does
*not* import the ``alpaca_trade_api`` SDK (which is blocked by
``check_imports``). The newer ``alpaca-py`` package would also work but we
prefer raw HTTP here so the bootstrap script has no SDK churn risk.
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING

import httpx
import structlog

from tpcore.db import build_asyncpg_pool

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)

_ALPACA_BROKER_BASE = "https://api.alpaca.markets"
_ALPACA_DATA_BASE = "https://data.alpaca.markets"
_RATE_LIMIT_SLEEP_SEC = 0.35  # ~170 rpm, safely under 200 rpm cap


@dataclass
class _AssetRecord:
    symbol: str
    status: str  # "active" | "inactive"
    delisting_date: date | None  # populated for inactive
    exchange: str


def _alpaca_headers() -> dict[str, str]:
    """Build auth headers from the env. Accepts both naming conventions
    (``ALPACA_KEY``/``ALPACA_SECRET`` is what `.env` uses today)."""
    api_key = os.environ.get("ALPACA_KEY") or os.environ["ALPACA_API_KEY"]
    api_secret = os.environ.get("ALPACA_SECRET") or os.environ["ALPACA_API_SECRET"]
    return {"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": api_secret}


async def _list_assets(client: httpx.AsyncClient, status: str) -> list[_AssetRecord]:
    """List active or inactive US equities."""
    url = f"{_ALPACA_BROKER_BASE}/v2/assets"
    params = {"status": status, "class": "us_equity"}
    resp = await client.get(url, params=params)
    resp.raise_for_status()
    out: list[_AssetRecord] = []
    for row in resp.json():
        # NOTE: Alpaca does not expose delisting_date on the assets endpoint.
        # We treat inactive symbols as delisted and infer the delisting date
        # from the last bar returned by the data endpoint. TODO: capture it.
        out.append(
            _AssetRecord(
                symbol=row["symbol"],
                status=row["status"],
                delisting_date=None,
                exchange=row.get("exchange", ""),
            )
        )
    return out


async def fetch_daily_bars(
    client: httpx.AsyncClient,
    symbol: str,
    start: date,
    end: date,
    *,
    feed: str = "iex",
    adjustment: str = "all",
) -> list[dict]:
    """Page through ``/v2/stocks/{symbol}/bars`` for ``timeframe=1Day``.

    ``adjustment="all"`` returns split- and dividend-adjusted prices, which
    is what backtests want. Set to ``"raw"`` if you need to compare with
    intraday quotes.
    """
    url = f"{_ALPACA_DATA_BASE}/v2/stocks/{symbol}/bars"
    params: dict[str, str] = {
        "timeframe": "1Day",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "feed": feed,
        "limit": "10000",
        "adjustment": adjustment,
    }
    bars: list[dict] = []
    while True:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        body = resp.json()
        page = body.get("bars") or []
        bars.extend(page)
        next_token = body.get("next_page_token")
        if not next_token:
            break
        params["page_token"] = next_token
        await asyncio.sleep(_RATE_LIMIT_SLEEP_SEC)
    return bars


async def _upsert_bars(
    pool: "asyncpg.Pool",
    symbol: str,
    bars: list[dict],
    delisted: bool,
    delisting_date: date | None = None,
) -> int:
    """Insert/update bars in ``platform.prices_daily``. Returns rows written.

    Idempotent via the ``(ticker, date)`` primary key + ``ON CONFLICT DO
    UPDATE``. Adjusted_close mirrors close — we ingest with
    ``adjustment="all"``, so the returned prices are already adjusted; the
    column is kept distinct for forward compatibility with a future raw+adj
    dual ingestion.
    """
    if not bars:
        return 0
    sql = """
        INSERT INTO platform.prices_daily (
            ticker, date, open, high, low, close, volume,
            adjusted_close, delisted, delisting_date
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        ON CONFLICT (ticker, date) DO UPDATE SET
            open = EXCLUDED.open,
            high = EXCLUDED.high,
            low = EXCLUDED.low,
            close = EXCLUDED.close,
            volume = EXCLUDED.volume,
            adjusted_close = EXCLUDED.adjusted_close,
            delisted = EXCLUDED.delisted,
            delisting_date = EXCLUDED.delisting_date
    """
    rows: list[tuple] = []
    for b in bars:
        # Alpaca timestamps are RFC3339 like "2018-01-02T05:00:00Z" (midnight ET).
        # Taking the UTC date yields the NYSE session date for daily bars.
        ts_str = b["t"]
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        session_date = ts.date()
        close = b.get("c")
        rows.append(
            (
                symbol,
                session_date,
                b.get("o"),
                b.get("h"),
                b.get("l"),
                close,
                int(b.get("v") or 0),
                close,  # adjusted_close — same as close because adjustment=all
                delisted,
                delisting_date,
            )
        )
    async with pool.acquire() as conn:
        await conn.executemany(sql, rows)
    return len(rows)


async def run() -> None:
    """Ingest the entire active+inactive US equity universe.

    For a focused subset (e.g. the backtest universe), use
    ``scripts/backfill_backtest_universe.py`` which only takes ~1 minute
    of API time vs. the hours this full sweep would consume.
    """
    headers = _alpaca_headers()
    pool = await build_asyncpg_pool(os.environ["DATABASE_URL"])

    end = date.today()
    start = date(2016, 1, 1)  # Alpaca IEX free history cutoff

    try:
        async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
            active = await _list_assets(client, "active")
            inactive = await _list_assets(client, "inactive")
            logger.info("alpaca.assets", n_active=len(active), n_inactive=len(inactive))

            for asset in active + inactive:
                try:
                    bars = await fetch_daily_bars(client, asset.symbol, start, end)
                except httpx.HTTPStatusError as exc:  # pragma: no cover - operational
                    logger.warning(
                        "alpaca.bars.failed",
                        symbol=asset.symbol,
                        status=exc.response.status_code,
                    )
                    await asyncio.sleep(_RATE_LIMIT_SLEEP_SEC)
                    continue
                inserted = await _upsert_bars(
                    pool,
                    asset.symbol,
                    bars,
                    delisted=(asset.status == "inactive"),
                )
                logger.info("alpaca.upsert", symbol=asset.symbol, rows=inserted)
                await asyncio.sleep(_RATE_LIMIT_SLEEP_SEC)
    finally:
        await pool.close()


def main() -> None:  # pragma: no cover
    asyncio.run(run())


__all__ = ["fetch_daily_bars", "_alpaca_headers", "_upsert_bars", "main", "run"]


if __name__ == "__main__":  # pragma: no cover
    main()
