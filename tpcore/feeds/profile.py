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
    """Declarative cadence/scheduling/targeting/quota profile for one feed."""

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

    # ── Vendor download constraints (added 2026-05-24 per operator) ──────
    # The 4 declared facets above describe WHAT to pull and WHEN. These
    # describe HOW MUCH we are allowed to pull, so producers can schedule
    # backfills / repulls / batches at MAX THROUGHPUT without tripping the
    # vendor's rate limiter or burning a daily/monthly quota.
    #
    # rate_limit_requests / rate_limit_period_seconds = the headline
    # "X requests per Y seconds" budget (e.g. FRED 120/min, FMP Starter
    # 300/min, SEC EDGAR 10/sec). None = no vendor-side limit known.
    # The producer should rate-sleep at (period / requests) seconds
    # between requests, with a small safety margin.
    rate_limit_requests: int | None = None
    rate_limit_period_seconds: int | None = None
    # Daily quota — None = no daily cap (most paid tiers). Free tiers
    # typically have one (FMP Basic 250/day, finnhub free 60/min + 30/sec).
    daily_request_quota: int | None = None
    # Monthly volume quota — relevant for bytes-priced vendors like ApeWisdom.
    monthly_request_quota: int | None = None
    # Concurrent connection cap (asyncio semaphore size). None = vendor doesn't
    # publish one; default to 1 for politeness.
    concurrent_request_limit: int | None = None
    # Authoritative URL for the rate-limit + quota declaration (vendor docs)
    # so the next refresh of these numbers has a known starting point.
    quota_source_url: str | None = None

    evidence: str = ""                       # how cadence/lag/quotas were determined


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
        # FMP /stable/historical-price-eod/full on operator's Starter tier
        # ($200/yr): 300 req/min AND 100,000/day. Full-universe pull is
        # ~7,600 tickers — at 5 req/s that's ~25 min, well under daily cap.
        # FMP daily cap is the operator's recurring worry per
        # docs/OPERATIONS.md:1252 — encoded here so schedulers/backfillers
        # can budget against it instead of flying blind.
        rate_limit_requests=300, rate_limit_period_seconds=60,
        daily_request_quota=100_000,
        concurrent_request_limit=1,
        quota_source_url="https://site.financialmodelingprep.com/developer/docs/pricing",
        evidence="daily bars; pull is triggered by XNYS close, not a "
                 "blanket cron. 5d max-age tolerates weekends/holidays. "
                 "Quota: FMP Starter 300/min + 100k/day — primary daily-bars feed "
                 "since 2026-05-22 (project_fmp_primary_daily_bars_2026_05_22). "
                 "Daily cap verified against docs/OPERATIONS.md:1160,1252.",
    ),
    "finra_short_interest": FeedProfile(
        feed="finra_short_interest", trigger=FeedTrigger.VENDOR_BIMONTHLY,
        cadence_days=16, dissemination_lag_days=13,
        freshness_max_age_days=42, skip_guard_days=12,
        targeting=Targeting.CONSTRAINED_DEMAND_DRIVEN,
        # FINRA Data Cloud (OAuth2): no published per-client RPM limit.
        # 10/min is a conservative placeholder — adapter does a single
        # bulk GET per settlement period (~one call every 16 days), so
        # the effective rate is negligible regardless of the cap.
        rate_limit_requests=10, rate_limit_period_seconds=60,
        concurrent_request_limit=1,
        quota_source_url="https://api.finra.org/data/group/otcMarket/name/regShoDaily",
        evidence="live FINRA pull 2026-05-16: 10 settlement periods over "
                 "~140d ⇒ bi-monthly ~16d; release−settlement lag ~13d; "
                 "+~13d slack = 42 (was a guessed 35). Bulk file (single "
                 "GET per period). Quota: vendor publishes no per-client "
                 "RPM cap — declared 10/min is a conservative placeholder "
                 "(actual usage = one call per ~16-day cycle).",
    ),
    "aaii_sentiment": FeedProfile(
        feed="aaii_sentiment", trigger=FeedTrigger.VENDOR_WEEKLY,
        cadence_days=7, freshness_max_age_days=10, skip_guard_days=5,
        publication_probe=True, publish_weekday=4,  # Thursday (ISO Mon=1)
        # AAII serves a single static .xls workbook; no documented rate
        # limit but anti-bot HTTP (browser-mimicking User-Agent required).
        # The "rate" field is mostly symbolic — the adapter pulls ONE file
        # per weekly refresh, so any reasonable cap is non-binding.
        rate_limit_requests=6, rate_limit_period_seconds=60,
        concurrent_request_limit=1,
        quota_source_url="https://www.aaii.com/sentimentsurvey",
        evidence="AAII Sentiment Survey closes Wed, results posted "
                 "Thursday (vendor TZ→UTC). Freshness is vendor-anchored "
                 "(last scheduled Thu publish, not today−N) and confirmed "
                 "by the HEAD Last-Modified probe. Quota: anti-bot only — "
                 "rate field is symbolic (adapter pulls ONE full-history "
                 ".xls per weekly refresh; no per-request iteration).",
    ),
    "iborrowdesk_borrow_rates": FeedProfile(
        feed="iborrowdesk_borrow_rates", trigger=FeedTrigger.CONTINUOUS,
        cadence_days=1, freshness_max_age_days=5, skip_guard_days=1,
        targeting=Targeting.CONSTRAINED_DEMAND_DRIVEN,
        # iborrowdesk.com is a scraping target — no public API, anti-bot.
        # Polite cadence: 1 req per 3 sec.
        rate_limit_requests=20, rate_limit_period_seconds=60,
        concurrent_request_limit=1,
        quota_source_url="https://iborrowdesk.com",
        evidence="daily borrow rates; scrape-fragile + ticker-limited → "
                 "demand-driven targeting. 5d tolerates anti-bot skips. "
                 "Quota: anti-bot only; 20/min == 3s spacing for politeness.",
    ),
    "apewisdom_social_sentiment": FeedProfile(
        feed="apewisdom_social_sentiment", trigger=FeedTrigger.INTRADAY,
        cadence_days=1, freshness_max_age_days=7, skip_guard_days=1,
        targeting=Targeting.CONSTRAINED_DEMAND_DRIVEN,
        # ApeWisdom free API has NO documented hard limit. The adapter
        # paces at 1-2 req/s courtesy (per tpcore/apewisdom/adapter.py:11).
        # Declared 60/min == 1 rps which matches the lower end of that
        # courtesy band — conservative.
        rate_limit_requests=60, rate_limit_period_seconds=60,
        concurrent_request_limit=1,
        quota_source_url="https://apewisdom.io/api",
        evidence="ApeWisdom refreshes ~2h; coverage ceiling ~23% of "
                 "T1/T2 (1131 source tickers, 345 overlap) — see "
                 "social_sentiment_freshness (floor 0.15, evidence-derived). "
                 "Quota: vendor publishes none; adapter paces at 1-2 rps "
                 "courtesy (tpcore/apewisdom/adapter.py:11). Declared "
                 "60/min == 1 rps matches lower courtesy band.",
    ),
    "finnhub_insider_sentiment": FeedProfile(
        feed="finnhub_insider_sentiment", trigger=FeedTrigger.VENDOR_QUARTERLY,
        cadence_days=30, freshness_max_age_days=None, skip_guard_days=25,
        targeting=Targeting.CONSTRAINED_DEMAND_DRIVEN,
        # Finnhub free tier: 60 req/min sustained + 30 req/sec burst cap.
        # FeedProfile models only the per-minute cap; the per-second burst
        # (30/sec) is enforced inside the adapter via httpx Limits.
        # If consumer-side rate enforcement gets wired, both caps must apply.
        rate_limit_requests=60, rate_limit_period_seconds=60,
        concurrent_request_limit=1,
        quota_source_url="https://finnhub.io/docs/api/rate-limit",
        evidence="Finnhub MSPR is monthly-period (age measured in months "
                 "by its check); free-tier ticker-limited → demand-driven. "
                 "Quota: Finnhub free tier 60/min sustained + 30/s burst "
                 "(per-second cap not modeled in profile; enforce adapter-side).",
    ),
    # P0_3 RETIRE 2026-05-25 — ``insider_sentiment_daily`` FeedProfile
    # removed (target table ``platform.insider_filings`` was DROPPED in
    # migration 20260522_0200; redundant with the
    # ``sec_insider_transactions`` SEC-EDGAR Form-4 path).
    "greeks_max_pain": FeedProfile(
        feed="greeks_max_pain", trigger=FeedTrigger.MARKET_CLOSE,
        cadence_days=1, freshness_max_age_days=7, skip_guard_days=1,
        targeting=Targeting.CONSTRAINED_DEMAND_DRIVEN,
        # greeks.pro free tier: 10 req/min + 600 req/day + 1 symbol.
        # Corrected 2026-05-24 per the FeedProfile vendor-validation audit;
        # in-repo adapter docstring (tpcore/greeks/adapter.py:7) was the
        # authoritative source — earlier 6/min figure here was guesswork.
        rate_limit_requests=10, rate_limit_period_seconds=60,
        daily_request_quota=600,
        concurrent_request_limit=1,
        quota_source_url="https://greeks.pro",
        evidence="greeks.pro free tier = 1 symbol (SPY) by design; "
                 "demand-driven if it ever expands. Quota: 10/min + "
                 "600/day per tpcore/greeks/adapter.py (verified 2026-05-16).",
    ),
    "earnings_events": FeedProfile(
        feed="earnings_events", trigger=FeedTrigger.VENDOR_QUARTERLY,
        cadence_days=91, freshness_max_age_days=90, skip_guard_days=6,
        rate_limit_requests=300, rate_limit_period_seconds=60,
        daily_request_quota=100_000,
        concurrent_request_limit=1,
        quota_source_url="https://site.financialmodelingprep.com/developer/docs/pricing",
        evidence="earnings beats are quarterly; 90d max-age = ~one "
                 "earnings cycle (FMP). Quota: FMP Starter 300/min + "
                 "100k/day (shared with all FMP endpoints).",
    ),
    "sec_insider_transactions": FeedProfile(
        feed="sec_insider_transactions", trigger=FeedTrigger.CONTINUOUS,
        cadence_days=1, freshness_max_age_days=14, skip_guard_days=3,
        # SEC EDGAR declared rate limit: 10 req/s (HTTP 429 if exceeded).
        # Requires User-Agent identifying the requester.
        rate_limit_requests=10, rate_limit_period_seconds=1,
        concurrent_request_limit=1,
        quota_source_url="https://www.sec.gov/os/accessing-edgar-data",
        evidence="Form 4 ≥ daily, 8-K weekly across the universe; 14d "
                 "max-age. Bulk Form-345 ETL + full-history 8-K shipped. "
                 "Quota: SEC EDGAR enforced 10/s with mandatory "
                 "User-Agent; concurrency 1 — no batching.",
    ),
    "macro_indicators": FeedProfile(
        feed="macro_indicators", trigger=FeedTrigger.VENDOR_RELEASE,
        cadence_days=30, freshness_max_age_days=90, skip_guard_days=7,
        # FRED documented limit: 120 req/min. The per-series cadence varies
        # (VIX daily, claims weekly, INDPRO monthly) — see INDICATOR_CADENCE
        # + platform.series_catalog. Total INDICATOR_SERIES = ~62; one
        # full pull is well under the per-minute budget.
        rate_limit_requests=120, rate_limit_period_seconds=60,
        concurrent_request_limit=1,
        quota_source_url="https://fred.stlouisfed.org/docs/api/api_key.html",
        evidence="FRED series have PER-SERIES cadences (VIX daily, claims "
                 "weekly, INDPRO monthly). 90d blanket max-age covers the "
                 "slowest; the per-series publication-availability gate is "
                 "the phased refinement (facet 4) — now also in "
                 "platform.series_catalog. Quota: FRED 120/min.",
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
        # DERIVED — no external pull. Rate limits are N/A (recompute only
        # touches platform.macro_data + platform.prices_daily).
        evidence="computed from existing platform data (no external "
                 "pull); 3 NYSE sessions max-age. No vendor rate limit "
                 "(internal recompute only).",
    ),
    'fundamentals_quarterly': FeedProfile(
        feed='fundamentals_quarterly', trigger=FeedTrigger.VENDOR_RELEASE,
        cadence_days=91, freshness_max_age_days=120, skip_guard_days=6,
        rate_limit_requests=300, rate_limit_period_seconds=60,
        daily_request_quota=100_000,
        concurrent_request_limit=1,
        quota_source_url="https://site.financialmodelingprep.com/developer/docs/pricing",
        evidence='financial fundamentals (pb/de/revenue/net_income/fcf/etc) for value-engine setup detection — already ingested via FMP for months; formal ProviderBinding registration was missing (surfaced 2026-05-20 by the autonomous-self-heal P0 completeness invariant work). Quota: FMP Starter 300/min + 100k/day (shared with all FMP endpoints).',
    ),
    'corporate_actions': FeedProfile(
        feed='corporate_actions', trigger=FeedTrigger.VENDOR_RELEASE,
        cadence_days=1, freshness_max_age_days=7, skip_guard_days=1,
        # Alpaca /v2/corporate_actions is served by the TRADING API
        # (not the Market Data API — Alpaca has two separate rate-limit
        # buckets). Free/Basic tier: 200 req/min. Clarified 2026-05-24 per
        # FeedProfile vendor-validation audit.
        rate_limit_requests=200, rate_limit_period_seconds=60,
        concurrent_request_limit=1,
        quota_source_url="https://docs.alpaca.markets/docs/rate-limits#trading-api",
        evidence='splits + dividends from Alpaca /v2/corporate_actions (Trading API endpoint, NOT Market Data API — separate rate buckets) — already ingested for months; formal ProviderBinding registration was missing (surfaced 2026-05-20 by the autonomous-self-heal P0 completeness invariant work, mirroring the fundamentals_quarterly gap). Quota: Alpaca Trading API Basic 200/min.',
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
