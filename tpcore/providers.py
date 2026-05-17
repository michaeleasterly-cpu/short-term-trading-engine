"""Data-provider binding registry — the snap-in/out control surface.

Flat single-source-of-truth, symmetric to ``tpcore.engine_profile`` /
``tpcore.risk.limits_profile`` / ``tpcore.feeds.FeedProfile`` /
``tpcore.selfheal.HealSpec``. Decouples **feed** (the logical data need;
what consumers reference via ``DataProviderInterface``) from
**provider** (a concrete source + adapter that satisfies it).

Phase 1 of the Data Provider Lifecycle (spec
``docs/superpowers/specs/2026-05-17-data-provider-lifecycle-design.md``,
plan ``…/plans/2026-05-17-data-provider-lifecycle-plan.md``). **Landed
dark**: nothing in the runtime/ingest path imports this in Phase 1 —
it records *current reality* and is the SoT the later CUTOVER/EVALUATE
phases act on. Same model as ``engine_profile`` Sub-project A.

Bindings are EVIDENCE-DERIVED (read out of each handler/adapter), never
assumed — the same discipline as ``HealSpec.depends_on`` and the
``FeedProfile`` evidence strings. Today every feed has exactly one
ACTIVE provider and no fallbacks; Phase 4 adds parity-verified
candidates.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class ProviderStatus(StrEnum):
    """Lifecycle status of one (feed, provider) binding."""

    CANDIDATE = "candidate"    # proposed; not serving
    ACTIVE = "active"          # the one serving the feed now
    FALLBACK = "fallback"      # parity-verified; cutover-ready standby
    DEPRECATED = "deprecated"  # scheduled for retirement
    RETIRED = "retired"        # offboarded; kept for provenance only


class ProviderBinding(BaseModel):
    """One (feed, provider) binding. Frozen — the registry is a SoT."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Logical feed — the FeedProfile / HealSpec.source vocabulary.
    feed: str
    # Concrete provider identity ("alpaca", "fred", "internal", …).
    provider: str
    # Dotted path to the CURRENT ingest entrypoint for this binding.
    # Phase 1 records the true entrypoint (function/stage); the
    # DataProviderInterface conformance is an ONBOARD-gate concern for
    # NEW providers (spec §4 stage 3), not retrofitted onto the SoT.
    adapter_module: str
    status: ProviderStatus
    # WHY this binding/status — no-vendor-blame discipline (mirrors
    # FeedProfile.evidence). How the provider was determined; for
    # derived feeds, what it is computed from.
    evidence: str
    # Last EVALUATE data-parity pass vs the incumbent. Required for a
    # FALLBACK (it cannot stand in without a parity pass) — enforced
    # now even though the parity gate itself lands in Phase 2.
    parity_verified_at: date | None = None

    def model_post_init(self, _ctx: object) -> None:  # noqa: D401
        if self.status is ProviderStatus.FALLBACK and self.parity_verified_at is None:
            raise ValueError(
                f"ProviderBinding[{self.feed}/{self.provider}]: FALLBACK "
                f"requires parity_verified_at (a standby must be parity-"
                f"verified vs the incumbent before it can be cut over)"
            )
        if not self.evidence.strip():
            raise ValueError(
                f"ProviderBinding[{self.feed}/{self.provider}]: evidence "
                f"is mandatory (no-vendor-blame discipline)"
            )


