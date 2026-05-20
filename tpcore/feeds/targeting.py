"""Demand-driven targeting for constrained feeds (#165 facet 3).

A feed with a hard limit (rate cap, ticker cap, free-tier ceiling,
anti-bot) must spend its scarce budget where an event is materialising
— not on random/whole-universe tickers. For
``Targeting.CONSTRAINED_DEMAND_DRIVEN`` feeds, the pull is PRIORITISED
by the demand set; ``WHOLE_UNIVERSE`` feeds are never narrowed.

Demand is derived **read-only from shared platform tables** — engine
*output*, never engine code (the "no engine code modified" constraint
holds): tickers the engines are actually acting on / watching —
``open_orders`` (live/recent orders), ``aar_events`` (recent
post-trade activity), ``universe_candidates`` (recent screened
candidates). An empty demand set is valid (paper/early stage) — the
caller then keeps its existing bounded behaviour; demand only
RE-ORDERS within the budget, it never widens or zeroes a pull.

Rollout state (2026-05-20):

* **Wedge fits + wired:** ``iborrowdesk_borrow_rates`` (per-ticker
  scrape loop with a max_tickers cap) and ``finnhub_insider_sentiment``
  (per-ticker API loop with a ~60/min free-tier rate cap). Demand
  tickers land at the FRONT of the loop so a truncated run still
  covers what the engines care about.
* **Wedge does NOT fit — intentionally probe-less:**
  ``apewisdom_social_sentiment`` and ``finra_short_interest`` are
  single bulk pulls (no per-ticker API call to prioritise — the
  constraint is the vendor's global response ceiling, not our per-
  ticker budget). ``greeks_max_pain`` is a single-symbol snapshot
  (no universe to prioritise — engines read specific symbols, so
  dynamic demand-driven symbol-switching would mismatch consumers).
  These remain CONSTRAINED_DEMAND_DRIVEN in FeedProfile because the
  budget constraint is real, but addressed via cadence + DFCR
  provider augmentation (not ticker prioritisation).

Adding a probe-fits constrained feed = one ``demand_targets``/
``prioritise`` call in the handler, mirroring the exemplars above.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from tpcore.feeds.profile import FEED_PROFILES, Targeting

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

_DEMAND_SQL = """
    SELECT DISTINCT ticker FROM (
        SELECT ticker FROM platform.open_orders
        WHERE ticker IS NOT NULL
        UNION
        SELECT ticker FROM platform.aar_events
        WHERE ticker IS NOT NULL
          AND recorded_at >= now() - interval '60 days'
        UNION
        SELECT ticker FROM platform.universe_candidates
        WHERE ticker IS NOT NULL
          AND created_at >= now() - interval '30 days'
    ) d
"""


async def demand_targets(
    pool: asyncpg.Pool, feed: str
) -> list[str] | None:
    """Prioritised demand tickers for a constrained feed.

    Returns ``None`` for WHOLE_UNIVERSE feeds (no narrowing) or an
    unknown feed. Returns a (possibly empty) ticker list for
    CONSTRAINED_DEMAND_DRIVEN feeds — the caller pulls these FIRST
    within its existing budget, then fills the remainder normally.
    An empty list ⇒ no current demand ⇒ caller's unchanged behaviour.
    """
    p = FEED_PROFILES.get(feed)
    if p is None or p.targeting != Targeting.CONSTRAINED_DEMAND_DRIVEN:
        return None
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(_DEMAND_SQL)
    except Exception:
        # Demand is an optimisation, never load-bearing — a failure
        # must not break the pull; degrade to "no demand signal".
        return []
    return sorted({r["ticker"].upper() for r in rows if r["ticker"]})


def prioritise(universe: list[str], demand: list[str] | None) -> list[str]:
    """Re-order ``universe`` so demand tickers (that are in-universe)
    come first, preserving the rest. Pure. ``demand`` None/empty →
    universe unchanged (no regression for WHOLE_UNIVERSE / no-demand)."""
    if not demand:
        return universe
    uset = set(universe)
    head = [t for t in demand if t in uset]
    hset = set(head)
    return head + [t for t in universe if t not in hset]


__all__ = ["demand_targets", "prioritise"]
