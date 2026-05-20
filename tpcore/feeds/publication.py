"""Publication-availability gate (#165 facet 4).

A freshness check should go red only when the VENDOR has published
something newer than we hold — not merely because our newest row is
older than the cadence window. "Vendor hasn't published yet" is an
expected quiet state, NOT our defect and NOT a self-heal trigger
(re-pulling then is pointless churn). Per the no-lazy-vendor-blame
rule, "vendor is late" must be PROVEN by a cheap probe, never assumed.

Generic mechanism: a per-feed optional async ``PublicationProbe`` that
returns the source's latest available period (cheaply — a HEAD /
small request, NOT a full download). ``source_has_newer`` consults it:

* ``True``  — source has newer than we hold → genuine staleness, red,
  honestly self-healable (re-pull will fix it).
* ``False`` — source has nothing newer → vendor-late → quiet, NOT red.
* ``None``  — no probe registered, or the probe failed → caller MUST
  fall back to the strict (assume-behind) behaviour. Never silently
  green: an unprovable "maybe vendor-late" stays red.

Honest scope: the generic gate + the AAII exemplar (HEAD
``Last-Modified`` on its .xls — cheap, no auth, live-verifiable) are
built and enforced. Other feeds have no cheap "latest available"
probe yet (e.g. FINRA's API exposes no max-settlement without full
pagination) — they are intentionally absent here and fall back to
the strict cadence behaviour (already honest post-recalibration).
Adding a feed's probe is one registry entry — no gate edits.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta

from tpcore.feeds.profile import FEED_PROFILES

# A probe returns the vendor's latest-available period, or None if it
# cannot be determined (caller then stays strict).
PublicationProbe = Callable[[], Awaitable[date | None]]


async def _aaii_probe() -> date | None:
    from tpcore.aaii import AAIIAdapter

    async with AAIIAdapter() as a:
        return await a.latest_published()


async def _fred_probe() -> date | None:
    """Feed-level probe for ``macro_indicators``: ask FRED for each
    series's ``observation_end`` and return MIN across them.

    Multi-series feeds need a conservative composition: ``False`` from
    ``source_has_newer`` means "the vendor has nothing newer than we
    hold" — for a multi-series feed, that only holds if EVERY series
    has nothing newer. Taking MIN means the probe answers True (heal
    is honest) as soon as ANY series has moved past our ``our_latest``
    floor; the more permissive "MAX" would silently green when one
    laggard series alone covered for an entire stuck feed.

    Returns ``None`` if any series probe fails — strict-behind
    fallback per the publication-gate contract."""
    from tpcore.fred import INDICATOR_SERIES, FREDAdapter

    earliest: date | None = None
    async with FREDAdapter() as a:
        for _name, series_id in INDICATOR_SERIES:
            d = await a.latest_published(series_id)
            if d is None:
                return None
            if earliest is None or d < earliest:
                earliest = d
    return earliest


async def _alpaca_probe() -> date | None:
    """Feed-level probe for ``prices_daily``: ask Alpaca for the date
    of its latest daily bar on SPY.

    Single-anchor design (not MIN-across-universe): the prices_daily
    feed has thousands of tickers, but the question this probe
    answers is "has a new NYSE session published yet?" — answered
    deterministically by any always-trading anchor. SPY is the
    natural choice: high-liquidity, every NYSE session, never
    delisted, already in ``CRITICAL_TICKERS`` for
    prices_daily_freshness. MIN-across-universe (the FRED pattern)
    would be wrong here: a single delisted/halted ticker would peg
    the answer to its last bar's date forever.

    The adapter pins the call to the IEX feed (the Algo Trader Plus
    tier 403s the latest-bar endpoint on SIP; IEX is unrestricted +
    SPY is universally traded so coverage is complete for the
    anchor). Production ingestion still uses SIP for the historical
    bars pull — this probe is a separate cheap "is there a new
    session?" question.

    Returns ``None`` if the probe fails (e.g. ALPACA_KEY unset, API
    outage) — caller stays strict per the publication-gate contract."""
    from tpcore.alpaca import AlpacaDataAdapter

    try:
        adapter = AlpacaDataAdapter()
    except RuntimeError:
        # ALPACA_KEY / ALPACA_SECRET unset — can't probe; stay strict.
        return None
    return await adapter.latest_published("SPY")


# feed (matches FeedProfile key / HealSpec.source) → probe.
PUBLICATION_PROBES: dict[str, PublicationProbe] = {
    "aaii_sentiment": _aaii_probe,
    "macro_indicators": _fred_probe,
    "prices_daily": _alpaca_probe,
}


async def source_has_newer(feed: str, our_latest: date | None) -> bool | None:
    """Does the vendor have something newer than ``our_latest``?

    ``None`` → undeterminable (no probe / probe failed / no held data)
    → caller falls back to strict red. Never returns False unless the
    probe positively shows the vendor has nothing newer.
    """
    probe = PUBLICATION_PROBES.get(feed)
    if probe is None or our_latest is None:
        return None
    src_latest = await probe()
    if src_latest is None:
        return None
    return src_latest > our_latest


def expected_latest_publish(
    feed: str, now: datetime | None = None
) -> date | None:
    """The vendor's most-recent SCHEDULED publish date ≤ ``now``,
    anchored to the VENDOR's calendar in UTC — never our clock.

    For a fixed-weekday vendor (``publish_weekday`` set, e.g. AAII =
    Thursday): the latest occurrence of that ISO weekday on/before
    today (UTC), minus ``dissemination_lag_days``. The freshness check
    is "behind" only if our newest row predates THIS date (we missed a
    scheduled vendor publish) — not "today − N".

    Returns ``None`` when the feed has no fixed-weekday schedule (its
    freshness is already vendor-anchored a different way — FINRA's
    settlement-date check, the market calendar — or its per-entity
    schedule is the phased deep work). Caller then keeps cadence
    behaviour; never silent-green.
    """
    p = FEED_PROFILES.get(feed)
    if p is None or p.publish_weekday is None:
        return None
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    today = now.astimezone(UTC).date()
    # Step back to the most recent occurrence of the vendor's weekday.
    delta = (today.isoweekday() - p.publish_weekday) % 7
    last_scheduled = today - timedelta(days=delta)
    # Vendor needs the dissemination lag to actually post it; if we're
    # still inside that window for the most recent scheduled date, the
    # prior cycle is the one we should already hold.
    if (today - last_scheduled).days < p.dissemination_lag_days:
        last_scheduled -= timedelta(days=7)
    return last_scheduled


__all__ = [
    "PUBLICATION_PROBES",
    "PublicationProbe",
    "expected_latest_publish",
    "source_has_newer",
]
