"""Self-heal vendor-late probes — bridge between the per-adapter
publication.latest_published() probes (cheap "vendor's latest") and
the orchestrator's per-source heal/escalate decision.

The publication.py probes answer "what is the vendor's latest?" The
orchestrator additionally needs "is the vendor newer than what WE
hold?" — which requires reading our own DB. This module owns that
DB-side lookup so the orchestrator stays generic.

Why a separate module from publication.py: publication.py is
adapter/feed-side (no DB knowledge); these probes compose it with a
per-source DB query and live in the self-heal layer where the
orchestrator is. Adding a probe is one registry entry — no
orchestrator edits.

A probe returns ``None`` if it cannot determine the vendor state
(probe failed, our DB is empty, vendor returned malformed metadata).
The orchestrator falls back to the existing heal-or-escalate behaviour
in that case — never silently green on an undeterminable signal.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import date
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from tpcore.feeds.publication import source_has_newer

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg


class VendorState(BaseModel):
    """The probe's typed answer: our DB state + vendor state + verdict.

    ``has_newer`` is the verdict the orchestrator acts on; the two
    date fields are for the distinct INFO event the wrapper emits."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    our_latest: date
    vendor_latest: date
    has_newer: bool


VendorProbe = Callable[["asyncpg.Pool"], Awaitable[VendorState | None]]


async def _aaii_sentiment_probe(pool: asyncpg.Pool) -> VendorState | None:
    """Vendor-state probe for the AAII Sentiment Survey.

    Our latest = MAX(date) FROM platform.aaii_sentiment.
    Vendor latest = HEAD Last-Modified on AAII's .xls (publication.py
    AAII probe). Returns None if either side is undeterminable so the
    orchestrator stays strict."""
    async with pool.acquire() as conn:
        our_latest = await conn.fetchval(
            "SELECT MAX(date) FROM platform.aaii_sentiment"
        )
    if our_latest is None:
        return None
    has_newer = await source_has_newer("aaii_sentiment", our_latest)
    if has_newer is None:
        return None
    # source_has_newer doesn't return the vendor's exact latest; for the
    # INFO event the wrapper emits, we want to surface that date too.
    # Re-fetch through the same probe — cheap (already cached by AAII's
    # browser-friendly server-side cache).
    from tpcore.feeds.publication import PUBLICATION_PROBES
    probe = PUBLICATION_PROBES.get("aaii_sentiment")
    if probe is None:
        return None
    vendor_latest = await probe()
    if vendor_latest is None:
        return None
    return VendorState(
        our_latest=our_latest,
        vendor_latest=vendor_latest,
        has_newer=has_newer,
    )


async def _macro_indicators_probe(pool: asyncpg.Pool) -> VendorState | None:
    """Vendor-state probe for FRED macro_indicators.

    Our latest = MIN(per-series MAX(date)) FROM platform.macro_indicators.
    The MIN composition mirrors the publication.py _fred_probe's
    MIN-across-series — both sides anchor to the most-behind series so
    "vendor has nothing newer" only fires when EVERY series is current.

    Vendor latest = MIN(observation_end) across INDICATOR_SERIES (the
    feed-level publication.py probe). Returns None if either side is
    undeterminable."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT MIN(latest) AS our_latest
            FROM (
                SELECT indicator, MAX(date) AS latest
                FROM platform.macro_indicators
                GROUP BY indicator
            ) per_series
        """)
    our_latest = row["our_latest"] if row else None
    if our_latest is None:
        return None
    has_newer = await source_has_newer("macro_indicators", our_latest)
    if has_newer is None:
        return None
    from tpcore.feeds.publication import PUBLICATION_PROBES
    probe = PUBLICATION_PROBES.get("macro_indicators")
    if probe is None:
        return None
    vendor_latest = await probe()
    if vendor_latest is None:
        return None
    return VendorState(
        our_latest=our_latest,
        vendor_latest=vendor_latest,
        has_newer=has_newer,
    )


async def _prices_daily_probe(pool: asyncpg.Pool) -> VendorState | None:
    """Vendor-state probe for Alpaca prices_daily.

    Our latest = MAX(date) FROM platform.prices_daily WHERE ticker='SPY'.
    SPY is the universal anchor (in CRITICAL_TICKERS, every NYSE
    session, never delisted) — symmetric with the publication.py
    _alpaca_probe which queries Alpaca's latest bar on SPY. Querying
    MIN-across-universe would be wrong here: a delisted/halted ticker
    would peg our_latest to its last-trade-date forever.

    Vendor latest = Alpaca's latest SPY bar (publication.py probe).
    Returns None if either side is undeterminable (empty DB, probe
    failure)."""
    async with pool.acquire() as conn:
        our_latest = await conn.fetchval(
            "SELECT MAX(date) FROM platform.prices_daily WHERE ticker = 'SPY'"
        )
    if our_latest is None:
        return None
    has_newer = await source_has_newer("prices_daily", our_latest)
    if has_newer is None:
        return None
    from tpcore.feeds.publication import PUBLICATION_PROBES
    probe = PUBLICATION_PROBES.get("prices_daily")
    if probe is None:
        return None
    vendor_latest = await probe()
    if vendor_latest is None:
        return None
    return VendorState(
        our_latest=our_latest,
        vendor_latest=vendor_latest,
        has_newer=has_newer,
    )


# feed/HealSpec.source → vendor-state probe. Sources without an entry
# fall back to the orchestrator's existing behaviour (heal as usual);
# adding a probe is one entry, no orchestrator edits.
VENDOR_PROBES: dict[str, VendorProbe] = {
    "aaii_sentiment": _aaii_sentiment_probe,
    "macro_indicators": _macro_indicators_probe,
    "prices_daily": _prices_daily_probe,
}


__all__ = ["VENDOR_PROBES", "VendorProbe", "VendorState"]
