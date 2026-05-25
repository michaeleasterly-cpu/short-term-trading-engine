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

PR-13 (2026-05-25): edge adapter — callers still pass a ticker list and
get a ticker-keyed dict back, but internally dispatches to
classification_id and reads via PricesRepo. The post-v2.2
prices_daily.classification_id is the canonical join column.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import TYPE_CHECKING

import pandas as pd

from tpcore.data.repositories import PricesRepo
from tpcore.identity.dispatcher import IdentityDispatcher

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

    Edge adapter: ticker list in, ticker-keyed dict[ticker, pd.DataFrame]
    out. Internally dispatches ticker → classification_id (TTL+LRU
    cached) and fetches via PricesRepo.get_window_batch by cid. Tickers
    absent from ticker_history resolve to None and are silently dropped
    (preserves the prior "ticker missing from DB → absent from output"
    behavior).
    """
    dispatcher = IdentityDispatcher(pool)
    repo = PricesRepo(pool)

    cid_to_ticker: dict[str, str] = {}
    for t in tickers:
        cid = await dispatcher.ticker_to_classification_id(t)
        if cid is not None:
            cid_to_ticker[cid] = t

    if not cid_to_ticker:
        return {}

    bars_by_cid = await repo.get_window_batch(list(cid_to_ticker), start, end)
    by_ticker: dict[str, list[dict]] = defaultdict(list)
    for cid, bars in bars_by_cid.items():
        ticker = cid_to_ticker[cid]
        for b in sorted(bars, key=lambda x: x.date):
            by_ticker[ticker].append(
                {
                    "date": b.date,
                    "open": float(b.open),
                    "high": float(b.high),
                    "low": float(b.low),
                    "close": float(b.close),
                    "volume": int(b.volume),
                }
            )

    out: dict[str, pd.DataFrame] = {}
    for ticker, ticker_rows in by_ticker.items():
        if len(ticker_rows) < min_bars:
            continue
        df = pd.DataFrame(ticker_rows).set_index("date").sort_index()
        out[ticker] = df
    return out
