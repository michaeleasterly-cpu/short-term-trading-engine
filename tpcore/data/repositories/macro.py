"""MacroRepo — series_id-keyed macro observations from platform.macro_data.

Replaces the ad-hoc pattern (sentinel):
    SELECT * FROM platform.macro_indicators
    WHERE indicator = $1 AND date BETWEEN $2 AND $3

with a typed repo against ``platform.macro_data`` (Task #18 SCD-2
consolidation that landed 2026-05-24). Macro data is series-keyed,
NOT classification_id-keyed — macro series have no security identity.

The series_id convention follows ``macro_data.series_id`` values
(e.g. ``'fred:VIXCLS'``, ``'aaii:bullish_pct'``, ``'cnn:fear_greed'``).
Caller passes the canonical series_id; repo does not invent one.

Note on the schema: ``platform.macro_data`` is the canonical post-P7
table; the legacy ``macro_indicators`` / ``aaii_sentiment`` /
``fear_greed`` names are now renamed-cutover (not shim views per the
2026-05-24 audit). Engines reading legacy table names should migrate
to this repo.
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


_SERIES_WINDOW_SQL = """
    SELECT observed_date, value_num, value_text, source
    FROM platform.macro_data
    WHERE series_id = $1
      AND observed_date BETWEEN $2 AND $3
    ORDER BY observed_date
"""

_LATEST_AS_OF_SQL = """
    SELECT observed_date, value_num, value_text, source
    FROM platform.macro_data
    WHERE series_id = $1
      AND observed_date <= $2
    ORDER BY observed_date DESC
    LIMIT 1
"""

_BATCH_WINDOW_SQL = """
    SELECT series_id, observed_date, value_num, value_text, source
    FROM platform.macro_data
    WHERE series_id = ANY($1::text[])
      AND observed_date BETWEEN $2 AND $3
    ORDER BY series_id, observed_date
"""

_BATCH_WINDOW_WITH_SOURCE_SQL = """
    SELECT series_id, observed_date, value_num, value_text, source
    FROM platform.macro_data
    WHERE series_id = ANY($1::text[])
      AND observed_date BETWEEN $2 AND $3
      AND source = $4
    ORDER BY series_id, observed_date
"""


class MacroObservation(BaseModel):
    """One macro observation — date + value + source.

    ``value_num`` and ``value_text`` are mutually exclusive: numeric
    series populate ``value_num`` and leave ``value_text`` NULL;
    string-valued series (rare — sentiment regime labels, etc.) do
    the opposite. Caller checks which is populated.
    """

    model_config = ConfigDict(frozen=True)

    observed_date: date
    value_num: Decimal | None
    value_text: str | None
    source: str


class MacroRepo:
    """Macro observations from ``platform.macro_data``, series_id-keyed."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def get_window(
        self,
        series_id: str,
        start: date,
        end: date,
    ) -> list[MacroObservation]:
        """Return observations for one series in ``[start, end]`` inclusive."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(_SERIES_WINDOW_SQL, series_id, start, end)
        return [MacroObservation.model_validate(dict(r)) for r in rows]

    async def get_window_batch(
        self,
        series_ids: list[str] | tuple[str, ...],
        start: date,
        end: date,
        *,
        source: str | None = None,
    ) -> dict[str, list[MacroObservation]]:
        """Return ``{series_id: [MacroObservation, ...]}`` for many series in one shot.

        Caller passes the canonical series_id list (e.g.
        ``['sahm_rule', 'industrial_production']``). ``source`` is an
        optional disambiguator — only matters when the same series_id
        could exist across providers (rare; today none collide). When
        a series has no observations in the window, its key is omitted
        from the result (NOT returned as an empty list) — same shape
        convention as ``PricesRepo.get_window_batch``.

        Args:
            series_ids: list of canonical series identifiers.
            start: inclusive lower bound on ``observed_date``.
            end: inclusive upper bound on ``observed_date``.
            source: optional provider filter (``'fred'``, ``'aaii'``,
                ``'cnn_fear_greed'``). When ``None`` (the default),
                rows from all sources are returned.
        """
        if not series_ids:
            return {}
        ids = list(series_ids)
        async with self._pool.acquire() as conn:
            if source is None:
                rows = await conn.fetch(_BATCH_WINDOW_SQL, ids, start, end)
            else:
                rows = await conn.fetch(_BATCH_WINDOW_WITH_SOURCE_SQL, ids, start, end, source)
        out: dict[str, list[MacroObservation]] = {}
        for r in rows:
            series_id = r["series_id"]
            out.setdefault(series_id, []).append(
                MacroObservation(
                    observed_date=r["observed_date"],
                    value_num=r["value_num"],
                    value_text=r["value_text"],
                    source=r["source"],
                )
            )
        return out

    async def get_latest_as_of(
        self,
        series_id: str,
        as_of: date,
    ) -> MacroObservation | None:
        """Most recent observation on or before ``as_of``.

        Returns ``None`` if the series has no observation at or before
        ``as_of`` (data starts later, or the series doesn't exist).
        Use this when an engine needs "the latest value as the market
        knew it on date X" — the standard PIT semantics for macro.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(_LATEST_AS_OF_SQL, series_id, as_of)
        if row is None:
            return None
        return MacroObservation.model_validate(dict(row))


__all__ = ["MacroObservation", "MacroRepo"]
