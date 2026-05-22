"""Recompute ``platform.liquidity_tiers`` from ``spread_observations``.

Source policy
-------------
Default source is ``abdi_ranaldo`` (the active spread estimator as of
2026-05-15; replaced Corwin-Schultz which was found to invert
liquidity rankings on individual stocks). The aggregator is
source-agnostic — pass ``--sources`` to filter to a different
estimator, e.g. ``--sources tradier_streaming`` when a real-time
quote feed lands. Legacy ``corwin_schultz`` rows are retained in
``platform.spread_observations`` for historical audit but are not
read by default.

Why provisional=false out of the gate
-------------------------------------
The Tradier-driven design intended the ``provisional`` flag to model
"we've seen too few intraday quotes to trust the tier". With AR we
already have 20+ daily OHLC bars per ticker the first time this
runs, which is enough for the per-ticker mean to stabilise. We still
set ``provisional = true`` for any ticker with fewer than 5
observations in the aggregate (e.g. brand-new IPOs) — same intent,
different trigger.

Active-universe gap-fill (2026-05-22)
-------------------------------------
``_stage_tier_refresh`` has a 60-day per-source bootstrap skip-guard
to amortise the ~20-30min Abdi-Ranaldo bootstrap. Tickers that newly
enter the active universe BETWEEN bootstrap runs (new IPOs / freshly
relisted symbols / new universe additions) therefore have zero
``spread_observations`` rows and the per-source aggregation cannot
emit them — leaving them silently missing from ``liquidity_tiers``
until the next full bootstrap (up to ~60 days).

This violated the zero-tolerance ``liquidity_tiers_completeness``
invariant (every active-universe stock has a row, any tier 1-5).
The producer now closes the gap unconditionally: after the main
aggregation upsert, any active-universe stock ticker still missing
from ``liquidity_tiers`` gets a placeholder row at ``tier=DEFAULT_TIER``
(``observations=0``, ``provisional=true``, ``median/p95_spread_pct =
TIER_BOUNDS[3]`` = 2%, the T4 ceiling — conservative on purpose so
the row doesn't accidentally enter T1+T2 filters). The next quarterly
full bootstrap will overwrite these rows with real estimates via
``ON CONFLICT (ticker) DO UPDATE``.

This is a producer-side fix — the check's universe definition is
unchanged. Tickers excluded from the active universe upstream
(``asset_class != 'stock'`` ETFs / SPACs / funds, or dormant tickers
with no bar in the trailing 30 NYSE sessions) are still legitimately
absent from ``liquidity_tiers``.

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
from typing import Any

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

# Active-universe stock tickers that are still missing from
# ``platform.liquidity_tiers`` AFTER the per-source aggregation upsert.
# Mirrors the universe definition in
# ``tpcore.quality.validation.checks.liquidity_tiers_completeness``
# (asset_class='stock' OR NULL, ≥1 prices_daily bar in trailing 30
# NYSE sessions, NOT delisted) so the producer and the check agree on
# what "active universe" means by construction. Used by the gap-fill
# pass to insert provisional placeholder rows so newly-listed /
# freshly-relisted tickers are never silently dropped between
# quarterly bootstraps.
_GAP_FILL_SQL = """
    WITH active_universe AS (
        SELECT DISTINCT pd.ticker
        FROM platform.prices_daily pd
        LEFT JOIN platform.ticker_classifications tc USING (ticker)
        WHERE pd.date >= CURRENT_DATE - INTERVAL '30 days'
          AND pd.delisted = false
          AND COALESCE(tc.asset_class, 'stock') = 'stock'
    )
    SELECT au.ticker
    FROM active_universe au
    LEFT JOIN platform.liquidity_tiers lt USING (ticker)
    WHERE lt.ticker IS NULL
    ORDER BY au.ticker
