"""Daily Universe Pre-Screener — populate ``platform.universe_candidates``.

For each engine that opts in, joins ``platform.liquidity_tiers`` against
the most recent close in ``platform.prices_daily``, applies the engine's
own tradability filter, and upserts one row per (as_of_date, engine,
ticker) into ``platform.universe_candidates``.

V1 ships only the momentum populator. Sigma/Reversion/Vector keep their
hardcoded universes until they need this.

Run order
---------
Must run AFTER the daily prices_daily ingest and AFTER the weekly tier
refresh has completed — both are sources of truth for the prescreener.
The wrapper script ``scripts/run_prescreener.sh`` enforces the IPv4
pooler URL (see memory: project_supabase_dual_db_urls.md).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

import structlog

from momentum.models import MAX_TIER_FOR_TRADING, is_tradeable_common_stock

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)


_FETCH_MOMENTUM_CANDIDATES_SQL = """
    WITH latest_close AS (
        SELECT DISTINCT ON (ticker) ticker, date AS close_date, close
        FROM platform.prices_daily
        WHERE date <= $1
        ORDER BY ticker, date DESC
    )
    SELECT lt.ticker,
           lt.tier,
           lc.close       AS last_close,
           lc.close_date  AS close_date
    FROM platform.liquidity_tiers lt
    LEFT JOIN latest_close lc ON lc.ticker = lt.ticker
    WHERE lt.tier <= $2
    ORDER BY lt.ticker
"""

_UPSERT_SQL = """
    INSERT INTO platform.universe_candidates
        (as_of_date, engine, ticker, tier, last_close, reason)
    VALUES ($1, $2, $3, $4, $5, $6)
    ON CONFLICT (as_of_date, engine, ticker) DO UPDATE SET
        tier       = EXCLUDED.tier,
        last_close = EXCLUDED.last_close,
        reason     = EXCLUDED.reason
"""


async def prescreen_momentum(
    pool: asyncpg.Pool,
    as_of: date,
    *,
    max_tier: int = MAX_TIER_FOR_TRADING,
) -> dict[str, int]:
    """Populate ``universe_candidates`` for ``engine='momentum'`` at ``as_of``.

    Returns a counters dict for logging:
    ``{"considered": N, "kept": K, "dropped_no_close": X, "dropped_untradeable": Y}``.

    The function is idempotent — re-running for the same ``as_of`` updates
    rows in place. It does **not** delete rows from prior dates; those are
    intentionally kept as a historical roster.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            _FETCH_MOMENTUM_CANDIDATES_SQL,
            as_of,
            max_tier,
        )

    considered = len(rows)
    kept: list[tuple] = []
    dropped_no_close = 0
    dropped_untradeable = 0
    for r in rows:
        ticker = r["ticker"]
        tier = int(r["tier"])
        last_close = r["last_close"]
        if last_close is None:
            dropped_no_close += 1
            continue
        last_close_dec = Decimal(str(last_close))
        if not is_tradeable_common_stock(ticker, last_close_dec):
            dropped_untradeable += 1
            continue
        kept.append((as_of, "momentum", ticker, tier, last_close_dec, None))

    if kept:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.executemany(_UPSERT_SQL, kept)

    counters = {
        "considered": considered,
        "kept": len(kept),
        "dropped_no_close": dropped_no_close,
        "dropped_untradeable": dropped_untradeable,
    }
    logger.info(
        "universe.prescreen.momentum",
        as_of=as_of.isoformat(),
        max_tier=max_tier,
        **counters,
    )
    return counters


__all__ = ["prescreen_momentum"]
