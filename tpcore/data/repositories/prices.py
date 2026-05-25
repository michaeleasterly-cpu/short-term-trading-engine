"""PricesRepo — classification_id-keyed daily bars from platform.prices_daily.

Replaces the ad-hoc per-engine pattern:
    SELECT ticker, date, open, high, low, close, volume
    FROM platform.prices_daily
    WHERE ticker = ANY($1) AND date BETWEEN $2 AND $3

with a classification_id-keyed repo. Engines that adopt the repo never
join through ``ticker_history`` to read bars — the post-v2.2
``prices_daily.classification_id`` column (100% populated per
2026-05-24 audit) is the canonical join key.

Two access modes:
- ``get_window(cid, start, end)`` — single instrument, returns
  ``list[Bar]`` (one Pydantic model per session).
- ``get_window_batch(cids, start, end)`` — multi-instrument with
  auto-chunking + Supabase-recovery middleware (same pattern as
  ``tpcore.data.batched_fetchers.fetch_bars_batch``), returns
  ``dict[cid, list[Bar]]``.

Bars are bounded by ``[start, end]`` inclusive. The repo does NOT
apply as-of semantics — caller decides the window. SCD-2 logic isn't
needed here because ``classification_id`` is stable across renames.
"""

from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

import structlog
from pydantic import BaseModel, ConfigDict

from tpcore.data.batched_fetchers import with_supabase_recovery

if TYPE_CHECKING:
    import asyncpg

logger = structlog.get_logger(__name__)


_BARS_BATCH_SQL = """
    SELECT classification_id, date, open, high, low, close, volume
    FROM platform.prices_daily
    WHERE classification_id = ANY($1::text[])
      AND date BETWEEN $2 AND $3
    ORDER BY classification_id, date
"""

_BARS_SINGLE_SQL = """
    SELECT date, open, high, low, close, volume
    FROM platform.prices_daily
    WHERE classification_id = $1
      AND date BETWEEN $2 AND $3
    ORDER BY date
"""

_LATEST_AT_OR_BEFORE_BATCH_SQL = """
    SELECT DISTINCT ON (classification_id)
        classification_id, date, open, high, low, close, volume
    FROM platform.prices_daily
    WHERE classification_id = ANY($1::text[])
      AND date <= $2
    ORDER BY classification_id, date DESC
"""

_CHUNK_SIZE = 500


class Bar(BaseModel):
    """One daily bar — date + OHLCV. Decimal preserves provider precision."""

    model_config = ConfigDict(frozen=True)

    date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


class PricesRepo:
    """Daily bars from ``platform.prices_daily``, keyed on classification_id."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def get_window(
        self,
        classification_id: str,
        start: date,
        end: date,
    ) -> list[Bar]:
        """Return bars for one cid in ``[start, end]`` inclusive.

        Empty list if the cid has no bars in the window (delisted,
        not-yet-issued, or genuinely missing — caller distinguishes).
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(_BARS_SINGLE_SQL, classification_id, start, end)
        return [Bar.model_validate(dict(r)) for r in rows]

    @with_supabase_recovery
    async def get_window_batch(
        self,
        classification_ids: list[str] | tuple[str, ...],
        start: date,
        end: date,
    ) -> dict[str, list[Bar]]:
        """Return ``{cid: [Bar, ...]}`` for every cid in the window.

        Auto-chunks at 500 cids — same threshold ``batched_fetchers``
        uses on the ticker path. Chunks run in parallel via
        ``asyncio.gather``; wall-clock cost ≈ one chunk's round-trip.

        Wrapped in ``with_supabase_recovery`` — one retry on
        ``QueryCanceledError``; second failure raises
        ``UniverseTooLargeError`` so the scheduler can decide.

        Empty result for a cid is dropped (key absent) rather than
        returned as ``{cid: []}`` — match caller expectations from the
        existing ``fetch_bars_batch``.
        """
        if not classification_ids:
            return {}

        cids = list(classification_ids)
        chunks = [cids[i : i + _CHUNK_SIZE] for i in range(0, len(cids), _CHUNK_SIZE)]

        async def _fetch_chunk(chunk: list[str]) -> list[dict]:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(_BARS_BATCH_SQL, chunk, start, end)
            return [dict(r) for r in rows]

        chunk_results = await asyncio.gather(*(_fetch_chunk(c) for c in chunks))
        by_cid: dict[str, list[Bar]] = {}
        for chunk_rows in chunk_results:
            for r in chunk_rows:
                cid = r["classification_id"]
                bars = by_cid.setdefault(cid, [])
                bars.append(
                    Bar(
                        date=r["date"],
                        open=r["open"],
                        high=r["high"],
                        low=r["low"],
                        close=r["close"],
                        volume=r["volume"],
                    )
                )
        return by_cid

    async def latest_at_or_before_batch(
        self,
        classification_ids: list[str] | tuple[str, ...],
        as_of: date,
    ) -> dict[str, Bar]:
        """Return ``{cid: Bar}`` — the most recent bar with ``date <= as_of``
        for each cid.

        Misses (cid with no bars at or before ``as_of``) are absent from
        the result dict. Used by schedulers that need the "latest close
        as of session date" for each instrument in a basket.

        Single SQL using Postgres ``DISTINCT ON`` — no chunking; basket
        sizes are small (sentinel basket ≈ 6 tickers).
        """
        if not classification_ids:
            return {}
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(_LATEST_AT_OR_BEFORE_BATCH_SQL, list(classification_ids), as_of)
        out: dict[str, Bar] = {}
        for r in rows:
            cid = r["classification_id"]
            out[cid] = Bar(
                date=r["date"],
                open=r["open"],
                high=r["high"],
                low=r["low"],
                close=r["close"],
                volume=r["volume"],
            )
        return out


__all__ = ["Bar", "PricesRepo"]
