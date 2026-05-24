"""InsiderRepo — classification_id-keyed SEC insider transactions.

Reads ``platform.insider_transactions`` (renamed from
``sec_insider_transactions`` in v2.2 phase 1) by classification_id.
The table holds Form-3/4/5 filings keyed on filing_date with
transaction type, share count, price, and aggregate value.

Engine consumer: ``catalyst`` (insider-cluster detection in both
backtest.py and scheduler.py).

Auto-chunked batch fetch — the table is large (≈647k rows post
SEC orphan resolver) and catalyst scans wide universe windows.
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


_TXN_COLS = "filing_date, insider_name, transaction_type, shares, price, value, source"

_WINDOW_SQL = f"""
    SELECT {_TXN_COLS}
    FROM platform.insider_transactions
    WHERE classification_id = $1
      AND filing_date BETWEEN $2 AND $3
    ORDER BY filing_date, insider_name
"""

_BATCH_SQL = f"""
    SELECT classification_id, {_TXN_COLS}
    FROM platform.insider_transactions
    WHERE classification_id = ANY($1::text[])
      AND filing_date BETWEEN $2 AND $3
    ORDER BY classification_id, filing_date, insider_name
"""

_CHUNK_SIZE = 500


class InsiderTransaction(BaseModel):
    """One Form-3/4/5 transaction. ``transaction_type`` is BUY/SELL/etc."""

    model_config = ConfigDict(frozen=True)

    filing_date: date
    insider_name: str
    transaction_type: str
    shares: int
    price: Decimal
    value: Decimal
    source: str


class InsiderRepo:
    """SEC insider transactions, classification_id-keyed."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def get_window(
        self,
        classification_id: str,
        start: date,
        end: date,
    ) -> list[InsiderTransaction]:
        """All transactions for one cid in ``[start, end]`` inclusive."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(_WINDOW_SQL, classification_id, start, end)
        return [InsiderTransaction.model_validate(dict(r)) for r in rows]

    @with_supabase_recovery
    async def get_window_batch(
        self,
        classification_ids: list[str] | tuple[str, ...],
        start: date,
        end: date,
    ) -> dict[str, list[InsiderTransaction]]:
        """``{cid: [transactions, ...]}`` for many cids in one round-trip.

        Auto-chunks at 500 cids; parallel via ``asyncio.gather``;
        Supabase-recovery middleware as elsewhere. Missing cids absent.
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
        out: dict[str, list[InsiderTransaction]] = {}
        for chunk_rows in chunk_results:
            for r in chunk_rows:
                cid = r.pop("classification_id")
                out.setdefault(cid, []).append(InsiderTransaction.model_validate(r))
        return out


__all__ = ["InsiderRepo", "InsiderTransaction"]
