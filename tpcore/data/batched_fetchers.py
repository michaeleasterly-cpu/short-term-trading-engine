"""Shared batched fetchers — one SQL round-trip per call, not N.

The recurring bug pattern across engines (vector, sigma, reversion):
a per-ticker ``await get_daily_bars(symbol, ...)`` loop over the full
universe. At 7,695 active tickers and ~40ms per round-trip, a single
scan costs 5 minutes of wall time; on the all_active universe a single
ticker hung on a Supabase statement timeout would tank the whole run.

This module is the single source of truth for "fetch bars for N
tickers" and "fetch fundamentals for N tickers" with:

* **One SQL** per call — ``WHERE ticker = ANY($1)`` + grouping in Python.
* **Auto-chunking** at ``_CHUNK_SIZE=500`` tickers so the planner doesn't
  degrade on huge ANY clauses joined against ``prices_daily``.
* **Recovery middleware** via ``@with_supabase_recovery`` — one retry
  on transient ``QueryCanceledError``; if it still fails, raise the
  structured ``UniverseTooLargeError`` so the scheduler can decide
  whether to shrink-and-retry or exit.

Engines call these instead of writing their own batch SQL. Vector
already retrofitted (commit 09cdb5a); sigma + reversion next.
"""

from __future__ import annotations

import asyncio
import functools
from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import structlog

from tpcore.errors import UniverseTooLargeError

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)


# ────────────────────────────────────────────────────────────────────────
# Recovery middleware
# ────────────────────────────────────────────────────────────────────────


_CHUNK_SIZE = 500


def with_supabase_recovery(fn):
    """Decorator: one retry on Supabase statement-timeout, then raise.

    The asyncpg exception module is imported lazily so this decorator
    has no hard dep at module load time. Callers see a
    ``UniverseTooLargeError`` on the second failure — never a bare
    ``QueryCanceledError`` leaking out of the batched fetcher.
    """

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        import asyncpg.exceptions

        last_exc: BaseException | None = None
        for attempt in (1, 2):
            try:
                return await fn(*args, **kwargs)
            except asyncpg.exceptions.QueryCanceledError as exc:
                last_exc = exc
                logger.warning(
                    "tpcore.batched_fetchers.recovery_retry",
                    func=fn.__name__,
                    attempt=attempt,
                    error=str(exc),
                )
                # Brief backoff to let any concurrent lock clear.
                await asyncio.sleep(1.5)
        assert last_exc is not None
        # Count tickers if the first positional arg has __len__ (the
        # common case: tickers list as first or second positional).
        ticker_count = 0
        for a in args:
            if isinstance(a, (list, tuple, set)) and a and isinstance(next(iter(a)), str):
                ticker_count = len(a)
                break
        raise UniverseTooLargeError(
            ticker_count=ticker_count, attempt=2, original=last_exc
        )

    return wrapper


# ────────────────────────────────────────────────────────────────────────
# Bars
# ────────────────────────────────────────────────────────────────────────


_BARS_SQL = """
    SELECT ticker, date, open, high, low, close, volume
    FROM platform.prices_daily
    WHERE ticker = ANY($1::text[])
      AND date BETWEEN $2 AND $3
    ORDER BY ticker, date
"""


@with_supabase_recovery
async def fetch_bars_batch(
    pool: asyncpg.Pool,
    tickers: list[str] | tuple[str, ...],
    start: date,
    end: date,
) -> dict[str, list[dict]]:
    """Return ``{ticker: [bar_dict, ...]}`` for every ticker in one shot.

    Bars are bounded by ``[start, end]`` inclusive — caller decides the
    lookback. Auto-chunks at 500 tickers to keep the planner happy on
    big universes. Each chunk runs in parallel via ``asyncio.gather``
    so the wall-clock cost is roughly one chunk's round-trip.

    Returns an empty dict if no tickers passed in. Tickers with no
    bars in the window get an empty list (key still present) so the
    caller can iterate over every input ticker deterministically.
    """
    if not tickers:
        return {}
    ticker_list = list(dict.fromkeys(tickers))  # dedupe, preserve order
    out: dict[str, list[dict]] = {t: [] for t in ticker_list}
    chunks = [
        ticker_list[i : i + _CHUNK_SIZE]
        for i in range(0, len(ticker_list), _CHUNK_SIZE)
    ]

    async def _one(chunk: list[str]) -> list[Any]:
        async with pool.acquire() as conn:
            return await conn.fetch(_BARS_SQL, chunk, start, end)

    chunk_results = await asyncio.gather(*[_one(c) for c in chunks])
    for rows in chunk_results:
        for r in rows:
            out[r["ticker"]].append({
                "date": r["date"],
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "volume": int(r["volume"]),
            })
    return out


# ────────────────────────────────────────────────────────────────────────
# Fundamentals
# ────────────────────────────────────────────────────────────────────────


_FUNDS_COLS = (
    "ticker, filing_date, period_end_date, period_label, "
    "net_income, fcf, operating_cash_flow, capex, revenue, "
    "total_assets, total_liabilities, current_assets, current_liabilities, "
    "receivables, cash_and_equivalents, shares_outstanding"
)

_FUNDS_SQL = f"""
    SELECT {_FUNDS_COLS}
    FROM platform.fundamentals_quarterly
    WHERE ticker = ANY($1::text[]) AND filing_date <= $2
    ORDER BY ticker, filing_date DESC
"""


