"""Per-feed cadence profile — the single source of truth (#163).

Replaces scattered, independently-guessed blanket constants (per-check
``MAX_AGE_DAYS``, per-handler ``skip_guard_days``, the one-size daily
``--update`` sweep) with ONE evidence-backed declaration per feed.
tpcore-common; a feed with a unique cadence overrides the blanket by
declaring its own profile entry. A feed's cadence drives — coherently,
from the same number — its skip-guard, its freshness threshold, and
the self-heal expectation, so they can never disagree.

Four declared facets per feed:

1. ``trigger``  — the use-case event that should drive the pull (NOT a
   blanket daily cron). Declarative today; the scheduler re-arch that
   *enforces* it is phased (launchd/daemon level — not ripped out here).
2. ``cadence``  — ``cadence_days`` + measured ``dissemination_lag_days``
   → ``freshness_max_age_days`` and ``skip_guard_days``. FULLY ENFORCED:
   the freshness checks read ``freshness_max_age_days`` from here.
3. ``targeting`` — WHOLE_UNIVERSE vs CONSTRAINED_DEMAND_DRIVEN (scarce
   feeds spend budget where an engine setup is forming). Declared;
   the engine-coupled demand set is phased (crosses the engine
   boundary — not built here).
4. ``publication_probe`` — whether a cheap "source has newer than we
   hold?" probe exists so a red means *our* miss, not vendor-late.
   Declared per feed (mostly False today — honest); per-adapter probes
   are phased.

Honest scope: facet 2 is enforced now; facets 1/3/4 are captured as
data with per-feed values and explicitly phased — NOT silently
dropped, NOT fake-claimed done.
"""
from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class FeedTrigger(StrEnum):
    """The use-case event that should drive a feed's pull."""

    MARKET_CLOSE = "market_close"          # prices: after XNYS close
    VENDOR_BIMONTHLY = "vendor_bimonthly"  # FINRA short interest
    VENDOR_WEEKLY = "vendor_weekly"        # AAII survey (Thu)
    VENDOR_RELEASE = "vendor_release"      # FRED per-series schedule
    VENDOR_QUARTERLY = "vendor_quarterly"  # fundamentals / earnings
    CONTINUOUS = "continuous"              # SEC EDGAR, IBorrowDesk
    INTRADAY = "intraday"                  # ApeWisdom (~2h refresh)
    RECOMPUTE = "recompute"                # liquidity_tiers/classify
    DERIVED = "derived"                    # fear_greed (no external pull)


class Targeting(StrEnum):
    WHOLE_UNIVERSE = "whole_universe"
    CONSTRAINED_DEMAND_DRIVEN = "constrained_demand_driven"


