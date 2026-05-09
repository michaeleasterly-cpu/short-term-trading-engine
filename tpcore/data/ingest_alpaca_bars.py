"""Bootstrap script: download daily bars for the entire US equity universe from Alpaca.

Run once to populate ``prices_daily``. Later wired up as a daily cron.

* Uses Alpaca **free tier** REST API (IEX feed for free historical bars).
* Active universe via ``GET /v2/assets?status=active&class=us_equity``.
* Inactive universe via ``GET /v2/assets?status=inactive&class=us_equity``;
  for each, pulls bars covering its trading life and marks ``delisted=True``.
* Rate-limited (Alpaca free tier ≈ 200 req/min).
* All timestamps stored as UTC; ``date`` is the bar's session date.

This file deliberately depends only on ``httpx`` and ``tpcore.interfaces.data``
— it does *not* import the ``alpaca_trade_api`` SDK (which is blocked by
``check_imports``).
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import date

import httpx
import structlog

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


async def _fetch_daily_bars(
    client: httpx.AsyncClient,
    symbol: str,
    start: date,
    end: date,
) -> list[dict]:
    """Page through ``/v2/stocks/{symbol}/bars`` for ``timeframe=1Day``."""
    url = f"{_ALPACA_DATA_BASE}/v2/stocks/{symbol}/bars"
    params: dict[str, str] = {
        "timeframe": "1Day",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "feed": "iex",
        "limit": "10000",
        "adjustment": "raw",
    }
    bars: list[dict] = []
    while True:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        body = resp.json()
        bars.extend(body.get("bars") or [])
        next_token = body.get("next_page_token")
        if not next_token:
            break
        params["page_token"] = next_token
        await asyncio.sleep(_RATE_LIMIT_SLEEP_SEC)
    return bars


async def _upsert_bars(pool, symbol: str, bars: list[dict], delisted: bool) -> int:
    """Insert/update bars in ``prices_daily``.

    TODO: implement with asyncpg. Schema target::

        prices_daily(
            ticker text,
            date date,
            open numeric, high numeric, low numeric, close numeric,
            volume bigint,
            adjusted_close numeric,
            delisted boolean,
            delisting_date date,
            PRIMARY KEY (ticker, date)
        )

    Use ``INSERT ... ON CONFLICT (ticker, date) DO UPDATE SET ...``.
    """
    _ = (pool, symbol, bars, delisted)
    raise NotImplementedError


async def run() -> None:
    api_key = os.environ["ALPACA_API_KEY"]
    api_secret = os.environ["ALPACA_API_SECRET"]
    headers = {"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": api_secret}
    pool = None  # TODO: build asyncpg pool from DATABASE_URL.

    end = date.today()
    # Alpaca IEX free history starts 2016-01-01.
    start = date(2016, 1, 1)

    async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
        active = await _list_assets(client, "active")
        inactive = await _list_assets(client, "inactive")
        logger.info("alpaca.assets", n_active=len(active), n_inactive=len(inactive))

        for asset in active + inactive:
            try:
                bars = await _fetch_daily_bars(client, asset.symbol, start, end)
            except httpx.HTTPStatusError as exc:  # pragma: no cover - operational
                logger.warning("alpaca.bars.failed", symbol=asset.symbol, status=exc.response.status_code)
                await asyncio.sleep(_RATE_LIMIT_SLEEP_SEC)
                continue
            await _upsert_bars(pool, asset.symbol, bars, delisted=(asset.status == "inactive"))
            await asyncio.sleep(_RATE_LIMIT_SLEEP_SEC)


def main() -> None:  # pragma: no cover
    asyncio.run(run())


__all__ = ["main", "run"]


if __name__ == "__main__":  # pragma: no cover
    main()
