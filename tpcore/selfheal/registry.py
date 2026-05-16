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

_PENDING = (
    "no bounded targeted repair spec yet — detected + hard-gated "
    "(blocks the emit / engine sweep) and escalates to the operator; "
    "P0 #132 per-source rollout (flip to healable when its repair mode "
    "lands)"
)

# Explicit, exhaustive. Order mirrors suite.KNOWN_CHECK_NAMES for
# symmetry / easy diffing.
_SPECS: tuple[HealSpec, ...] = (
    HealSpec(check_name="delistings", source="delistings",
             healable=False, unhealable_reason=_PENDING),
    HealSpec(check_name="constituent", source="sp500_constituents",
             healable=False, unhealable_reason=_PENDING),
    HealSpec(check_name="splits", source="splits",
             healable=False, unhealable_reason=_PENDING),
    HealSpec(check_name="row_integrity", source="prices_daily",
             healable=False,
             unhealable_reason=(
                 "row_integrity failure is a data-corruption class, not a "
                 "missing-bars gap — must be investigated, never bulk "
                 "re-pulled blindly")),
    HealSpec(check_name="fundamentals_integrity", source="fundamentals_quarterly",
             healable=False, unhealable_reason=_PENDING),
    HealSpec(check_name="corporate_actions_integrity", source="corporate_actions",
             healable=False, unhealable_reason=_PENDING),
    HealSpec(check_name="catalyst_events_freshness", source="catalyst_events",
             healable=False, unhealable_reason=_PENDING),
    HealSpec(check_name="sec_filings_freshness", source="sec_insider_transactions",
             healable=False, unhealable_reason=_PENDING),
    HealSpec(check_name="liquidity_tiers_freshness", source="liquidity_tiers",
             healable=False, unhealable_reason=_PENDING),
    HealSpec(check_name="ticker_classifications_coverage", source="ticker_classifications",
             healable=False, unhealable_reason=_PENDING),
    HealSpec(check_name="macro_indicators_freshness", source="macro_indicators",
             healable=False, unhealable_reason=_PENDING),
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
