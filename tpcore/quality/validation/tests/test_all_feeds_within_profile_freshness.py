"""Comprehensive live-DB freshness regression — every FeedProfile entry.

Wave 5+6 closeout (2026-05-22): the full-spectrum cadence-aware feed audit
found 14/15 feeds within profile freshness on a live probe; this test is
the recurring guardian that ensures none silently goes stale between
audits.

Pattern follows ``tests/test_survivorship_completeness.py``:

* opt-in via ``DATABASE_URL`` (or the IPv4 alias); CI skip is expected.
* parametrized over the live ``FEED_PROFILES`` registry — adding a new
  feed automatically gets a row here.
* feeds with ``freshness_max_age_days=None`` are skipped (they're
  coverage/window/derived checks, NOT age-based).
* feeds where the obvious latest-bar column doesn't exist (e.g.
  ``ticker_classifications`` — the SoT is a snapshot table) get skipped
  with a documented reason. NOT silently absent.

This is the REGRESSION CATCHER that prevents "data went stale silently"
— the defect class the operator's 2026-05-22 audit was designed to
catch. Without good data we got shit for brains.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

from tpcore.feeds.profile import FEED_PROFILES


@dataclass(frozen=True)
class _LatestProbe:
    """How to read the latest data timestamp for a feed."""

    sql: str
    # ``None`` if the feed is a snapshot/coverage check (no age-based test);
    # ``reason`` documents WHY the live-DB check is skipped (transparency,
    # not silence).
    skip_reason: str | None = None


# Per-feed latest-timestamp probes. Keyed by FeedProfile key.
# All feeds with ``freshness_max_age_days`` set get a probe; feeds with
# ``None`` skip via the explicit ``skip_reason`` (NOT silent — the
# audit-trail must show the gap).
_PROBES: dict[str, _LatestProbe] = {
    "prices_daily": _LatestProbe(
        # Anchor on SPY — the platform's macro anchor; always-trading;
        # universally covered. Matches the prices_daily_freshness check's
        # critical-ticker logic.
        sql="SELECT MAX(date) FROM platform.prices_daily WHERE ticker='SPY'",
    ),
    "finra_short_interest": _LatestProbe(
        sql="SELECT MAX(settlement_date) FROM platform.short_interest",
    ),
    "aaii_sentiment": _LatestProbe(
        # Task #18 P7: reads macro_data source='aaii' / observed_date.
        sql="SELECT MAX(observed_date) FROM platform.macro_data "
            "WHERE source='aaii' AND realtime_end='infinity'",
    ),
    "iborrowdesk_borrow_rates": _LatestProbe(
        sql="SELECT MAX(date) FROM platform.borrow_rates",
    ),
    "apewisdom_social_sentiment": _LatestProbe(
        sql="SELECT MAX(date) FROM platform.social_sentiment",
    ),
    "finnhub_insider_sentiment": _LatestProbe(
        sql="",
        skip_reason=(
            "monthly-period feed (age measured in year*12+month, not "
            "calendar days); freshness_max_age_days=None in the profile"
        ),
    ),
    "earnings_events": _LatestProbe(
        sql="SELECT MAX(report_date) FROM platform.earnings_events",
    ),
    "sec_insider_transactions": _LatestProbe(
        sql=(
            "SELECT MAX(filing_date) FROM platform.insider_transactions"
        ),
    ),
    "macro_indicators": _LatestProbe(
        # Task #18 P7: reads macro_data source='fred' / observed_date.
        sql="SELECT MAX(observed_date) FROM platform.macro_data "
            "WHERE source='fred' AND realtime_end='infinity'",
    ),
    "liquidity_tiers": _LatestProbe(
        sql="SELECT MAX(last_updated)::date FROM platform.liquidity_tiers",
    ),
    "ticker_classifications": _LatestProbe(
        sql="",
        skip_reason=(
            "coverage metric (live COUNT vs source-count snapshot), not "
            "an age threshold; freshness_max_age_days=None in the profile"
        ),
    ),
    "fear_greed": _LatestProbe(
        # Task #18 P7: reads macro_data source='cnn_fear_greed' / observed_date.
        sql="SELECT MAX(observed_date) FROM platform.macro_data "
            "WHERE source='cnn_fear_greed' AND realtime_end='infinity'",
    ),
    "fundamentals_quarterly": _LatestProbe(
        sql=(
            "SELECT MAX(period_end_date) FROM platform.fundamentals_quarterly"
        ),
    ),
    "corporate_actions": _LatestProbe(
        sql=(
            "SELECT MAX(action_date) FROM platform.corporate_actions"
        ),
    ),
    # P0_3 RETIRE 2026-05-25 — ``insider_sentiment_daily`` LatestProbe
    # removed (target table ``platform.insider_filings`` dropped).
}


def _have_database_url() -> bool:
    return bool(
        os.environ.get("DATABASE_URL")
        or os.environ.get("DATABASE_URL_IPV4")
    )


def test_every_feed_profile_has_a_probe_or_skip_reason() -> None:
    """Adding a new feed to FEED_PROFILES MUST add a probe row here —
    silent absence is a defect (the whole point of this test is to be
    the comprehensive guard)."""
    missing = sorted(set(FEED_PROFILES) - set(_PROBES))
    assert not missing, (
        f"FeedProfile entries without a live-DB probe row: {missing}. "
        f"Add a _LatestProbe (or skip_reason) to _PROBES so the new "
        f"feed is covered by the regression guardian."
    )
    extra = sorted(set(_PROBES) - set(FEED_PROFILES))
    assert not extra, (
        f"Probe rows without a matching FeedProfile entry: {extra}. "
        f"Stale probe — remove it or fix the feed name."
    )


@pytest.mark.integration
@pytest.mark.skipif(
    not _have_database_url(),
    reason="live-DB regression test; CI skip is expected",
)
@pytest.mark.parametrize("feed", sorted(FEED_PROFILES))
async def test_feed_latest_within_profile_freshness(feed: str) -> None:
    """Each FeedProfile-tracked feed's latest data must be within its
    declared ``freshness_max_age_days``. This is the cross-feed
    regression catcher — one of these going red means a feed silently
    went stale between audits."""
    import asyncpg

    profile = FEED_PROFILES[feed]
    probe = _PROBES[feed]
    if probe.skip_reason is not None:
        pytest.skip(f"{feed}: {probe.skip_reason}")
    if profile.freshness_max_age_days is None:
        pytest.skip(
            f"{feed}: freshness_max_age_days=None — not an age-based feed"
        )

    db_url = (
        os.environ.get("DATABASE_URL")
        or os.environ["DATABASE_URL_IPV4"]
    )
    conn = await asyncpg.connect(db_url, statement_cache_size=0)
    try:
        latest: Any = await conn.fetchval(probe.sql)
    finally:
        await conn.close()

    assert latest is not None, (
        f"{feed}: probe '{probe.sql}' returned NULL — table empty or "
        f"the probe is mis-aimed. Investigate before relaxing."
    )
    today = datetime.now(UTC).date()
    age_days = (today - latest).days
    threshold = profile.freshness_max_age_days
    assert age_days <= threshold, (
        f"{feed}: latest data {latest} is {age_days}d old, exceeds "
        f"FeedProfile freshness_max_age_days={threshold}. The feed has "
        f"silently gone stale — investigate ingest + self-heal."
    )