def _row_to_fund(r: Any) -> dict[str, Any]:
    def _d(v: Any) -> Decimal | None:
        return Decimal(str(v)) if v is not None else None

    return {
        "symbol": r["ticker"],
        "period": r["period_label"],
        "period_end_date": r["period_end_date"],
        "filing_date": r["filing_date"],
        "net_income": _d(r["net_income"]),
        "revenue": _d(r["revenue"]),
        "fcf": _d(r["fcf"]),
        "operating_cash_flow": _d(r["operating_cash_flow"]),
        "capex": _d(r["capex"]),
        "total_assets": _d(r["total_assets"]),
        "total_liabilities": _d(r["total_liabilities"]),
        "current_assets": _d(r["current_assets"]),
        "current_liabilities": _d(r["current_liabilities"]),
        "receivables": _d(r["receivables"]),
        "cash_and_equivalents": _d(r["cash_and_equivalents"]),
        "shares_outstanding": _d(r["shares_outstanding"]),
    }


@with_supabase_recovery
async def fetch_fundamentals_batch(
    pool: asyncpg.Pool,
    tickers: list[str] | tuple[str, ...],
    as_of: date,
) -> dict[str, dict[str, Any] | None]:
    """Return ``{ticker: {latest_fund_row + history}}`` for every ticker.

    Same shape as ``FundamentalsCache.get_quarterly_fundamentals`` output
    (so downstream consumers don't change), but one batched SQL instead
    of N. Tickers without any PIT-eligible row map to ``None``.
    """
    if not tickers:
        return {}
    ticker_list = [t.upper() for t in dict.fromkeys(tickers)]
    chunks = [
        ticker_list[i : i + _CHUNK_SIZE]
        for i in range(0, len(ticker_list), _CHUNK_SIZE)
    ]

    async def _one(chunk: list[str]) -> list[Any]:
        async with pool.acquire() as conn:
            return await conn.fetch(_FUNDS_SQL, chunk, as_of)

    chunk_results = await asyncio.gather(*[_one(c) for c in chunks])
    by_ticker: dict[str, list[dict]] = defaultdict(list)
    for rows in chunk_results:
        for r in rows:
            by_ticker[r["ticker"]].append(_row_to_fund(r))

    out: dict[str, dict[str, Any] | None] = {}
    for t in ticker_list:
        rows = by_ticker.get(t, [])
        if not rows:
            out[t] = None
            continue
        latest = dict(rows[0])
        latest["history"] = rows[1:]
        out[t] = latest
    return out


# ────────────────────────────────────────────────────────────────────────
# PrefetchedBarsAdapter — drop-in for engine plugs that expect a
# ``get_daily_bars`` interface but want one batched read upfront.
# ────────────────────────────────────────────────────────────────────────


class PrefetchedBarsAdapter:
    """Wraps a pre-fetched ``{ticker: [bar_dict]}`` dict + exposes the
    same async ``get_daily_bars(symbol, start, end, as_of)`` interface
    the postgres adapter uses.

    The engine plugs (sigma + reversion set up_detection) loop per
    ticker and call ``await self._data.get_daily_bars(...)``. Without
    pre-fetching that's 7,695 round-trips per scan. With this adapter:
    the scheduler does ONE ``fetch_bars_batch`` call, wraps the result,
    and the plug runs over in-memory data.

    If a fallback ``data_adapter`` is provided, tickers not in the
    pre-fetched dict fall through to it — useful for SPY / VIX-proxy
    lookups that the scheduler didn't include in the batch.
    """

    def __init__(
        self,
        bars_by_ticker: dict[str, list[dict]],
        *,
        fallback: Any | None = None,
    ) -> None:
        self._bars = bars_by_ticker
        self._fallback = fallback

    async def get_daily_bars(
        self,
        symbol: str,
        start: date,
        end: date | None = None,
        as_of: date | None = None,
    ):
        """Return Bars filtered by [start, min(end, as_of)] inclusive."""
        # Import here to avoid a hard module-import dep on pydantic at
        # this file's load time.
        from datetime import UTC
        from datetime import datetime as _dt

        from tpcore.interfaces.data import Bar

        raw = self._bars.get(symbol)
        if raw is None and self._fallback is not None:
            return await self._fallback.get_daily_bars(symbol, start, end, as_of)
        if raw is None:
            return []
        upper = end if end is not None else None
        if as_of is not None and (upper is None or as_of < upper):
            upper = as_of
        out: list[Bar] = []
        for b in raw:
            d = b["date"]
            if d < start:
                continue
            if upper is not None and d > upper:
                continue
            out.append(Bar(
                symbol=symbol,
                ts=_dt.combine(d, _dt.min.time()).replace(tzinfo=UTC),
                open=Decimal(str(b["open"])),
                high=Decimal(str(b["high"])),
                low=Decimal(str(b["low"])),
                close=Decimal(str(b["close"])),
                volume=int(b["volume"]),
                adjusted_close=Decimal(str(b["close"])),
            ))
        return out


__all__ = [
    "PrefetchedBarsAdapter",
    "fetch_bars_batch",
    "fetch_fundamentals_batch",
    "with_supabase_recovery",
]
