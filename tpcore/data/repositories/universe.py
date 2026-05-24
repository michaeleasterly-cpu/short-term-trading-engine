"""Universe enumeration — keyed on classification_id, ticker is decorative.

Replaces the 7-engine ad-hoc pattern:
    SELECT ticker FROM platform.liquidity_tiers WHERE tier <= 2
    SELECT DISTINCT ticker FROM platform.fundamentals_quarterly WHERE ...
    SELECT ticker FROM platform.prices_daily WHERE ...

with a single typed repo backed by ``platform.v_universe`` (the view
landed in migration 20260524_2000). Engines call ``enumerate()``,
get back ``list[UniverseRow]`` keyed on ``classification_id``, and
carry that surrogate through all downstream queries (PricesRepo,
FundamentalsRepo, etc.) — no SCD-2 join in the hot loop.

The view's underlying join (ticker_classifications × ticker_history ×
liquidity_tiers) yields one row per ``(classification_id, ticker_history
row)``. The repo's as_of filter collapses to one row per
classification_id; without as_of, the open row (``valid_to IS NULL``)
is returned.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import structlog
from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    import asyncpg

logger = structlog.get_logger(__name__)


class UniverseRow(BaseModel):
    """One row of the universe — classification_id-keyed.

    ``ticker_at_date`` is the ticker recorded in ``ticker_history`` for
    the row's validity window. ``current_ticker`` is the latest ticker
    on ``ticker_classifications`` regardless of as_of (use for display
    when no as_of context exists). Engines should NOT use either ticker
    as a join key — that's what ``classification_id`` is for; ticker is
    purely decorative for logs and dashboard.
    """

    model_config = ConfigDict(frozen=True)

    classification_id: str
    ticker_at_date: str
    current_ticker: str | None
    asset_class: str | None
    country: str | None
    status: str | None
    liquidity_tier: int | None
    valid_from: date
    valid_to: date | None


class UniverseRepo:
    """Read-only enumeration of the trading universe.

    Args:
        pool: ``asyncpg.Pool``. Caller manages lifecycle (same pattern
            as ``FundamentalsCache`` and ``IdentityDispatcher``).
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def enumerate(
        self,
        *,
        as_of: date | None = None,
        max_liquidity_tier: int | None = None,
        asset_class: str | None = None,
        country: str | None = None,
        include_untracked_liquidity: bool = False,
    ) -> list[UniverseRow]:
        """Return universe rows matching the supplied filters.

        Args:
            as_of: ``None`` returns currently-active rows (``valid_to IS
                NULL``); a date applies SCD-2 semantics. Backtests
                crossing renames pass the row-date here.
            max_liquidity_tier: ``None`` includes all tiers; an int
                returns only rows with ``liquidity_tier <= N``. Combined
                with ``include_untracked_liquidity=False`` (the default)
                this excludes rows without a ``liquidity_tiers`` entry.
            asset_class: filter to e.g. 'stock', 'etf'. ``None`` = no filter.
            country: ISO-3166-1 alpha-2; ``None`` = no filter.
            include_untracked_liquidity: when ``max_liquidity_tier`` is
                set, controls whether rows with ``liquidity_tier IS
                NULL`` are returned. Default ``False`` mirrors the
                existing 'T1+T2 only' engine semantics — untracked
                liquidity is excluded.

        Returns:
            ``list[UniverseRow]`` ordered by ``classification_id``.
        """
        clauses: list[str] = []
        args: list[object] = []
        n = 1

        if as_of is None:
            clauses.append("valid_to IS NULL")
        else:
            args.append(as_of)
            clauses.append(f"valid_from <= ${n} AND (valid_to IS NULL OR valid_to >= ${n})")
            n += 1

        if max_liquidity_tier is not None:
            args.append(max_liquidity_tier)
            tier_clause = f"liquidity_tier <= ${n}"
            if include_untracked_liquidity:
                tier_clause = f"(liquidity_tier IS NULL OR {tier_clause})"
            clauses.append(tier_clause)
            n += 1

        if asset_class is not None:
            args.append(asset_class)
            clauses.append(f"asset_class = ${n}")
            n += 1

        if country is not None:
            args.append(country)
            clauses.append(f"country = ${n}")
            n += 1

        sql = (
            "SELECT classification_id, ticker_at_date, current_ticker, "
            "asset_class, country, status, liquidity_tier, valid_from, valid_to "
            "FROM platform.v_universe "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY classification_id"
        )
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql, *args)
        return [UniverseRow.model_validate(dict(r)) for r in rows]


__all__ = ["UniverseRepo", "UniverseRow"]