"""

# Placeholder spread for gap-fill rows. TIER_BOUNDS[3] = 0.0200 is the
# T4 upper bound — by construction this places gap-filled tickers in
# T5 via ``_tier_for`` (>= 0.0200 → 5). But we WANT them at the
# documented DEFAULT_TIER (4) so downstream consumers that filter on
# ``tier <= 4`` for "tradeable" don't accidentally drop a brand-new
# IPO that's actually liquid (the placeholder is conservative pending
# the next bootstrap). We therefore set ``median_spread_pct`` to a
# value JUST under the T4 ceiling (1.999%) so it both lands in T4 via
# the regular ``_tier_for`` math AND is obviously a placeholder under
# manual inspection. ``observations=0`` and ``provisional=true`` are
# the audit signals that this row came from gap-fill, not from a real
# Abdi-Ranaldo estimate.
_GAP_FILL_PLACEHOLDER_SPREAD = Decimal("0.01999")


async def _gap_fill_active_universe(
    conn: Any,
) -> list[str]:
    """Find active-universe stock tickers still missing from
    ``platform.liquidity_tiers`` after the per-source aggregation
    upsert. Returns the sorted ticker list (possibly empty)."""
    rows = await conn.fetch(_GAP_FILL_SQL)
    return [r["ticker"] for r in rows]


async def assign_tiers(*, db_url: str, sources: list[str]) -> dict[int, int]:
    """Recompute every ticker's tier; return ``{tier: n_tickers}``.

    Two-pass:

    1. **Aggregation pass** — fold ``spread_observations`` (last 30
       days, requested sources) into per-ticker (median, p95,
       observations) → tier band, upsert.
    2. **Gap-fill pass** — any active-universe stock ticker still
       missing from ``platform.liquidity_tiers`` (newly listed since
       the last full bootstrap, freshly relisted, etc.) gets a
       placeholder row at ``tier=DEFAULT_TIER``,
       ``provisional=true``, ``observations=0``. Closes the
       ``liquidity_tiers_completeness`` invariant gap (audit
       2026-05-21 #260).

    The bucket counts both passes. The keys are tier numbers and the
    values are the number of tickers upserted into that tier in this
    call.
    """
    pool = await build_asyncpg_pool(db_url, max_size=4)
    try:
        # ── 1. Aggregation pass ────────────────────────────────────
        async with pool.acquire() as conn:
            rows = await conn.fetch(_AGGREGATE_SQL, sources)
        if not rows:
            logger.warning("assign_tiers.no_observations sources=%s", sources)
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
        if params:
            async with pool.acquire() as conn:
                await conn.executemany(_UPSERT_SQL, params)

        # ── 2. Gap-fill pass ───────────────────────────────────────
        async with pool.acquire() as conn:
            missing = await _gap_fill_active_universe(conn)
        if missing:
            placeholder = _GAP_FILL_PLACEHOLDER_SPREAD
            placeholder_tier = _tier_for(placeholder)
            # Defensive — the placeholder is chosen so this is
            # exactly DEFAULT_TIER. If a future change to TIER_BOUNDS
            # invalidates that, fail loud rather than silently
            # mis-tiering 15k tickers.
            if placeholder_tier != DEFAULT_TIER:
                raise RuntimeError(
                    f"gap-fill placeholder spread {placeholder} maps to "
                    f"tier {placeholder_tier}, expected DEFAULT_TIER="
                    f"{DEFAULT_TIER}. Adjust _GAP_FILL_PLACEHOLDER_SPREAD."
                )
            gap_params: list[tuple] = [
                (
                    t,
                    DEFAULT_TIER,
                    placeholder,
                    placeholder,
                    0,           # observations — zero by construction
                    True,        # provisional — gap-fill audit signal
                )
                for t in missing
            ]
            async with pool.acquire() as conn:
                await conn.executemany(_UPSERT_SQL, gap_params)
            bucket[DEFAULT_TIER] = bucket.get(DEFAULT_TIER, 0) + len(missing)
            logger.info(
                "assign_tiers.gap_fill n=%d tier=%d sample=%s",
                len(missing), DEFAULT_TIER, missing[:5],
            )

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
        default="abdi_ranaldo",
        help=(
            "Comma-separated list of spread_observations.source values to "
            "aggregate. Default 'abdi_ranaldo' (the active estimator). "
            "Legacy 'corwin_schultz' rows are retained for audit but no "
            "longer aggregated by default. When a real quote feed lands, "
            "add it here."
        ),
    )
    return p.parse_args(argv)


def main() -> None:  # pragma: no cover
    raise SystemExit(asyncio.run(amain(_parse_args())))


if __name__ == "__main__":
    main()


__all__ = ["assign_tiers", "TIER_BOUNDS", "DEFAULT_TIER", "_tier_for"]
