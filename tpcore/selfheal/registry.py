"""The single HealSpec registry — one entry per validation check.

Clockwork guarantee: ``test_selfheal`` asserts the registry key set is
EXACTLY ``suite.KNOWN_CHECK_NAMES``. Adding a validation check (i.e.
onboarding a data feed per the 6-stage contract) therefore *fails the
build* until a deliberate HealSpec decision is recorded here —
``healable`` with a bounded canonical repair, or ``healable=False``
with an honest ``unhealable_reason``. You cannot ship a feed and
forget self-heal; the registry forces the choice.

Per-source rollout (TODO #132): non-prices feeds currently carry an
honest ``healable=False`` spec (detected + hard-gated, escalates to the
operator). As each gets a bounded targeted repair mode on its canonical
stage (the ``repair_gaps`` pattern), flip its spec to ``healable=True``
— a one-line change here, zero orchestrator edits.
"""
from __future__ import annotations

from tpcore.quality.validation.suite import KNOWN_CHECK_NAMES

from .spec import HealSpec

# Bounded targeted repair shared by both prices checks: the daily_bars
# stage's repair_gaps mode re-pulls ONLY the invariant-flagged tickers
# (computed from the same _evaluate the checks use → detector/healer
# can't disagree). Proven 2026-05-15: closes live gaps in seconds vs a
# whole-universe force_refresh that times out at 3600s.
_PRICES_REPAIR = {"repair_gaps": "true"}

# Honest disposition is per failure-CLASS, not a blanket placeholder.
#
# _PENDING is honest ONLY for genuinely re-pullable freshness/coverage
# feeds whose targeted repair mode is still being rolled out (#132) —
# a heal really is coming.
_PENDING = (
    "no bounded targeted repair spec yet — detected + hard-gated "
    "(blocks the emit / engine sweep) and escalates to the operator; "
    "P0 #132 per-source rollout (flip to healable when its repair mode "
    "lands)"
)

# _CORRUPTION: physical-truth/integrity failure (NULLs, impossible
# dates, nonpositive shares, bad ratios). A red is bad rows ALREADY in
# the table — re-pull cannot honestly fix it and a blind bulk re-pull
# could destroy correct data. Permanently healable=False by nature;
# the only correct "heal" is operator investigation. Not a rollout gap.
_CORRUPTION = (
    "data-corruption / physical-truth class, not a missing-data gap — "
    "must be investigated, never bulk re-pulled blindly; healable=False "
    "is permanent and honest, NOT pending a rollout"
)

# _SOURCE_OF_TRUTH: our data disagrees with an authoritative reference
# (S&P constituent set / known delistings / known splits). A red is a
# reconciliation discrepancy, not staleness — re-pulling the feed
# cannot reconcile it. Permanently healable=False; escalate to
# investigate which side is wrong.
_SOURCE_OF_TRUTH = (
    "discrepancy vs an authoritative source-of-truth (constituents / "
    "delistings / splits) — a reconciliation failure, not staleness; "
    "re-pull cannot fix it. healable=False is permanent and honest"
)

# _NEEDS_FORCE_PARAM: genuinely re-pullable freshness/coverage, BUT
# its canonical stage takes no config and has no skip-guard-bypass
# param — the orchestrator cannot force a re-pull, so a healable=True
# spec would silently no-op and infinite-retry (fake-green). Honest
# until the stage gains a force param (#132 per-feed work), then flip.
_NEEDS_FORCE_PARAM = (
    "re-pullable in principle, but the canonical stage exposes no "
    "skip-guard-bypass --param yet, so the orchestrator cannot force "
    "the repair (a healable spec would silently no-op → infinite "
    "retry). Flip to healable once the stage gains a force param "
    "(#132 per-feed work) — NOT a fake-green now"
)

