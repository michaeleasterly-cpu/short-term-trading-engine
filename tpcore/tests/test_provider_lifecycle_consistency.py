"""3-way-retire enforcement — the cross-SoT consistency guard (Phase 3).

A feed lives in three single-sources-of-truth: ProviderBinding (who
serves it), FeedProfile (it is monitored), HealSpec (it has a heal
decision). Ad-hoc retirements this session (Sigma → fake-healable
HealSpec left behind; FRED truncation → dangling spec) proved that
retiring one SoT without the others leaves half-retired state that
no-ops or alarms forever.

These tests make half-retirement **fail the build** (the clockwork
discipline, same as the HealSpec registry-coverage test):

* a feed with a NON-retired binding must be present in BOTH
  FeedProfile and the HealSpec source set;
* a fully-RETIRED feed must be ABSENT from BOTH (its FeedProfile and
  HealSpec must be removed in the same change).
"""
from __future__ import annotations

from tpcore.feeds.profile import FEED_PROFILES
from tpcore.providers import PROVIDER_BINDINGS, ProviderStatus
from tpcore.selfheal.registry import HEAL_SPECS

_HEALSPEC_SOURCES = {s.source for s in HEAL_SPECS.values()}
_FEED_PROFILE_FEEDS = set(FEED_PROFILES)


def _fully_retired(feed: str) -> bool:
    bindings = PROVIDER_BINDINGS.get(feed, [])
    return bool(bindings) and all(
        b.status is ProviderStatus.RETIRED for b in bindings
    )


def test_live_feed_present_in_all_three_sots() -> None:
    """A feed with any non-RETIRED binding must be monitored
    (FeedProfile) AND have a heal decision (HealSpec) — you cannot
    serve a feed that isn't watched/healable."""
    for feed, bindings in PROVIDER_BINDINGS.items():
        if _fully_retired(feed):
            continue
        live = [b for b in bindings if b.status is not ProviderStatus.RETIRED]
        if not live:
            continue
        assert feed in _FEED_PROFILE_FEEDS, (
            f"{feed}: has a live ProviderBinding but no FeedProfile "
            f"(serving an unmonitored feed)"
        )
        assert feed in _HEALSPEC_SOURCES, (
            f"{feed}: has a live ProviderBinding but no HealSpec "
            f"(serving a feed with no heal decision)"
        )


def test_fully_retired_feed_offboarded_everywhere() -> None:
    """3-way-atomic retire: a feed whose every binding is RETIRED must
    be gone from FeedProfile AND the HealSpec source set — else it is
    half-retired (the dangling-spec / fake-healable class)."""
    for feed in PROVIDER_BINDINGS:
        if not _fully_retired(feed):
            continue
        assert feed not in _FEED_PROFILE_FEEDS, (
            f"{feed}: fully RETIRED but still in FEED_PROFILES — "
            f"half-retired (monitoring a dead feed). Remove its "
            f"FeedProfile in the same change."
        )
        assert feed not in _HEALSPEC_SOURCES, (
            f"{feed}: fully RETIRED but still a HealSpec.source — "
            f"half-retired (a heal spec for a dead feed is fake-"
            f"healable by construction). Remove/repoint it in the "
            f"same change."
        )


def test_today_no_feed_is_half_retired() -> None:
    """Sanity for the current registry: nothing is mid-retirement."""
    assert not any(
        _fully_retired(f) for f in PROVIDER_BINDINGS
    ), "a feed is marked fully RETIRED — the §3 RETIRE checklist applies"
