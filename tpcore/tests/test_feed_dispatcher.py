"""Profile-driven dispatcher (#165 facet 1) — pure selection core.

Feed-driven, not a blanket cron: each feed dispatched only when ITS
trigger/cadence is due per its FeedProfile.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tpcore.feeds.dispatcher import FEED_STAGE, compute_due_feeds

_NOW = datetime(2026, 5, 16, 21, 0, tzinfo=UTC)


def _due(last: dict[str, datetime], *, closed: bool = False) -> set[str]:
    return {d.feed for d in compute_due_feeds(_NOW, last, market_just_closed=closed)}


def test_never_pulled_is_due() -> None:
    # Empty history → every schedulable feed is due (bootstrap).
    due = _due({})
    assert due == set(FEED_STAGE) - set()  # all feeds with a stage
    assert "finra_short_interest" in due and "macro_indicators" in due


def test_bimonthly_not_due_within_cadence() -> None:
    # FINRA cadence_days=16; pulled 5d ago → NOT due.
    last = {f: _NOW - timedelta(days=5) for f in FEED_STAGE}
    assert "finra_short_interest" not in _due(last)


def test_bimonthly_due_after_cadence() -> None:
    last = {"finra_short_interest": _NOW - timedelta(days=20)}
    assert "finra_short_interest" in _due(last)


def test_market_close_feed_only_after_close() -> None:
    # prices_daily (MARKET_CLOSE), pulled 2d ago.
    last = {"prices_daily": _NOW - timedelta(days=2)}
    assert "prices_daily" not in _due(last, closed=False)
    assert "prices_daily" in _due(last, closed=True)


def test_market_close_not_redispatched_same_session() -> None:
    # Already pulled 10 min ago, market just closed → not due again.
    last = {"prices_daily": _NOW - timedelta(minutes=10)}
    assert "prices_daily" not in _due(last, closed=True)


def test_continuous_polls_on_cadence() -> None:
    # SEC continuous, cadence 1d. 2d stale → due; 2h ago → not.
    assert "sec_insider_transactions" in _due(
        {"sec_insider_transactions": _NOW - timedelta(days=2)})
    assert "sec_insider_transactions" not in _due(
        {"sec_insider_transactions": _NOW - timedelta(hours=2)})


def test_weekly_feed_cadence() -> None:
    # AAII cadence_days=7.
    assert "aaii_sentiment" not in _due({"aaii_sentiment": _NOW - timedelta(days=3)})
    assert "aaii_sentiment" in _due({"aaii_sentiment": _NOW - timedelta(days=8)})


def test_only_profiled_stages_dispatched() -> None:
    # Every dispatched feed maps to a real canonical ops stage name.
    for d in compute_due_feeds(_NOW, {}, market_just_closed=True):
        assert d.stage == FEED_STAGE[d.feed]
        assert d.reason  # honest reason always present


def test_naive_datetimes_are_coerced_utc() -> None:
    naive_now = datetime(2026, 5, 16, 21, 0)  # noqa: DTZ001 - intentional
    out = compute_due_feeds(
        naive_now, {"macro_indicators": datetime(2026, 1, 1)},  # noqa: DTZ001
        market_just_closed=False)
    assert any(d.feed == "macro_indicators" for d in out)  # 4mo stale → due