class FeedProfile(BaseModel):
    """Declarative cadence/scheduling/targeting profile for one feed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    feed: str
    trigger: FeedTrigger
    cadence_days: int                       # natural period; 0 = continuous/derived
    dissemination_lag_days: int = 0          # measured event→availability lag
    # ENFORCED facet: None = not a staleness check (coverage/window/derived)
    freshness_max_age_days: int | None = None
    skip_guard_days: int | None = None       # handler skip-guard default
    targeting: Targeting = Targeting.WHOLE_UNIVERSE
    publication_probe: bool = False          # phased per-adapter
    # Vendor publication schedule (UTC-anchored, NOT our clock). For
    # weekly feeds: ISO weekday the vendor posts (Mon=1..Sun=7). When
    # set, freshness reasons from "last scheduled vendor publish" not
    # "today − N" — see publication.expected_latest_publish.
    publish_weekday: int | None = None
    evidence: str = ""                       # how cadence/lag were determined


# ── Registry ──────────────────────────────────────────────────────────
# Keyed by feed/source name (matches HealSpec.source / the check's feed).
# Values are evidence-backed: measured this session or from the feed's
# documented publication schedule — NOT re-guessed. Where a value was a
# guessed blanket before, it is corrected here and the check now reads
# from this single source.
FEED_PROFILES: dict[str, FeedProfile] = {
    "prices_daily": FeedProfile(
        feed="prices_daily", trigger=FeedTrigger.MARKET_CLOSE,
        cadence_days=1, freshness_max_age_days=5, skip_guard_days=1,
        evidence="daily bars; pull is triggered by XNYS close, not a "
                 "blanket cron. 5d max-age tolerates weekends/holidays.",
    ),
    "finra_short_interest": FeedProfile(
        feed="finra_short_interest", trigger=FeedTrigger.VENDOR_BIMONTHLY,
        cadence_days=16, dissemination_lag_days=13,
        freshness_max_age_days=42, skip_guard_days=12,
        targeting=Targeting.CONSTRAINED_DEMAND_DRIVEN,
        evidence="live FINRA pull 2026-05-16: 10 settlement periods over "
                 "~140d ⇒ bi-monthly ~16d; release−settlement lag ~13d; "
                 "+~13d slack = 42 (was a guessed 35).",
    ),
    "aaii_sentiment": FeedProfile(
        feed="aaii_sentiment", trigger=FeedTrigger.VENDOR_WEEKLY,
        cadence_days=7, freshness_max_age_days=10, skip_guard_days=5,
        publication_probe=True, publish_weekday=4,  # Thursday (ISO Mon=1)
        evidence="AAII Sentiment Survey closes Wed, results posted "
                 "Thursday (vendor TZ→UTC). Freshness is vendor-anchored "
                 "(last scheduled Thu publish, not today−N) and confirmed "
                 "by the HEAD Last-Modified probe.",
    ),
    "iborrowdesk_borrow_rates": FeedProfile(
        feed="iborrowdesk_borrow_rates", trigger=FeedTrigger.CONTINUOUS,
        cadence_days=1, freshness_max_age_days=5, skip_guard_days=1,
        targeting=Targeting.CONSTRAINED_DEMAND_DRIVEN,
        evidence="daily borrow rates; scrape-fragile + ticker-limited → "
                 "demand-driven targeting. 5d tolerates anti-bot skips.",
    ),
    "apewisdom_social_sentiment": FeedProfile(
        feed="apewisdom_social_sentiment", trigger=FeedTrigger.INTRADAY,
        cadence_days=1, freshness_max_age_days=7, skip_guard_days=1,
        targeting=Targeting.CONSTRAINED_DEMAND_DRIVEN,
        evidence="ApeWisdom refreshes ~2h; coverage ceiling ~23% of "
                 "T1/T2 (1131 source tickers, 345 overlap) — see "
                 "social_sentiment_freshness (floor 0.15, evidence-derived).",
    ),
    "finnhub_insider_sentiment": FeedProfile(
        feed="finnhub_insider_sentiment", trigger=FeedTrigger.VENDOR_QUARTERLY,
        cadence_days=30, freshness_max_age_days=None, skip_guard_days=25,
        targeting=Targeting.CONSTRAINED_DEMAND_DRIVEN,
        evidence="Finnhub MSPR is monthly-period (age measured in months "
                 "by its check); free-tier ticker-limited → demand-driven.",
    ),
    "greeks_max_pain": FeedProfile(
        feed="greeks_max_pain", trigger=FeedTrigger.MARKET_CLOSE,
        cadence_days=1, freshness_max_age_days=7, skip_guard_days=1,
        targeting=Targeting.CONSTRAINED_DEMAND_DRIVEN,
        evidence="greeks.pro free tier = 1 symbol (SPY) by design; "
                 "demand-driven if it ever expands.",
    ),
    "earnings_events": FeedProfile(
        feed="earnings_events", trigger=FeedTrigger.VENDOR_QUARTERLY,
        cadence_days=91, freshness_max_age_days=90, skip_guard_days=6,
        evidence="earnings beats are quarterly; 90d max-age = ~one "
                 "earnings cycle (FMP).",
    ),
    "sec_insider_transactions": FeedProfile(
        feed="sec_insider_transactions", trigger=FeedTrigger.CONTINUOUS,
        cadence_days=1, freshness_max_age_days=14, skip_guard_days=3,
        evidence="Form 4 ≥ daily, 8-K weekly across the universe; 14d "
                 "max-age. Bulk Form-345 ETL + full-history 8-K shipped.",
    ),
    "macro_indicators": FeedProfile(
        feed="macro_indicators", trigger=FeedTrigger.VENDOR_RELEASE,
        cadence_days=30, freshness_max_age_days=90, skip_guard_days=7,
        evidence="FRED series have PER-SERIES cadences (VIX daily, claims "
                 "weekly, INDPRO monthly). 90d blanket max-age covers the "
                 "slowest; the per-series publication-availability gate is "
                 "the phased refinement (facet 4).",
    ),
    "liquidity_tiers": FeedProfile(
        feed="liquidity_tiers", trigger=FeedTrigger.RECOMPUTE,
        cadence_days=90, freshness_max_age_days=100, skip_guard_days=90,
        evidence="tiers drift slowly; recompute quarterly. 100d max-age.",
    ),
    "ticker_classifications": FeedProfile(
        feed="ticker_classifications", trigger=FeedTrigger.RECOMPUTE,
        cadence_days=30, freshness_max_age_days=None, skip_guard_days=30,
        evidence="asset-class near-static; coverage metric, not age "
                 "(refresh picks up new listings monthly).",
    ),
    "fear_greed": FeedProfile(
        feed="fear_greed", trigger=FeedTrigger.DERIVED,
        cadence_days=1, freshness_max_age_days=3, skip_guard_days=1,
        evidence="computed from existing platform data (no external "
                 "pull); 3 NYSE sessions max-age.",
    ),
    'fundamentals_quarterly': FeedProfile(
        feed='fundamentals_quarterly', trigger=FeedTrigger.VENDOR_RELEASE,
        cadence_days=91, freshness_max_age_days=120, skip_guard_days=6,
        evidence='financial fundamentals (pb/de/revenue/net_income/fcf/etc) for value-engine setup detection — already ingested via FMP for months; formal ProviderBinding registration was missing (surfaced 2026-05-20 by the autonomous-self-heal P0 completeness invariant work).',
    ),
}


def profile_for(feed: str) -> FeedProfile | None:
    return FEED_PROFILES.get(feed)


def freshness_max_age_days(feed: str, default: int) -> int:
    """Authoritative freshness max-age for ``feed``.

    The profile is the single source of truth; ``default`` is only the
    safety net if a feed somehow has no profile (the drift test makes
    that a build failure, so it should never fire in practice).
    """
    p = FEED_PROFILES.get(feed)
    if p is None or p.freshness_max_age_days is None:
        return default
    return p.freshness_max_age_days


__all__ = [
    "FEED_PROFILES",
    "FeedProfile",
    "FeedTrigger",
    "Targeting",
    "freshness_max_age_days",
    "profile_for",
]
