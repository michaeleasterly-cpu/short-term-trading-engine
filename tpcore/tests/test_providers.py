"""Invariant/drift tests for the ProviderBinding registry (Phase 1).

Symmetric to the HealSpec registry-coverage test: a new feed fails the
build until a binding decision is recorded, exactly one provider may be
ACTIVE per feed (the snap-in/out invariant), and every recorded
adapter_module must actually resolve (evidence is real, not assumed).
"""
from __future__ import annotations

import importlib

import pytest

from tpcore.feeds.profile import FEED_PROFILES
from tpcore.providers import (
    ProviderBinding,
    ProviderStatus,
    active_provider,
    all_feeds,
    bindings_for,
)


def test_coverage_drift_both_directions() -> None:
    """Every FeedProfile feed has a binding, and every binding's feed
    is a known feed — so a new feed/provider can't be silently
    forgotten (the self-heal-registry clockwork pattern)."""
    feed_profile_feeds = set(FEED_PROFILES)
    bound = all_feeds()
    missing = feed_profile_feeds - bound
    extra = bound - feed_profile_feeds
    assert missing == set(), f"FeedProfile feeds with no ProviderBinding: {missing}"
    assert extra == set(), f"ProviderBindings for unknown feeds: {extra}"


def test_exactly_one_active_per_feed() -> None:
    """The snap-in/out invariant: a feed is served by exactly one
    ACTIVE provider (others are candidate/fallback/deprecated/retired)."""
    for feed in all_feeds():
        actives = [
            b for b in bindings_for(feed) if b.status is ProviderStatus.ACTIVE
        ]
        assert len(actives) == 1, (
            f"{feed}: expected exactly one ACTIVE provider, got "
            f"{[b.provider for b in actives]}"
        )
        assert active_provider(feed) is actives[0]


def test_adapter_module_resolves() -> None:
    """Evidence not assumed: every adapter_module dotted path must
    resolve — either an importable module, or module + attribute."""
    for feed in all_feeds():
        for b in bindings_for(feed):
            path = b.adapter_module
            try:
                importlib.import_module(path)
                continue
            except ModuleNotFoundError:
                pass
            mod_path, _, attr = path.rpartition(".")
            assert mod_path, f"{feed}/{b.provider}: bad adapter_module {path!r}"
            mod = importlib.import_module(mod_path)
            assert hasattr(mod, attr), (
                f"{feed}/{b.provider}: {mod_path} has no attribute "
                f"{attr!r} (adapter_module {path!r} does not resolve)"
            )


def test_fallback_requires_parity_verified_at() -> None:
    """A FALLBACK can't stand in without a parity pass (model enforces
    on construction; assert generically over the live registry too)."""
    for feed in all_feeds():
        for b in bindings_for(feed):
            if b.status is ProviderStatus.FALLBACK:
                assert b.parity_verified_at is not None, (
                    f"{feed}/{b.provider}: FALLBACK without parity_verified_at"
                )


def test_binding_is_frozen_and_evidence_mandatory() -> None:
    b = active_provider("prices_daily")
    assert b is not None
    with pytest.raises((TypeError, ValueError)):
        b.status = ProviderStatus.RETIRED  # frozen
    with pytest.raises(ValueError, match="evidence is mandatory"):
        ProviderBinding(
            feed="x", provider="y", adapter_module="tpcore.providers",
            status=ProviderStatus.ACTIVE, evidence="   ",
        )
    with pytest.raises(ValueError, match="FALLBACK requires parity_verified_at"):
        ProviderBinding(
            feed="x", provider="y", adapter_module="tpcore.providers",
            status=ProviderStatus.FALLBACK, evidence="e",
        )
