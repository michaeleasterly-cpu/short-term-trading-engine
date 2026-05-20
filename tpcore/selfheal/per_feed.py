"""Per-feed validate + bounded self-heal — the on-completion unit.

Phase 1 of the per-feed validate-on-completion design (spec
``docs/superpowers/specs/2026-05-17-per-feed-validate-on-completion-design.md``).
**Landed dark**: pure helpers, no caller in the cycle yet (Phase 2
wires the leaf-feed hook; Phase 3 the derived-feed ordering).

The check IS the validator — `validate_one` runs the *canonical*
`check_<feed>` (not a bespoke guard), so the producer self-validation
and the suite cannot drift. `heal_one` is the single-check counterpart
of `run_self_heal`: it runs ONE feed's bounded HealSpec repair (the
orchestrator has no source-subset API — verified — so this reuses the
HealSpec + an injected canonical runner; the orchestrator is
untouched). `healable=False` feeds escalate, never heal (honest,
matches the registry).

A drift test asserts the check registry == ``suite.KNOWN_CHECK_NAMES``
(clockwork: a new check fails the build until it is mapped here too).
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import structlog

from tpcore.feeds.dispatcher import FEED_STAGE
from tpcore.quality.validation.checks.aaii_sentiment_freshness import (
    check_aaii_sentiment_freshness,
)
from tpcore.quality.validation.checks.borrow_rates_freshness import (
    check_borrow_rates_freshness,
)
from tpcore.quality.validation.checks.constituent import check_constituent_snapshot
from tpcore.quality.validation.checks.corporate_actions_integrity import (
    check_corporate_actions_integrity,
)
from tpcore.quality.validation.checks.delistings import check_delistings
from tpcore.quality.validation.checks.earnings_events_freshness import (
    check_earnings_events_freshness,
)
from tpcore.quality.validation.checks.fear_greed_freshness import (
    check_fear_greed_freshness,
)
from tpcore.quality.validation.checks.fundamentals_integrity import (
    check_fundamentals_integrity,
)
from tpcore.quality.validation.checks.insider_sentiment_freshness import (
    check_insider_sentiment_freshness,
)
from tpcore.quality.validation.checks.liquidity_tiers_freshness import (
    check_liquidity_tiers_freshness,
)
from tpcore.quality.validation.checks.macro_indicators_completeness import (
    check_macro_indicators_completeness,
)
from tpcore.quality.validation.checks.macro_indicators_freshness import (
    check_macro_indicators_freshness,
)
from tpcore.quality.validation.checks.options_max_pain_freshness import (
    check_options_max_pain_freshness,
)
from tpcore.quality.validation.checks.prices_daily_completeness import (
    check_prices_daily_completeness,
)
from tpcore.quality.validation.checks.prices_daily_freshness import (
    check_prices_daily_freshness,
)
from tpcore.quality.validation.checks.row_integrity import check_row_integrity
from tpcore.quality.validation.checks.sec_filings_freshness import (
    check_sec_filings_freshness,
)
from tpcore.quality.validation.checks.short_interest_freshness import (
    check_short_interest_freshness,
)
from tpcore.quality.validation.checks.social_sentiment_freshness import (
    check_social_sentiment_freshness,
)
from tpcore.quality.validation.checks.splits import check_splits
from tpcore.quality.validation.checks.ticker_classifications_freshness import (
    check_ticker_classifications_coverage,
)
from tpcore.quality.validation.models import CheckResult
from tpcore.quality.validation.sources.constituents import FixtureConstituentSource
from tpcore.quality.validation.sources.delistings import FixtureDelistingsSource
from tpcore.quality.validation.sources.splits import FixtureSplitsSource
from tpcore.selfheal.registry import HEAL_SPECS, spec_for
from tpcore.selfheal.runner import make_canonical_runner

logger = structlog.get_logger(__name__)

# Reverse of the existing feed→stage SoT (spec §2.1: "No new mapping —
# reuse what exists"). ingest stage name → feed. Values are unique
# (one schedulable stage per feed); a coverage test pins every entry to
# ≥1 canonical check so a misaligned new feed fails the build.
_STAGE_FEED: dict[str, str] = {stage: feed for feed, stage in FEED_STAGE.items()}

RunStage = Callable[[str, dict[str, str]], Awaitable[int]]
_CheckCallable = Callable[..., Awaitable[CheckResult]]


def _src(check: _CheckCallable, source: Any) -> _CheckCallable:
    """Bind the fixture source so every entry is uniform ``fn(pool)``."""

    async def _bound(pool: Any) -> CheckResult:
        return await check(pool, source)

    return _bound


# check_name → uniform ``async fn(pool) -> CheckResult``. The 3
# source-of-truth checks (delistings/constituent/splits) use the same
# Fixture* defaults run_suite uses. A drift test pins this == suite.
_CHECK_FN: dict[str, _CheckCallable] = {
    "delistings": _src(check_delistings, FixtureDelistingsSource()),
    "constituent": _src(check_constituent_snapshot, FixtureConstituentSource()),
    "splits": _src(check_splits, FixtureSplitsSource()),
    "row_integrity": _src(check_row_integrity, None),
    "fundamentals_integrity": _src(check_fundamentals_integrity, None),
    "corporate_actions_integrity": _src(check_corporate_actions_integrity, None),
    "earnings_events_freshness": _src(check_earnings_events_freshness, None),
    "sec_filings_freshness": _src(check_sec_filings_freshness, None),
    "liquidity_tiers_freshness": _src(check_liquidity_tiers_freshness, None),
    "ticker_classifications_coverage": _src(
        check_ticker_classifications_coverage, None
    ),
    "macro_indicators_freshness": _src(check_macro_indicators_freshness, None),
    "macro_indicators_completeness": _src(check_macro_indicators_completeness, None),
    "prices_daily_freshness": _src(check_prices_daily_freshness, None),
    "prices_daily_completeness": _src(check_prices_daily_completeness, None),
    "options_max_pain_freshness": _src(check_options_max_pain_freshness, None),
    "insider_sentiment_freshness": _src(check_insider_sentiment_freshness, None),
    "social_sentiment_freshness": _src(check_social_sentiment_freshness, None),
    "fear_greed_freshness": _src(check_fear_greed_freshness, None),
    "short_interest_freshness": _src(check_short_interest_freshness, None),
    "borrow_rates_freshness": _src(check_borrow_rates_freshness, None),
    "aaii_sentiment_freshness": _src(check_aaii_sentiment_freshness, None),
}


@dataclass(frozen=True)
class HealOneResult:
    check_name: str
    healed: bool
    attempts: int
    escalated_reason: str | None = None


def feed_checks(feed: str) -> list[str]:
    """The canonical check name(s) whose HealSpec.source == ``feed``."""
    return sorted(
        check for check, spec in HEAL_SPECS.items() if spec.source == feed
    )


async def validate_one(pool: Any, check_name: str) -> CheckResult:
    """Run the single canonical check (the suite's exact function)."""
    fn = _CHECK_FN.get(check_name)
    if fn is None:
        raise KeyError(f"no canonical check registered for {check_name!r}")
    return await fn(pool)


async def validate_feed(pool: Any, feed: str) -> tuple[bool, list[str]]:
    """Run every canonical check for ``feed``. Returns
    ``(all_passed, [red check names])``."""
    red: list[str] = []
    for cn in feed_checks(feed):
        r = await validate_one(pool, cn)
        if not r.passed:
            red.append(cn)
    return (not red, red)


async def heal_one(
    pool: Any, check_name: str, run_stage: RunStage
) -> HealOneResult:
    """Bounded single-check self-heal — the per-feed counterpart of
    ``run_self_heal``. Runs ONLY this check's HealSpec repair, bounded
    by ``max_attempts``, re-validating after each. ``healable=False``
    (or unknown) → escalate, never heal."""
    spec = spec_for(check_name)
    if spec is None:
        return HealOneResult(check_name, False, 0,
                             "no HealSpec — unknown red (add a spec)")
    if not spec.healable:
        return HealOneResult(check_name, False, 0, spec.unhealable_reason)
    for attempt in range(1, spec.max_attempts + 1):
        rc = await run_stage(spec.stage, spec.params)
        if rc != 0:
            return HealOneResult(
                check_name, False, attempt,
                f"bounded repair stage {spec.stage!r} exited {rc} — "
                f"cannot self-heal through a failing repair")
        if (await validate_one(pool, check_name)).passed:
            logger.info("per_feed.healed", check=check_name, attempt=attempt)
            return HealOneResult(check_name, True, attempt)
    return HealOneResult(
        check_name, False, spec.max_attempts,
        f"still red after {spec.max_attempts} bounded repair attempts")


@dataclass(frozen=True)
class FeedOutcome:
    feed: str
    green: bool
    healed: list[str]
    escalated: list[tuple[str, str]]  # (check, reason)


async def validate_and_heal_feed(
    pool: Any, feed: str, run_stage: RunStage
) -> FeedOutcome:
    """The on-completion unit (landed dark — Phase 2 wires the caller):
    validate ``feed``; for each red check, bounded ``heal_one`` +
    re-validate; honest escalation for what stays red. Idempotent: a
    green feed is a no-op."""
    ok, red = await validate_feed(pool, feed)
    if ok:
        return FeedOutcome(feed, True, [], [])
    healed: list[str] = []
    escalated: list[tuple[str, str]] = []
    for cn in red:
        r = await heal_one(pool, cn, run_stage)
        if r.healed:
            healed.append(cn)
        else:
            escalated.append((cn, r.escalated_reason or "unknown"))
    return FeedOutcome(feed, not escalated, healed, escalated)


def upstream_feeds(feed: str) -> list[str]:
    """The feeds ``feed`` is derived from — the union of its checks'
    HealSpec.depends_on (feed names, read LIVE from the registry SoT,
    not an import snapshot — the cutover-agent lesson, spec §5).
    Empty ⇒ leaf."""
    return sorted(
        {
            dep
            for c in feed_checks(feed)
            if (s := spec_for(c)) is not None
            for dep in s.depends_on
        }
    )


def is_leaf_feed(feed: str) -> bool:
    """Leaf = no check declares a HealSpec.depends_on (spec §3).
    Derived feeds validate only after every upstream is green this
    cycle (Phase 3), never on a single upstream's completion."""
    return not upstream_feeds(feed)


async def on_stage_complete(
    pool: Any,
    stage_name: str,
    run_id: str,
    *,
    cycle_green: set[str] | None = None,
) -> FeedOutcome | None:
    """Per-feed validate-on-completion hook — call after an ingest stage
    completes OK. Resolves stage→feed via the existing SoT.

    - **Leaf feed** (Phase 2): validate immediately, bounded-heal on red.
    - **Derived feed** (Phase 3): its own producing stage runs *after*
      its upstreams' stages, so at this point validate it **only if
      every** ``upstream_feeds`` went green this cycle (tracked in
      ``cycle_green``, read live). Any upstream not green ⇒ defer
      (return ``None``) — the end-of-cycle monolithic gate stays
      authoritative (spec §4); we never false-fail a derived feed on a
      red upstream.

    ``cycle_green`` is the in-cycle set of feeds this hook has already
    validated green (cmd_update owns it across stages). When a feed
    validates green here it is added, so a later derived feed can see
    it. ``None`` ⇒ no cycle state (leaf-only, Phase-2 behaviour).
    Returns ``None`` for infra stages + deferred derived feeds.
    """
    feed = _STAGE_FEED.get(stage_name)
    if feed is None:
        return None  # infra stage — not a feed producer
    if not feed_checks(feed):
        return None

    ups = upstream_feeds(feed)
    if ups:  # derived feed
        if cycle_green is None:
            return None  # no cycle state → defer (Phase-2 compat)
        missing = [u for u in ups if u not in cycle_green]
        if missing:
            logger.info(
                "per_feed.derived_deferred",
                feed=feed, stage=stage_name, upstream_not_green=missing,
            )
            return None  # final monolithic gate is authoritative

    outcome = await validate_and_heal_feed(
        pool, feed, make_canonical_runner(run_id)
    )
    if outcome.green and cycle_green is not None:
        cycle_green.add(feed)
    return outcome


__all__ = [
    "FeedOutcome",
    "HealOneResult",
    "RunStage",
    "feed_checks",
    "heal_one",
    "is_leaf_feed",
    "on_stage_complete",
    "upstream_feeds",
    "validate_and_heal_feed",
    "validate_feed",
    "validate_one",
]