# Explicit, exhaustive. Order mirrors suite.KNOWN_CHECK_NAMES for
# symmetry / easy diffing.
_SPECS: tuple[HealSpec, ...] = (
    HealSpec(check_name="delistings", source="delistings",
             healable=False, unhealable_reason=_SOURCE_OF_TRUTH),
    HealSpec(check_name="constituent", source="sp500_constituents",
             healable=False, unhealable_reason=_SOURCE_OF_TRUTH),
    HealSpec(check_name="splits", source="splits",
             healable=False, unhealable_reason=_SOURCE_OF_TRUTH),
    HealSpec(check_name="row_integrity", source="prices_daily",
             healable=False, unhealable_reason=_CORRUPTION),
    HealSpec(check_name="fundamentals_integrity", source="fundamentals_quarterly",
             healable=False, unhealable_reason=_CORRUPTION),
    HealSpec(check_name="corporate_actions_integrity", source="corporate_actions",
             healable=False, unhealable_reason=_CORRUPTION),
    # Re-pullable freshness, bounded canonical stage, real
    # skip_guard_days=0 force → honestly healable. A red means stale →
    # forced re-pull genuinely clears it.
    HealSpec(check_name="earnings_events_freshness", source="earnings_events",
             healable=True, stage="earnings_refresh",
             params={"skip_guard_days": "0"}, max_attempts=2),
    HealSpec(check_name="sec_filings_freshness", source="sec_insider_transactions",
             healable=True, stage="sec_filings",
             params={"skip_guard_days": "0"}, max_attempts=2),
    HealSpec(check_name="macro_indicators_freshness", source="macro_indicators",
             healable=True, stage="macro_indicators",
             params={"skip_guard_days": "0"}, max_attempts=2),
    # Force param added to tier_refresh / classify_tickers
    # (skip_guard_days=0) → now honestly healable via canonical re-run.
    HealSpec(check_name="liquidity_tiers_freshness", source="liquidity_tiers",
             healable=True, stage="tier_refresh",
             params={"skip_guard_days": "0"}, max_attempts=2),
    HealSpec(check_name="ticker_classifications_coverage", source="ticker_classifications",
             healable=True, stage="classify_tickers",
             params={"skip_guard_days": "0"}, max_attempts=2),
    HealSpec(check_name="prices_daily_freshness", source="prices_daily",
             healable=True, stage="daily_bars", params=dict(_PRICES_REPAIR),
             max_attempts=3),
    HealSpec(check_name="prices_daily_completeness", source="prices_daily",
             healable=True, stage="daily_bars", params=dict(_PRICES_REPAIR),
             max_attempts=3),
    # A stale max-pain snapshot is fixed by re-running the bounded
    # canonical stage (1 symbol, 1 idempotent API call) — genuinely
    # healable, not escalate-only. force the skip-guard off so the
    # heal actually re-pulls.
    HealSpec(check_name="options_max_pain_freshness", source="greeks_max_pain",
             healable=True, stage="greeks_max_pain",
             params={"skip_guard": "false"}, max_attempts=2),
    # Stale insider-sentiment is fixed by re-running the bounded
    # canonical stage with the monthly skip-guard disabled.
    HealSpec(check_name="insider_sentiment_freshness",
             source="finnhub_insider_sentiment",
             healable=True, stage="finnhub_insider_sentiment",
             params={"skip_guard_days": "0"}, max_attempts=2),
    # Stale/low-coverage social sentiment → re-run the bounded stage
    # with the 24h skip-guard disabled.
    HealSpec(check_name="social_sentiment_freshness",
             source="apewisdom_social_sentiment",
             healable=True, stage="apewisdom_social_sentiment",
             params={"skip_guard_hours": "0"}, max_attempts=2),
    # Stale Fear & Greed → recompute via the bounded canonical stage
    # (reads existing platform data; no external pull).
    HealSpec(check_name="fear_greed_freshness", source="fear_greed",
             healable=True, stage="fear_greed", params={}, max_attempts=2),
    # Stale short interest / borrow rates → re-run the bounded
    # canonical stage with the skip-guard disabled.
    HealSpec(check_name="short_interest_freshness", source="finra_short_interest",
             healable=True, stage="finra_short_interest",
             params={"skip_guard_days": "0"}, max_attempts=2),
    HealSpec(check_name="borrow_rates_freshness", source="iborrowdesk_borrow_rates",
             healable=True, stage="iborrowdesk_borrow_rates",
             params={"skip_guard_hours": "0"}, max_attempts=2),
    HealSpec(check_name="aaii_sentiment_freshness", source="aaii_sentiment",
             healable=True, stage="aaii_sentiment",
             params={"skip_guard_days": "0"}, max_attempts=2),
)

HEAL_SPECS: dict[str, HealSpec] = {s.check_name: s for s in _SPECS}


def spec_for(check_name: str) -> HealSpec | None:
    """Return the HealSpec for a validation check name, or None if the
    check is unknown to the registry (treated as escalate — an unknown
    red must never be silently ignored)."""
    return HEAL_SPECS.get(check_name)


def registry_drift() -> tuple[set[str], set[str]]:
    """(missing, extra) vs suite.KNOWN_CHECK_NAMES. Both empty == in
    lockstep. Used by the clockwork coverage test."""
    known = set(KNOWN_CHECK_NAMES)
    have = set(HEAL_SPECS)
    return known - have, have - known


__all__ = ["HEAL_SPECS", "registry_drift", "spec_for"]
