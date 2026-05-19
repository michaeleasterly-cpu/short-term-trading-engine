"""Shared daily-bar loader for the per-trade backtest engines.

Lean P5.3 (#2) — consolidates the byte-identical ``_load_prices`` from
``reversion/backtest.py`` and ``vector/backtest.py``. The SQL and the
DataFrame parse are identical; the ONLY divergence is the minimum-bar
filter (reversion ``MA_50_PERIOD + 5`` == 55, vector ``SMA_200 + 5``
== 205). That divergence is **intentional** (different indicator
lookback windows) and is preserved here as the explicit, required
``min_bars`` keyword parameter — it is NOT erased or flattened.

Layering: ``tpcore`` only, zero engine imports (engine -> tpcore one-way,
enforced by ``tpcore/scripts/check_imports.py``).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import TYPE_CHECKING

import pandas as pd

if TYPE_CHECKING:  # pragma: no cover - typing only
    import asyncpg


async def load_prices(
    pool: asyncpg.Pool,
    tickers: list[str],
    start: date,
    end: date,
    *,
    min_bars: int,
) -> dict[str, pd.DataFrame]:
    """Load daily OHLCV bars for ``tickers`` between ``start`` and ``end``.

    Tickers with fewer than ``min_bars`` rows are dropped (the per-engine
    indicator-warmup floor — the sole intentional divergence, now an
    explicit caller-supplied parameter).
    """
    sql = """
        SELECT ticker, date, open, high, low, close, volume
        FROM platform.prices_daily
        WHERE ticker = ANY($1) AND date BETWEEN $2 AND $3
        ORDER BY ticker, date
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, tickers, start, end)
    by_ticker: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_ticker[r["ticker"]].append(
            {
                "date": r["date"],
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "volume": int(r["volume"]),
            }
        )
    out: dict[str, pd.DataFrame] = {}
    for ticker, ticker_rows in by_ticker.items():
        if len(ticker_rows) < min_bars:
            continue
        df = pd.DataFrame(ticker_rows).set_index("date").sort_index()
        out[ticker] = df
    return out
