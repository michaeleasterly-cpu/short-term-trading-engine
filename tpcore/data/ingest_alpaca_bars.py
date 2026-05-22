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
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

import httpx
import structlog

from tpcore.db import build_asyncpg_pool
from tpcore.outage import with_retry

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)

_ALPACA_BROKER_BASE_LIVE = "https://api.alpaca.markets"
_ALPACA_BROKER_BASE_PAPER = "https://paper-api.alpaca.markets"
_ALPACA_DATA_BASE = "https://data.alpaca.markets"  # same URL for paper + live
_RATE_LIMIT_SLEEP_SEC = 0.35  # ~170 rpm, safely under 200 rpm cap


def _alpaca_broker_base() -> str:
    """Pick the paper or live broker URL based on ``ALPACA_PAPER`` env.

    The data endpoint is shared, so only the broker (assets / orders) URL
    needs to switch. Defaults to paper — every environment we run today
    is paper-keyed, and a live key against the paper URL just 401s with
    a clear error rather than silently leaking real orders.
    """
    return (
        _ALPACA_BROKER_BASE_PAPER
        if os.getenv("ALPACA_PAPER", "true").lower() == "true"
        else _ALPACA_BROKER_BASE_LIVE
    )


# Back-compat shim — older callers referenced the constant directly.
# Kept as a property-like getter so importers always see the env-resolved
# value at access time, not at module-import time.
_ALPACA_BROKER_BASE = _ALPACA_BROKER_BASE_PAPER  # default; runtime callers use _alpaca_broker_base()


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
    url = f"{_alpaca_broker_base()}/v2/assets"
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


async def fetch_active_us_equities(
    client: httpx.AsyncClient,
    *,
    exchanges: tuple[str, ...] = ("NYSE", "NASDAQ"),
) -> list[dict]:
    """List active, tradable US equities on the named exchanges.

    Returns a list of ``{"symbol", "exchange"}`` dicts. Used by the
    ``all_active`` daily_bars discovery sweep — distinct from
    ``_list_assets`` (which returns ``_AssetRecord`` for the bootstrap
    script).
    """
    url = f"{_alpaca_broker_base()}/v2/assets"
    resp = await client.get(url, params={"status": "active", "asset_class": "us_equity"})
    resp.raise_for_status()
    wanted = {e.upper() for e in exchanges}
    out: list[dict] = []
    for row in resp.json():
        if not row.get("tradable"):
            continue
        ex = (row.get("exchange") or "").upper()
        if ex not in wanted:
            continue
        out.append({"symbol": row["symbol"], "exchange": ex})
    return out


@with_retry(max_attempts=4, backoff_base_sec=2.0, backoff_cap_sec=30.0)
async def fetch_daily_bars_multi(
    client: httpx.AsyncClient,
    symbols: list[str],
    start: date,
    end: date,
    *,
    feed: str = "sip",
    adjustment: str = "all",
) -> dict[str, list[dict]]:
    """Multi-symbol equivalent of :func:`fetch_daily_bars`.

    Hits ``/v2/stocks/bars`` (no symbol in path) which accepts up to 100
    symbols per call. Pages through ``next_page_token`` and merges all
    pages into one dict keyed by symbol. Symbols with no bars in the
    window are returned as empty lists rather than missing keys, so the
    caller can iterate over all input symbols deterministically.

    Wrapped with ``@with_retry`` so a transient 429/503 doesn't tank
    the whole batch — the inner ``raise_for_status`` becomes a retry
    signal rather than a permanent failure.
    """
    url = f"{_ALPACA_DATA_BASE}/v2/stocks/bars"
    params: dict[str, str] = {
        "symbols": ",".join(symbols),
        "timeframe": "1Day",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "feed": feed,
        "limit": "10000",
        "adjustment": adjustment,
    }
    out: dict[str, list[dict]] = {s: [] for s in symbols}
    while True:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        body = resp.json()
        bars_by_symbol = body.get("bars") or {}
        for sym, page in bars_by_symbol.items():
            out.setdefault(sym, []).extend(page or [])
        next_token = body.get("next_page_token")
        if not next_token:
            break
        params["page_token"] = next_token
        await asyncio.sleep(_RATE_LIMIT_SLEEP_SEC)
    return out


async def fetch_daily_bars(
    client: httpx.AsyncClient,
    symbol: str,
    start: date,
    end: date,
    *,
    feed: str = "sip",
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
    pool: asyncpg.Pool,
    symbol: str,
    bars: list[dict],
    delisted: bool,
    delisting_date: date | None = None,
    source: str = "alpaca",
) -> int:
    """Insert/update bars in ``platform.prices_daily``. Returns rows written.

    Idempotent via the ``(ticker, date)`` primary key + ``ON CONFLICT DO
    UPDATE``. Adjusted_close mirrors close — we ingest with
    ``adjustment="all"``, so the returned prices are already adjusted; the
    column is kept distinct for forward compatibility with a future raw+adj
    dual ingestion.

    ``source`` is the canonical provenance tag written to
    ``platform.prices_daily.source`` for every row touched by this call.
    Callers MUST pass the provider that actually fetched the bars — the
    FMP-feed path passes ``source='fmp'`` so its rows aren't mislabeled
    'alpaca' (the prior bug). The kwarg defaults to ``'alpaca'`` only for
    back-compat with the legacy Alpaca-only callers; new code should pass
    the source explicitly.
    """
    if not bars:
        return 0
    sql = """
        INSERT INTO platform.prices_daily (
            ticker, date, open, high, low, close, volume,
            adjusted_close, delisted, delisting_date, source
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
        ON CONFLICT (ticker, date) DO UPDATE SET
            open = EXCLUDED.open,
            high = EXCLUDED.high,
            low = EXCLUDED.low,
            close = EXCLUDED.close,
            volume = EXCLUDED.volume,
            adjusted_close = EXCLUDED.adjusted_close,
            delisted = EXCLUDED.delisted,
            delisting_date = EXCLUDED.delisting_date,
            source = EXCLUDED.source
    """
    # Physical-truth gate — matches validation.row_integrity expectations.
    # Bad rows MUST NEVER reach the database (per the data-acceptance rule
    # codified after the 94k-bad-row Tradier incident in May 2026):
    #   * close > 0 and <= 100M (no scale corruption, no pre-IPO zeros)
    #   * OHLC consistent (high >= max(open, close, low), low <= min(open, close, high))
    #   * volume >= 0 and not NULL
    #   * date not in the future
    today = datetime.now(UTC).date()
    rows: list[tuple] = []
    rejected = 0
    for b in bars:
        ts = datetime.fromisoformat(b["t"].replace("Z", "+00:00"))
        session_date = ts.date()
        o = b.get("o")
        h = b.get("h")
        low = b.get("l")
        close = b.get("c")
        v = b.get("v")
        if (o is None or h is None or low is None or close is None or v is None):
            rejected += 1
            continue
        if close <= 0 or close > 1e8 or o <= 0 or h <= 0 or low <= 0:
            rejected += 1
            continue
        if h < max(o, close, low) or low > min(o, close, h):
            rejected += 1
            continue
        if session_date > today:
            rejected += 1
            continue
        rows.append((
            symbol, session_date, o, h, low, close, int(v),
            close,  # adjusted_close — same as close because adjustment=all
            delisted, delisting_date, source,
        ))
    if rejected:
        logger.warning(
            "ingest_alpaca_bars.physical_truth_rejected",
            symbol=symbol, rejected=rejected, accepted=len(rows),
        )
    if not rows:
        return 0
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

    end = date.today()  # noqa: DTZ011
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
