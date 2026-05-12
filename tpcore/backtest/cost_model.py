"""Transaction cost model for backtests and the Risk Governor cost gate.

Two layers:

* ``SimpleCostModel`` — legacy per-side slippage helper. Still useful
  for backtests that want a flat assumption (research, calibration).
  Defaults bumped from 5 bps (the old "all stocks are liquid"
  assumption) to ``T4`` (the default tier) so an unconfigured backtest
  doesn't silently understate cost.
* ``get_round_trip_cost(pool, ticker)`` — DB-backed lookup against
  ``platform.liquidity_tiers``. Returns the actual median-spread cost
  for the ticker, or the T4 default (1.50% round-trip) when the
  ticker isn't tier'd yet. This is what the Risk Governor's
  ``check_cost`` calls.

Phase 2 source policy: tiers are populated by
``scripts/assign_liquidity_tiers.py`` from
``platform.spread_observations`` rows. Until a real-time quote feed
is wired, the only source feeding that table is the Corwin-Schultz
bootstrap (see ``tpcore.backtest.spread_estimator``). The cost-model
contract is source-agnostic — when a streaming feed lands, only the
aggregation script changes.
"""
from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

import structlog
from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)

# Tier defaults — same thresholds as scripts/assign_liquidity_tiers.py.
# Used both as fallback and as the slippage_bps default below.
DEFAULT_ROUND_TRIP_COST_PCT = Decimal("0.0150")  # T4 1.50% — the spec's default
DEFAULT_ROUND_TRIP_COST_BPS = DEFAULT_ROUND_TRIP_COST_PCT * Decimal("10000")
DEFAULT_PER_SIDE_SLIPPAGE_BPS = DEFAULT_ROUND_TRIP_COST_BPS / Decimal("2")


class SimpleCostModel(BaseModel):
    """Symmetric per-side slippage. Override ``slippage_bps`` for explicit values.

    Default is **75 bps per side = 150 bps round-trip = T4** so an
    unconfigured backtest matches the tier model's default-for-unknown.
    The old default of 5 bps (50× tighter) silently overstated edge
    for everything outside the T1 ultra-liquid bucket.
    """

    model_config = ConfigDict(extra="forbid")

    slippage_bps: Decimal = DEFAULT_PER_SIDE_SLIPPAGE_BPS
    commission_per_share: Decimal = Decimal("0")
    min_commission: Decimal = Decimal("0")

    def adjusted_fill_price(self, ref_price: Decimal, side: str) -> Decimal:
        """Apply slippage to ``ref_price``. ``side`` is ``"buy"`` or ``"sell"``."""
        bps = self.slippage_bps / Decimal("10000")
        if side == "buy":
            return ref_price * (Decimal("1") + bps)
        if side == "sell":
            return ref_price * (Decimal("1") - bps)
        raise ValueError(f"unknown side: {side!r}")

    def commission(self, qty: Decimal) -> Decimal:
        c = qty * self.commission_per_share
        return max(c, self.min_commission)


# ── DB-backed tier lookup ───────────────────────────────────────────────


_TIER_LOOKUP_SQL = """
    SELECT tier, median_spread_pct, provisional, last_updated
    FROM platform.liquidity_tiers
    WHERE ticker = $1
"""


async def get_round_trip_cost(
    pool: asyncpg.Pool,
    ticker: str,
) -> Decimal:
    """Return the round-trip cost estimate for ``ticker``.

    Uses the ticker's ``median_spread_pct`` from
    ``platform.liquidity_tiers``. Returns
    ``DEFAULT_ROUND_TRIP_COST_PCT`` (T4 = 1.50%) when the ticker is not
    tier'd yet.

    Rationale: for a market-in/market-out round-trip, the trader
    typically pays the full quoted spread (half on each side). The
    median spread is therefore a defensible point estimate for
    expected round-trip transaction cost. Real fill quality is
    bounded by ``slippage_bps`` in the parity log — this helper is
    the *a priori* cost we compare strategy edge against.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_TIER_LOOKUP_SQL, ticker)
    if row is None:
        return DEFAULT_ROUND_TRIP_COST_PCT
    median = row["median_spread_pct"]
    if median is None:
        return DEFAULT_ROUND_TRIP_COST_PCT
    return Decimal(str(median))


__all__ = [
    "SimpleCostModel",
    "get_round_trip_cost",
    "DEFAULT_ROUND_TRIP_COST_PCT",
    "DEFAULT_ROUND_TRIP_COST_BPS",
    "DEFAULT_PER_SIDE_SLIPPAGE_BPS",
]