# Evidence-derived from each handler/adapter (read, not assumed).
# Exactly one ACTIVE per feed; no fallbacks yet (Phase 4). Feed set ==
# tpcore.feeds.FEED_PROFILES keys (the drift test enforces both ways).
_BINDINGS: tuple[ProviderBinding, ...] = (
    ProviderBinding(
        feed="prices_daily", provider="alpaca",
        adapter_module="tpcore.data.ingest_alpaca_bars",
        status=ProviderStatus.ACTIVE,
        evidence="Alpaca /v2/stocks/bars multi-symbol; feed=iex (free "
                 "tier has no SIP entitlement — verified 2026-05-17).",
    ),
    ProviderBinding(
        feed="macro_indicators", provider="fred",
        adapter_module="tpcore.ingestion.handlers.handle_macro_indicators",
        status=ProviderStatus.ACTIVE,
        evidence="FRED series (INDICATOR_SERIES), pulled per-series with "
                 "skip_guard. hy_spread (BAMLH0A0HYM2) is subject to FRED "
                 "rolling-window truncation (the BAMLH0A0HYM2 incident); "
                 "the eco_archive CANDIDATE below is the recovery path.",
    ),
    # Phase 4: the ONE real alternative for this feed (no others exist —
    # the registry is not padded with fictitious fallbacks). Honest
    # CANDIDATE, NOT FALLBACK: a FALLBACK requires parity_verified_at
    # and "cutover-ready standby" semantics. This is the
    # hist_csv_path/hist_indicator recovery path that reloaded
    # BAMLH0A0HYM2 1996-2021 (eco-archive + Scribd fred-graph gap),
    # validated 772/772 EXACT on 2026-05-16 — parity-grade accuracy on
    # the historical overlap. It is NOT a live drop-in: it serves the
    # historical span only and does NOT keep the recent tail fresh
    # (FRED does). A true FALLBACK would be a hybrid (eco-archive
    # history + FRED live tail) — a future EVALUATE/ONBOARD, not
    # claimable today. CANDIDATE needs no parity_verified_at, so this
    # records the real recovery capability without fabricating a
    # cutover-ready date.
    ProviderBinding(
        feed="macro_indicators", provider="eco_archive",
        adapter_module="tpcore.ingestion.handlers._ingest_macro_hist_csv",
        status=ProviderStatus.CANDIDATE,
        evidence="Static-history recovery for hy_spread (BAMLH0A0HYM2) "
                 "when FRED truncates: loads the eco-archive + Scribd "
                 "fred-graph CSV (1996-2021), validated 772/772 EXACT "
                 "2026-05-16. CANDIDATE not FALLBACK — covers the "
                 "historical span only, does not keep the live tail "
                 "fresh; a full fallback (hybrid history+live tail) is a "
                 "future EVALUATE/ONBOARD.",
    ),
    ProviderBinding(
        feed="earnings_events", provider="fmp",
        adapter_module="scripts.ops._stage_earnings_refresh",
        status=ProviderStatus.ACTIVE,
        evidence="FMP earnings beats (weekly refresh; stock universe only).",
    ),
    ProviderBinding(
        feed="sec_insider_transactions", provider="sec_edgar",
        adapter_module="tpcore.ingestion.handlers.handle_sec_filings",
        status=ProviderStatus.ACTIVE,
        evidence="SEC EDGAR — bulk Form-345 datasets (insider) + 8-K "
                 "(material events).",
    ),
    ProviderBinding(
        feed="finra_short_interest", provider="finra",
        adapter_module="tpcore.ingestion.handlers.handle_finra_short_interest",
        status=ProviderStatus.ACTIVE,
        evidence="FINRA bi-monthly short-interest; 60d window covers the "
                 "latest ~3 settlement periods.",
    ),
    ProviderBinding(
        feed="apewisdom_social_sentiment", provider="apewisdom",
        adapter_module="tpcore.ingestion.handlers.handle_apewisdom_social_sentiment",
        status=ProviderStatus.ACTIVE,
        evidence="ApeWisdom API; ~23% measured coverage ceiling (floor "
                 "set at 15% from that evidence).",
    ),
    ProviderBinding(
        feed="iborrowdesk_borrow_rates", provider="iborrowdesk",
        adapter_module="tpcore.ingestion.handlers.handle_iborrowdesk_borrow_rates",
        status=ProviderStatus.ACTIVE,
        evidence="IBorrowDesk scrape (per-ticker); source-side blocks "
                 "degrade gracefully → escalation, not silent green.",
    ),
    ProviderBinding(
        feed="aaii_sentiment", provider="aaii",
        adapter_module="tpcore.ingestion.handlers.handle_aaii_sentiment",
        status=ProviderStatus.ACTIVE,
        evidence="AAII weekly sentiment workbook (full-history, "
                 "idempotent); vendor-anchored freshness (Thu publish).",
    ),
    ProviderBinding(
        feed="finnhub_insider_sentiment", provider="finnhub",
        adapter_module="tpcore.ingestion.handlers.handle_finnhub_insider_sentiment",
        status=ProviderStatus.ACTIVE,
        evidence="Finnhub insider-sentiment, full T1/T2 stock universe "
                 "loop; monthly cadence.",
    ),
    ProviderBinding(
        feed="greeks_max_pain", provider="tradier",
        adapter_module="tpcore.ingestion.handlers.handle_greeks_max_pain",
        status=ProviderStatus.ACTIVE,
        evidence="Max-pain computed from platform.tradier_options_chains "
                 "(Tradier options chains); SPY only.",
    ),
    ProviderBinding(
        feed="ticker_classifications", provider="alpaca",
        adapter_module="tpcore.data.classify_tickers.classify_all_tickers",
        status=ProviderStatus.ACTIVE,
        evidence="Derived from the Alpaca assets list (asset_class via "
                 "name/symbol heuristics) — no separate classifier vendor.",
    ),
    ProviderBinding(
        feed="liquidity_tiers", provider="internal",
        adapter_module="scripts.ops._stage_tier_refresh",
        status=ProviderStatus.ACTIVE,
        evidence="DERIVED internally from prices_daily (price/volume) + "
                 "spread_observations — no external vendor.",
    ),
    ProviderBinding(
        feed="fear_greed", provider="internal",
        adapter_module="tpcore.ingestion.handlers.handle_fear_greed",
        status=ProviderStatus.ACTIVE,
        evidence="DERIVED internally from macro_indicators (VIX/hy/yield) "
                 "+ prices_daily (SPY) — no external vendor; depends_on "
                 "those feeds (see HealSpec).",
    ),
)


PROVIDER_BINDINGS: dict[str, list[ProviderBinding]] = defaultdict(list)
for _b in _BINDINGS:
    PROVIDER_BINDINGS[_b.feed].append(_b)


def bindings_for(feed: str) -> list[ProviderBinding]:
    """All bindings for ``feed`` (any status). Empty if none."""
    return list(PROVIDER_BINDINGS.get(feed, []))


def active_provider(feed: str) -> ProviderBinding | None:
    """The single ACTIVE binding for ``feed`` (None if unbound)."""
    for b in PROVIDER_BINDINGS.get(feed, []):
        if b.status is ProviderStatus.ACTIVE:
            return b
    return None


def all_feeds() -> set[str]:
    """Every feed with at least one binding."""
    return set(PROVIDER_BINDINGS)


__all__ = [
    "PROVIDER_BINDINGS",
    "ProviderBinding",
    "ProviderStatus",
    "active_provider",
    "all_feeds",
    "bindings_for",
]
