"""Recompute ``platform.liquidity_tiers`` from ``spread_observations``.

Source policy
-------------
The original Phase 2 plan called for streaming Tradier quote data to
populate this table. Tradier is being deprecated, so we ship the cost
model on the Corwin-Schultz bootstrap alone: ``WHERE source =
'corwin_schultz'``. The aggregator was designed source-agnostic, so
when a real-time quote feed lands later it joins the same table by
extending the ``source IN (...)`` filter — no schema change needed.

Why provisional=false out of the gate
-------------------------------------
The Tradier-driven design intended the ``provisional`` flag to model
"we've seen too few intraday quotes to trust the tier". With CS we
already have 20+ daily HL pairs per ticker the first time this runs,
which is enough for the per-ticker mean to stabilise. We still set
``provisional = true`` for any ticker with fewer than 5 observations
in the aggregate (e.g. brand-new IPOs) — same intent, different
trigger.

Tier thresholds (from EDGE_VALIDATION_PLAN.md):
    T1: median spread < 0.05%
    T2: 0.05% – 0.15%
    T3: 0.15% – 0.50%
    T4: 0.50% – 2.00%   (also the default for unknown tickers)
    T5: > 2.00%

Run::

    DATABASE_URL=$DATABASE_URL_IPV4 python scripts/assign_liquidity_tiers.py
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from decimal import Decimal

from tpcore.db import build_asyncpg_pool

logger = logging.getLogger("scripts.assign_liquidity_tiers")

# Tier upper bounds (median spread as a fraction of mid). A ticker is
# in tier i iff its median spread is <= TIER_BOUNDS[i-1].
TIER_BOUNDS = (
    Decimal("0.0005"),  # T1: 5 bps
    Decimal("0.0015"),  # T2: 15 bps
    Decimal("0.0050"),  # T3: 50 bps
    Decimal("0.0200"),  # T4: 200 bps  (also the default tier)
)
DEFAULT_TIER = 4
MIN_OBSERVATIONS_FOR_STABLE = 5


def _tier_for(median_spread: Decimal) -> int:
    """Map a median-spread fraction to a 1..5 tier."""
    for i, bound in enumerate(TIER_BOUNDS, start=1):
        if median_spread < bound:
            return i
    return 5


_AGGREGATE_SQL = """
    SELECT ticker,
           percentile_cont(0.50) WITHIN GROUP (ORDER BY spread_pct) AS median_spread_pct,
           percentile_cont(0.95) WITHIN GROUP (ORDER BY spread_pct) AS p95_spread_pct,
           COUNT(*) AS observations
    FROM platform.spread_observations
    WHERE source = ANY($1::text[])
      AND observed_at > now() - INTERVAL '30 days'
    GROUP BY ticker
"""

_UPSERT_SQL = """
    INSERT INTO platform.liquidity_tiers
        (ticker, tier, median_spread_pct, p95_spread_pct, observations,
         provisional, last_updated)
    VALUES ($1, $2, $3, $4, $5, $6, now())
    ON CONFLICT (ticker) DO UPDATE SET
        tier              = EXCLUDED.tier,
        median_spread_pct = EXCLUDED.median_spread_pct,
        p95_spread_pct    = EXCLUDED.p95_spread_pct,
        observations      = EXCLUDED.observations,
        provisional       = EXCLUDED.provisional,
        last_updated      = now()
"""


async def assign_tiers(*, db_url: str, sources: list[str]) -> dict[int, int]:
    """Recompute every ticker's tier; return ``{tier: n_tickers}``."""
    pool = await build_asyncpg_pool(db_url, max_size=4)
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(_AGGREGATE_SQL, sources)
        if not rows:
            logger.warning("assign_tiers.no_observations sources=%s", sources)
            return {}
        params: list[tuple] = []
        bucket: dict[int, int] = {}
        for r in rows:
            median = Decimal(str(r["median_spread_pct"]))
            p95 = Decimal(str(r["p95_spread_pct"]))
            n = int(r["observations"])
            tier = _tier_for(median)
            provisional = n < MIN_OBSERVATIONS_FOR_STABLE
            params.append((r["ticker"], tier, median, p95, n, provisional))
            bucket[tier] = bucket.get(tier, 0) + 1
        async with pool.acquire() as conn:
            await conn.executemany(_UPSERT_SQL, params)
        return bucket
    finally:
        await pool.close()


async def amain(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db_url = os.getenv("DATABASE_URL") or os.getenv("DATABASE_URL_IPV4")
    if not db_url:
        print("FAILED — DATABASE_URL not set", file=sys.stderr)
        return 1
    sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    if not sources:
        print("FAILED — --sources must list at least one source", file=sys.stderr)
        return 1
    bucket = await assign_tiers(db_url=db_url, sources=sources)
    if not bucket:
        print("no rows assigned — spread_observations empty for the requested sources")
        return 0
    print(f"assigned {sum(bucket.values())} tickers across {len(bucket)} tiers")
    for tier in sorted(bucket):
        print(f"  T{tier}: {bucket[tier]}")
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument(
        "--sources",
        default="corwin_schultz",
        help=(
            "Comma-separated list of spread_observations.source values to "
            "aggregate. Default 'corwin_schultz' (the only source we ship "
            "with today). When a real quote feed lands, add it here."
        ),
    )
    return p.parse_args(argv)


def main() -> None:  # pragma: no cover
    raise SystemExit(asyncio.run(amain(_parse_args())))


if __name__ == "__main__":
    main()


__all__ = ["assign_tiers", "TIER_BOUNDS", "DEFAULT_TIER", "_tier_for"]
