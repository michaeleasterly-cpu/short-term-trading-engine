"""EarningsRepo — classification_id-keyed earnings events.

Reads ``platform.earnings_events`` (event_date, event_type,
magnitude_pct, source) by ``classification_id``. The post-v2.2
trigger populates classification_id at insert time.

Engines that consume earnings:
- ``vector`` (backtest.py) — EARNINGS_BEAT filter with magnitude_pct>0
- ``catalyst`` (backtest.py) — EARNINGS_BEAT in positive_beat_30d arm
- ``momentum`` (backtest.py) — same beat filter

The ``get_beats`` convenience method serves all three callsites
(``WHERE event_type='EARNINGS_BEAT' AND magnitude_pct > 0``).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

import structlog
from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    import asyncpg

logger = structlog.get_logger(__name__)


_EVENT_COLS = "event_date, event_type, magnitude_pct, source"

_WINDOW_SQL = f"""
    SELECT {_EVENT_COLS}
    FROM platform.earnings_events
    WHERE classification_id = $1
      AND event_date BETWEEN $2 AND $3
    ORDER BY event_date
"""

_BATCH_SQL = f"""
    SELECT classification_id, {_EVENT_COLS}
    FROM platform.earnings_events
    WHERE classification_id = ANY($1::text[])
      AND event_date BETWEEN $2 AND $3
    ORDER BY classification_id, event_date
"""

_BEATS_BATCH_SQL = f"""
    SELECT classification_id, {_EVENT_COLS}
    FROM platform.earnings_events
    WHERE classification_id = ANY($1::text[])
      AND event_date BETWEEN $2 AND $3
      AND event_type = 'EARNINGS_BEAT'
      AND magnitude_pct > 0
    ORDER BY classification_id, event_date
"""


class EarningsEvent(BaseModel):
    """One earnings event row. ``magnitude_pct`` nullable for non-beat rows."""

    model_config = ConfigDict(frozen=True)

    event_date: date
    event_type: str
    magnitude_pct: Decimal | None
    source: str


class EarningsRepo:
    """Earnings events from ``platform.earnings_events``, classification_id-keyed."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def get_window(
        self,
        classification_id: str,
        start: date,
        end: date,
    ) -> list[EarningsEvent]:
        """All events for one cid in ``[start, end]`` inclusive, ascending."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(_WINDOW_SQL, classification_id, start, end)
        return [EarningsEvent.model_validate(dict(r)) for r in rows]

    async def get_window_batch(
        self,
        classification_ids: list[str] | tuple[str, ...],
        start: date,
        end: date,
    ) -> dict[str, list[EarningsEvent]]:
        """``{cid: [events, ...]}`` for many cids in one round-trip.

        No chunking — earnings_events is small (≈35k rows total per
        the 2026-05-24 audit); single-statement scan handles thousands
        of cids without timeout risk. Missing cids absent from result.
        """
        if not classification_ids:
            return {}
        cids = list(classification_ids)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(_BATCH_SQL, cids, start, end)
        out: dict[str, list[EarningsEvent]] = {}
        for r in rows:
            d = dict(r)
            cid = d.pop("classification_id")
            out.setdefault(cid, []).append(EarningsEvent.model_validate(d))
        return out

    async def get_beats(
        self,
        classification_ids: list[str] | tuple[str, ...],
        start: date,
        end: date,
    ) -> dict[str, list[EarningsEvent]]:
        """Positive EARNINGS_BEAT events only — ``magnitude_pct > 0``.

        Convenience for the three engine callsites that filter for
        positive beats: vector/backtest.py, catalyst/backtest.py
        (positive_beat_30d arm), momentum/backtest.py. Same shape
        as ``get_window_batch``; missing cids absent from result.
        """
        if not classification_ids:
            return {}
        cids = list(classification_ids)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(_BEATS_BATCH_SQL, cids, start, end)
        out: dict[str, list[EarningsEvent]] = {}
        for r in rows:
            d = dict(r)
            cid = d.pop("classification_id")
            out.setdefault(cid, []).append(EarningsEvent.model_validate(d))
        return out

    async def cids_with_event_type(self, event_type: str) -> set[str]:
        """All cids with at least one event of ``event_type``.

        E.g. ``cids_with_event_type('EARNINGS_BEAT')`` returns every
        instrument that has had at least one positive (or any) earnings
        beat. Single SQL, whole-table scan — used by vector for the
        universe-construction primitive.
        """
        sql = "SELECT DISTINCT classification_id FROM platform.earnings_events WHERE event_type = $1"
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql, event_type)
        return {r["classification_id"] for r in rows if r["classification_id"] is not None}


__all__ = ["EarningsEvent", "EarningsRepo"]
