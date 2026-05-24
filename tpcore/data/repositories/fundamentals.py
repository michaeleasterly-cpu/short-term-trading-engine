"""FundamentalsRepo — classification_id-keyed quarterly fundamentals.

Reads ``platform.fundamentals_quarterly`` by ``classification_id``.
The post-v2.2 trigger populates ``classification_id`` from
``ticker_history`` on every INSERT; the audit confirms 99.99%
coverage (14 null residuals out of 178,835 rows — long-tail
unresolved tickers, other-session's lane).

Three access modes:
- ``get_quarterly_pit(cid, as_of=None)`` — point-in-time fetch
  matching the existing ``FundamentalsCache`` semantics: latest +
  history with ``filing_date <= as_of``. Returns the same shape the
  cache returns so callers can be migrated incrementally.
- ``get_window(cid, start, end)`` — every filing in the date window.
- ``get_window_batch(cids, start, end)`` — multi-instrument with
  chunking + Supabase recovery (same pattern as
  ``PricesRepo.get_window_batch`` and ``batched_fetchers``).

Engines that adopt this stop joining on ticker_history in their
inner loops — fundamentals_quarterly.classification_id is the
canonical join column post-v2.2.
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


_FUND_COLS = (
    "filing_date, period_end_date, period_label, "
    "net_income, fcf, operating_cash_flow, capex, revenue, "
    "total_assets, total_liabilities, current_assets, current_liabilities, "
    "receivables, cash_and_equivalents, shares_outstanding, pb, de"
)

_WINDOW_SQL = f"""
    SELECT {_FUND_COLS}
    FROM platform.fundamentals_quarterly
    WHERE classification_id = $1
      AND filing_date BETWEEN $2 AND $3
    ORDER BY filing_date
"""

_PIT_LATEST_SQL = f"""
    SELECT {_FUND_COLS}
    FROM platform.fundamentals_quarterly
    WHERE classification_id = $1
    ORDER BY filing_date DESC
"""

_PIT_AS_OF_SQL = f"""
    SELECT {_FUND_COLS}
    FROM platform.fundamentals_quarterly
    WHERE classification_id = $1
      AND filing_date <= $2
    ORDER BY filing_date DESC
"""

_BATCH_SQL = f"""
    SELECT classification_id, {_FUND_COLS}
    FROM platform.fundamentals_quarterly
    WHERE classification_id = ANY($1::text[])
      AND filing_date BETWEEN $2 AND $3
    ORDER BY classification_id, filing_date
"""

_CHUNK_SIZE = 500


class QuarterlyFundamentals(BaseModel):
    """One quarterly fundamentals row. Decimals preserve provider precision."""

    model_config = ConfigDict(frozen=True)

    filing_date: date
    period_end_date: date
    period_label: str | None
    net_income: Decimal | None
    fcf: Decimal | None
    operating_cash_flow: Decimal | None
    capex: Decimal | None
    revenue: Decimal | None
    total_assets: Decimal | None
    total_liabilities: Decimal | None
    current_assets: Decimal | None
    current_liabilities: Decimal | None
    receivables: Decimal | None
    cash_and_equivalents: Decimal | None
    shares_outstanding: Decimal | None
    pb: Decimal | None
    de: Decimal | None


class FundamentalsRepo:
    """Quarterly fundamentals by ``classification_id``."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def get_window(
        self,
        classification_id: str,
        start: date,
        end: date,
    ) -> list[QuarterlyFundamentals]:
        """Every filing in ``[start, end]`` inclusive, ascending."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(_WINDOW_SQL, classification_id, start, end)
        return [QuarterlyFundamentals.model_validate(dict(r)) for r in rows]

    async def get_quarterly_pit(
        self,
        classification_id: str,
        as_of: date | None = None,
    ) -> tuple[QuarterlyFundamentals | None, list[QuarterlyFundamentals]]:
        """Latest filing + prior history as of ``as_of``.

        Returns ``(latest, history)`` where:
          - ``latest`` is the most recent row with ``filing_date <= as_of``
            (or the overall most recent when ``as_of is None``), or
            ``None`` when no rows exist.
          - ``history`` is the strictly-earlier filings, newest first.

        Mirrors ``FundamentalsCache.get_quarterly_fundamentals`` shape
        so engines can migrate incrementally without changing their
        downstream consumption.
        """
        async with self._pool.acquire() as conn:
            if as_of is None:
                rows = await conn.fetch(_PIT_LATEST_SQL, classification_id)
            else:
                rows = await conn.fetch(_PIT_AS_OF_SQL, classification_id, as_of)
        if not rows:
            return None, []
        models = [QuarterlyFundamentals.model_validate(dict(r)) for r in rows]
        return models[0], models[1:]

    @with_supabase_recovery
    async def get_window_batch(
        self,
        classification_ids: list[str] | tuple[str, ...],
        start: date,
        end: date,
    ) -> dict[str, list[QuarterlyFundamentals]]:
        """``{cid: [filings, ...]}`` for many cids in one round-trip.

        Auto-chunks at 500 cids; chunks in parallel via
        ``asyncio.gather``. Supabase-recovery middleware retries once
        on ``QueryCanceledError`` then raises ``UniverseTooLargeError``.
        Empty result for a cid drops the key (matches PricesRepo).
        """
        if not classification_ids:
            return {}
        cids = list(classification_ids)
        chunks = [cids[i : i + _CHUNK_SIZE] for i in range(0, len(cids), _CHUNK_SIZE)]

        async def _fetch_chunk(chunk: list[str]) -> list[dict]:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(_BATCH_SQL, chunk, start, end)
            return [dict(r) for r in rows]

        chunk_results = await asyncio.gather(*(_fetch_chunk(c) for c in chunks))
        out: dict[str, list[QuarterlyFundamentals]] = {}
        for chunk_rows in chunk_results:
            for r in chunk_rows:
                cid = r.pop("classification_id")
                out.setdefault(cid, []).append(QuarterlyFundamentals.model_validate(r))
        return out

    async def funded_subset(
        self,
        classification_ids: list[str] | tuple[str, ...],
    ) -> set[str]:
        """Return the subset of cids with at least one fundamentals row.

        Engines often filter their universe to "tickers we have any
        fundamentals data for" (reversion's _funded_universe pattern).
        Single SQL — much cheaper than a per-cid existence check loop.
        Empty input returns empty set.
        """
        if not classification_ids:
            return set()
        sql = (
            "SELECT DISTINCT classification_id "
            "FROM platform.fundamentals_quarterly "
            "WHERE classification_id = ANY($1::text[])"
        )
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql, list(classification_ids))
        return {r["classification_id"] for r in rows}


__all__ = ["FundamentalsRepo", "QuarterlyFundamentals"]
