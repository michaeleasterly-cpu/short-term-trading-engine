"""Profile-driven feed dispatcher (#165 facet 1 — TRIGGER).

Replaces the blanket "one fixed-time cron runs every stage, skip-guards
filter" model with feed-driven selection: on each wake, ask each feed
"is your trigger due per your declared FeedProfile?" and dispatch ONLY
the due feeds via the canonical ``ops.py --stage`` infra.

This module is the PURE, unit-testable selection core — no DB, no
clock, no subprocess. ``compute_due_feeds`` takes the current time,
the per-feed last-success timestamps, and whether the market just
closed, and returns the feeds whose trigger is due. The DB-backed
runner (``runner`` / ``__main__``) supplies the real inputs.

Honest scope: this is the *cadence + trigger-class* dispatcher.
Exact vendor release calendars (FRED per-series dates, FINRA's precise
dissemination dates) are NOT modelled here — that is the
publication-availability gate (facet 4), separately phased. A feed
that is "due" here may still legitimately have nothing new at the
source; the skip-guards + idempotent upserts make a redundant dispatch
harmless, and facet 4 will later suppress the redundant pull entirely.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tpcore.feeds.profile import FEED_PROFILES, FeedProfile, FeedTrigger

# Stage to invoke per feed (canonical ops.py --stage name). Kept here,
# next to the trigger logic, so "which feeds are due" and "what to run"
# stay in lockstep. A feed with no schedulable stage (pure source-of-
# truth fixtures) is intentionally absent → never auto-dispatched.
FEED_STAGE: dict[str, str] = {
    "prices_daily": "daily_bars",
    "finra_short_interest": "finra_short_interest",
    "aaii_sentiment": "aaii_sentiment",
    "iborrowdesk_borrow_rates": "iborrowdesk_borrow_rates",
    "apewisdom_social_sentiment": "apewisdom_social_sentiment",
    "finnhub_insider_sentiment": "finnhub_insider_sentiment",
    "greeks_max_pain": "greeks_max_pain",
    "earnings_events": "earnings_refresh",
    "sec_insider_transactions": "sec_filings",
    "macro_indicators": "macro_indicators",
    "liquidity_tiers": "tier_refresh",
    "ticker_classifications": "classify_tickers",
    "fear_greed": "fear_greed",
}


class DueFeed:
    """A feed selected for dispatch + why (for honest logging)."""

    __slots__ = ("feed", "stage", "reason")

    def __init__(self, feed: str, stage: str, reason: str) -> None:
        self.feed = feed
        self.stage = stage
        self.reason = reason

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"DueFeed({self.feed} via {self.stage}: {self.reason})"

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, DueFeed)
            and (self.feed, self.stage) == (other.feed, other.stage)
        )


def _is_due(
    p: FeedProfile,
    now: datetime,
    last_success: datetime | None,
    market_just_closed: bool,
) -> tuple[bool, str]:
    """Pure per-feed trigger decision. Returns (due, reason)."""
    if last_success is None:
        return True, "never pulled"

    age = now - last_success
    cadence = max(int(p.cadence_days), 1)

    if p.trigger == FeedTrigger.MARKET_CLOSE:
        # Due once per trading day, after the close. The runner decides
        # market_just_closed via tpcore.calendar; here we only gate on
        # "not already pulled since the most recent close".
        if market_just_closed and age >= timedelta(hours=1):
            return True, "market closed; not yet pulled this session"
        return False, "intraday / already pulled this session"

    if p.trigger == FeedTrigger.CONTINUOUS:
        # Poll on its cadence (typically daily); a continuous source
        # always plausibly has something new.
        if age >= timedelta(days=cadence):
            return True, f"continuous; {age.days}d since last (cad {cadence}d)"
        return False, f"polled {age.days}d ago (cad {cadence}d)"

    # VENDOR_* / RECOMPUTE / DERIVED / INTRADAY: due when a full cadence
    # period has elapsed since the last successful pull. INTRADAY uses
    # cadence_days=1 so it is effectively daily here (sub-daily polling
    # is a facet-4/launchd-frequency concern, explicitly out of scope).
    if age >= timedelta(days=cadence):
        return True, f"cadence elapsed: {age.days}d ≥ {cadence}d ({p.trigger})"
    return False, f"fresh: {age.days}d < {cadence}d ({p.trigger})"


def compute_due_feeds(
    now: datetime,
    last_success_by_feed: dict[str, datetime],
    *,
    market_just_closed: bool,
) -> list[DueFeed]:
    """Feeds whose trigger is due, per their FeedProfile. Pure.

    ``last_success_by_feed`` maps feed → last successful pull (UTC).
    Feeds absent from the map are treated as never pulled. Only feeds
    with a schedulable canonical stage (``FEED_STAGE``) are considered.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    due: list[DueFeed] = []
    for feed, stage in FEED_STAGE.items():
        p = FEED_PROFILES.get(feed)
        if p is None:
            # Clockwork drift test guarantees coverage; defensively skip.
            continue
        ls = last_success_by_feed.get(feed)
        if ls is not None and ls.tzinfo is None:
            ls = ls.replace(tzinfo=UTC)
        ok, reason = _is_due(p, now, ls, market_just_closed)
        if ok:
            due.append(DueFeed(feed, stage, reason))
    return due


_LAST_SUCCESS_SQL = """
    SELECT data->>'stage' AS stage, MAX(recorded_at) AS last_ok
    FROM platform.application_log
    WHERE engine = 'ops'
      AND event_type = 'INGESTION_COMPLETE'
      AND data ? 'stage'
    GROUP BY data->>'stage'
"""


async def select_due(pool, now: datetime | None = None) -> list[DueFeed]:
    """DB-backed: read each stage's last successful completion from the
    canonical ``application_log`` signal (engine='ops',
    INGESTION_COMPLETE, data->>'stage'), derive the market-close gate
    from ``tpcore.calendar``, and return the feeds due per profile.

    No per-table plumbing — one uniform query. Idempotent + read-only.
    """
    from tpcore.calendar import previous_close

    now = now or datetime.now(UTC)
    stage_to_feed = {v: k for k, v in FEED_STAGE.items()}

    async with pool.acquire() as conn:
        rows = await conn.fetch(_LAST_SUCCESS_SQL)
    last_by_feed: dict[str, datetime] = {}
    for r in rows:
        feed = stage_to_feed.get(r["stage"])
        if feed and r["last_ok"] is not None:
            last_by_feed[feed] = r["last_ok"]

    # Market-close gate: a session close has happened that the prices
    # feed has not been pulled for yet.
    try:
        last_close = previous_close(now)
    except Exception:  # calendar edge / pre-history — be conservative
        last_close = None
    prices_ls = last_by_feed.get("prices_daily")
    market_just_closed = last_close is not None and (
        prices_ls is None or prices_ls < last_close
    )
    return compute_due_feeds(
        now, last_by_feed, market_just_closed=market_just_closed
    )


__all__ = ["DueFeed", "FEED_STAGE", "compute_due_feeds", "select_due"]
