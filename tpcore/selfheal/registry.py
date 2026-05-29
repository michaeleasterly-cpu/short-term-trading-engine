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

# Bounded targeted repair for the COMPLETENESS invariant: the
# daily_bars stage's repair_gaps mode re-pulls ONLY the
# invariant-flagged tickers (computed from the same _evaluate the
# completeness check uses → detector/healer can't disagree). Closes
# per-ticker completeness gaps in seconds.
_PRICES_REPAIR = {"repair_gaps": "true"}

# prices_daily_freshness goes red on staleness OR coverage_collapse.
# Three repair modes were tried 2026-05-17, only the third works:
#   1. repair_gaps — BLIND to coverage_collapse (derives targets from
#      the COMPLETENESS invariant, empty in this failure mode);
#      no-op'd a live 506/7,650 collapse. Fake-healable.
#   2. force_refresh active (whole universe) — TIMED OUT at the 3600s
#      stage cap, reaching only 6,910/7,650 in 60min. Re-pulling all
#      ~7,650 every cycle can't self-heal; the "could never self-heal"
#      caveat held even with the chunked endpoint.
#   3. repair_coverage — computes ONLY the tickers present on the
#      prior session but missing the target session and re-pulls just
#      those (747 = 8 chunks ≈ 6min). Bounded, deterministic,
#      detector/healer agree by construction. THIS is the real heal.
# Producer self-validation in _stage_daily_bars also fails the stage
# loudly on collapse, so this heal is the recovery path, not the only
# line of defence.
_PRICES_COVERAGE_REPAIR = {"repair_coverage": "true"}

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
    # Zero-tolerance live-DB-vs-archive shrinkage gate (the
    # BAMLH0A0HYM2 / vendor-truncation failure class). Healable via
    # the canonical ``corporate_actions`` Alpaca re-pull stage with
    # skip_guard disabled. Bounded by max_attempts=2. Spec:
    # docs/superpowers/specs/2026-05-20-corporate-actions-completeness-invariant.md.
    HealSpec(check_name="corporate_actions_completeness",
             source="corporate_actions",
             healable=True, stage="corporate_actions",
             params={"skip_guard_days": "0"}, max_attempts=2),
    # Re-pullable freshness, bounded canonical stage, real
    # skip_guard_days=0 force → honestly healable. A red means stale →
    # forced re-pull genuinely clears it.
    HealSpec(check_name="earnings_events_freshness", source="earnings_events",
             healable=True, stage="earnings_refresh",
             params={"skip_guard_days": "0"}, max_attempts=2),
    # Per-ticker zero-tolerance monotone-non-decrease invariant on
    # reported-earnings row counts (BEAT + NO_BEAT union) in
    # platform.earnings_events. Reported earnings rows are append-only —
    # any per-ticker rowcount drop vs the prior snapshot is vendor
    # truncation / deletion. Heal via the canonical earnings_refresh
    # stage with skip_guard_days=0 so the bounded re-pull actually fires.
    # Bounded by max_attempts=2. Baseline lives in
    # platform.earnings_events_count_snapshot (per-ticker PK; UPSERT on
    # PASS; the check's read+compare+UPSERT runs in a single tx so a
    # partial write can't poison the next cycle).
    # History: the prior BEAT-only KNOWN GAP (P1 follow-on) was resolved
    # 2026-05-20 by the NO_BEAT sentinel ingestion in
    # scripts/backfill_earnings_events.py — the invariant now gates on
    # truncation AND missed-detection from FMP outages.
    HealSpec(check_name="earnings_events_monotone",
             source="earnings_events",
             healable=True, stage="earnings_refresh",
             params={"skip_guard_days": "0"}, max_attempts=2),
    # {skip_guard_days:0} was a FAKE heal: _stage_sec_filings never
    # overlaid cfg on the default path so the param was silently
    # dropped, AND defaults (max_tickers=200, lookback=90) cannot clear
    # insufficient_stock_coverage (≥30% of ~1,500 stocks / 180d).
    # `repair` triggers the full-universe, 200d, skip-guard-off re-pull.
    HealSpec(check_name="sec_filings_freshness", source="sec_insider_transactions",
             healable=True, stage="sec_filings",
             params={"repair": "true"}, max_attempts=2),
    # Per-ticker zero-tolerance monotone-non-decrease invariant on
    # platform.sec_insider_transactions. Form 4 is append-only — any
    # per-ticker rowcount drop vs the prior snapshot is vendor
    # truncation / deletion. Heal via the same canonical sec_filings
    # `repair=true` stage the freshness check already uses (full T1+T2
    # stock universe, 200d lookback, skip-guard off) — that's the
    # broad re-pull most likely to restore truncated rows. Bounded by
    # max_attempts=2. Baseline lives in
    # platform.sec_insider_row_counts_snapshot (per-ticker PK; UPSERT
    # on PASS; the check's read+compare+UPSERT runs in a single tx so
    # a partial write can't poison the next cycle).
    HealSpec(check_name="sec_insider_monotone",
             source="sec_insider_transactions",
             healable=True, stage="sec_filings",
             params={"repair": "true"}, max_attempts=2),
    HealSpec(check_name="macro_indicators_freshness", source="macro_indicators",
             healable=True, stage="macro_indicators",
             params={"skip_guard_days": "0"}, max_attempts=2),
    # The completeness invariant catches gaps INSIDE the active range
    # of each FRED series (the 2026-05-15 BAMLH0A0HYM2 truncation class
    # — freshness stays green when latest_date is current but the
    # mid-range is gutted). Heal via the same canonical
    # ``macro_indicators`` stage with skip-guard off; the stage already
    # re-pulls all 7 series (universe = the 7 series), so per-indicator
    # subsetting is not meaningful at the stage level. Bounded by
    # max_attempts=2. Spec:
    # docs/superpowers/specs/2026-05-20-macro-indicators-completeness-invariant.md.
    HealSpec(check_name="macro_indicators_completeness", source="macro_indicators",
             healable=True, stage="macro_indicators",
             params={"skip_guard_days": "0"}, max_attempts=2),
    # Per-ticker quarterly-gap completeness: every consecutive pair of
    # period_end_date rows for T1/T2 live stocks is ≤100 days apart
    # (math-derived bound: Q4=92 days + 8-day slack). A gap > 100 days
    # is a missing quarter — the engines silently lose a quarter's
    # signal even though fundamentals_integrity is GREEN (each row is
    # well-formed). Heal via the canonical ``fundamentals_refresh``
    # stage with skip-guard off; bounded by max_attempts=2. Spec:
    # docs/superpowers/specs/2026-05-20-fundamentals-quarterly-completeness-invariant.md.
    HealSpec(check_name="fundamentals_quarterly_completeness",
             source="fundamentals_quarterly",
             healable=True, stage="fundamentals_refresh",
             params={"skip_guard_days": "0"}, max_attempts=2),
    # Force param added to tier_refresh / classify_tickers
    # (skip_guard_days=0) → now honestly healable via canonical re-run.
    HealSpec(check_name="liquidity_tiers_freshness", source="liquidity_tiers",
             healable=True, stage="tier_refresh",
             params={"skip_guard_days": "0"}, max_attempts=2),
    # Universe-survives-the-cut completeness invariant on
    # platform.liquidity_tiers. The table is DERIVED + RECOMPUTED
    # quarterly by tier_refresh — rows are NOT append-only, so the
    # per-ticker monotone-non-decrease pattern (sec_insider/earnings)
    # does NOT apply (a recompute can legitimately drop a delisted
    # ticker). The correct invariant is universe coverage: every
    # active-universe stock (stock asset_class + active in trailing
    # 30 NYSE sessions) must have a row. ONE missing → FAIL. Heal via
    # the same canonical tier_refresh stage with skip_guard_days=0
    # the freshness HealSpec already uses; bounded by max_attempts=2.
    HealSpec(check_name="liquidity_tiers_completeness", source="liquidity_tiers",
             healable=True, stage="tier_refresh",
             params={"skip_guard_days": "0"}, max_attempts=2),
    HealSpec(check_name="ticker_classifications_coverage", source="ticker_classifications",
             healable=True, stage="classify_tickers",
             params={"skip_guard_days": "0"}, max_attempts=2),
    HealSpec(check_name="prices_daily_freshness", source="prices_daily",
             healable=True, stage="daily_bars",
             params=dict(_PRICES_COVERAGE_REPAIR), max_attempts=3),
    HealSpec(check_name="prices_daily_completeness", source="prices_daily",
             healable=True, stage="daily_bars", params=dict(_PRICES_REPAIR),
             max_attempts=3),
    # Path-A FK closure: every prices_daily row must have a non-NULL
    # classification_id. Healable via sec_orphan_resolve which runs the
    # 3-phase deterministic cascade (truth-set CIK -> EDGAR direct ->
    # OpenFIGI + FMP fallback). Phase A+B+C achieved 100% closure live
    # 2026-05-24. Re-runs are idempotent (ON CONFLICT DO NOTHING) so the
    # heal cycle is safe regardless of how many orphans accumulate.
    # Bounded by max_attempts=2 (the resolver is per-ticker; if a heal
    # pass leaves residue, that's truly-unresolvable + needs operator
    # manual review per the foreign-ADR / SPAC-warrant / bankruptcy-
    # shell categorization).
    HealSpec(check_name="prices_daily_classification_id_completeness",
             source="prices_daily",
             healable=True, stage="sec_orphan_resolve",
             params={"phase_b": "true", "phase_c": "true"},
             max_attempts=2),
    # SCD-2 / bitemporal integrity checks added 2026-05-25 after the
    # META-was-tip-of-iceberg audit found 2,061 overlap pairs across
    # the corp-history substrate. ESCALATE-ONLY: these substrates
    # are derived (no vendor feed), so adding them to FEED_PROFILES /
    # ProviderBinding would be incorrect. Operators run the bounded
    # heal stages manually:
    #   issuer_history / issuer_securities → `issuer_history_cleanup`
    #   corporate_events → `audit_cleanup_2026_05_24`
    #   ticker_history → GIST exclude constraint enforces at runtime;
    #     any defect indicates an upstream loader bug needing review.
    HealSpec(check_name="issuer_history_integrity",
             source="issuer_history",
             healable=False,
             unhealable_reason=(
                 "Derived substrate, no vendor feed. Run "
                 "`ops.py --stage issuer_history_cleanup --param dry_run=false` "
                 "to repair (window-function chain)."
             )),
    HealSpec(check_name="issuer_securities_integrity",
             source="issuer_securities",
             healable=False,
             unhealable_reason=(
                 "Derived substrate, no vendor feed. Run "
                 "`ops.py --stage issuer_history_cleanup --param dry_run=false` "
                 "(same stage; covers issuer_securities partition)."
             )),
    HealSpec(check_name="corporate_events_integrity",
             source="corporate_events",
             healable=False,
             unhealable_reason=(
                 "Derived substrate, no vendor feed. Run "
                 "`ops.py --stage audit_cleanup_2026_05_24 --param dry_run=false` "
                 "to close older bitemporal versions' realtime_end."
             )),
    HealSpec(check_name="ticker_history_integrity",
             source="ticker_history",
             healable=False,
             unhealable_reason=(
                 "GIST exclude constraint is the runtime enforcer; "
                 "any defect here indicates an upstream loader bug "
                 "that needs operator review, not routine drift"
             )),
    # Options max-pain freshness — TEMPORARILY UNHEALABLE 2026-05-29
    # while greeks.pro account access is broken (operator can't log in to
    # rotate the revoked API key). The greeks_max_pain stage was
    # operator-disabled to a no-op stub in scripts/ops.py:5686-5709 on
    # 2026-05-28 so the cron stops 401-looping; that means the canonical
    # heal stage will succeed-but-do-nothing every call, leaving the
    # freshness check perma-red. Reclassify as healable=False with a
    # documented operator action so the cascade emits one INFO-level
    # UNHEALABLE acknowledgement per cycle instead of looping a no-op
    # refresh forever. REVERT in the same commit as re-enabling
    # _stage_greeks_max_pain (restore healable=True, stage="greeks_max_pain",
    # params={"skip_guard": "false"}, max_attempts=2).
    HealSpec(check_name="options_max_pain_freshness",
             source="greeks_max_pain",
             healable=False,
             unhealable_reason=(
                 "greeks.pro account access disabled 2026-05-29 "
                 "(operator portal login broken; email sent to vendor). "
                 "The greeks_max_pain stage is operator-disabled to a "
                 "no-op stub (scripts/ops.py:5686-5709) so the canonical "
                 "heal cannot actually refresh data. REVERT this entry "
                 "back to healable=True in the same commit that restores "
                 "the stage when greeks.pro access is back."
             )),
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
    # Fear & Greed is a DERIVED index — no external provider. The 2026
    # -05-17 audit flagged it "fake-healable" assuming isolated heal;
    # VERIFIED otherwise: handle_fear_greed recomputes from
    # macro_indicators (VIX/hy_spread/yield_curve) + prices_daily (SPY),
    # and the orchestrator iterates (max_iterations=4) — so its
    # upstreams heal in an earlier pass and its recompute succeeds in a
    # later one. healable=True is CORRECT and must stay: marking it
    # healable=False would (per `if unhealable: return`) make a routine
    # stale-fear_greed escalate the ENTIRE data layer and heal nothing.
    # depends_on makes the upstream contract explicit + test-enforced
    # (the fear_greed-class guard).
    HealSpec(check_name="fear_greed_freshness", source="fear_greed",
             healable=True, stage="fear_greed", params={}, max_attempts=2,
             depends_on=("macro_indicators", "prices_daily")),
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
    # P0_3 RETIRE 2026-05-25 — ``insider_filings_freshness`` HealSpec
    # removed alongside the dropped ``platform.insider_filings`` table.
    # The validation check, FeedProfile, ProviderBinding, producer
    # adapter, and ops stages were all retired in the same PR.
    # P0 trust-audit (2026-05-25): meta-monitor on daemon liveness.
    # Stops the silent-stall failure class where data_operations /
    # engine_service / allocator died days ago but nothing alerted
    # because no check covered daemon liveness. healable=False —
    # no canonical ops.py stage restarts a daemon process; the only
    # correct heal is operator launchd / systemd restart of the named
    # daemon (the FailureDetail names which daemon is stale and by
    # how long, so the escalation is directly actionable).
    HealSpec(check_name="daemon_freshness", source="daemon_heartbeats",
             healable=False, unhealable_reason=(
                 "daemon-process liveness — heal requires operator "
                 "restart of the named daemon via launchd / systemd; "
                 "no canonical ops.py stage restarts daemons. The "
                 "FailureDetail names which daemon is stale and by "
                 "how long, so the escalation is directly actionable"
             )),
    # P0 trust-audit (2026-05-25): meta-monitor on the lane's gate
    # emission. Catches the case where the lane has been red for so
    # long DATA_OPERATIONS_COMPLETE never fires, but no individual
    # check covered the absence of the gate event itself. healable=
    # False — the only way to re-emit is to clear every other red so
    # the gate fires; no single ops.py stage produces the event.
    HealSpec(check_name="data_operations_complete_cadence",
             source="application_log",
             healable=False, unhealable_reason=(
                 "lane-emission meta-monitor — DATA_OPERATIONS_COMPLETE "
                 "is the END product of a fully-green data lane run; "
                 "no canonical ops.py stage emits it directly. RED "
                 "means the lane has not reached 100% green within "
                 "the cadence window; clear the other reds and the "
                 "gate fires"
             )),
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
