"""Per-feed cadence profile (#163) — model + clockwork coverage.

Mirrors the HEAL_SPECS drift test: you cannot ship a self-healing
feed without declaring its evidence-backed cadence. Also pins the
single-source-of-truth wiring (the short_interest 35→42 bug fix).
"""
from __future__ import annotations

from tpcore.feeds import (
    FEED_PROFILES,
    FeedProfile,
    freshness_max_age_days,
    profile_for,
)
from tpcore.selfheal.registry import HEAL_SPECS


def test_every_healable_feed_has_a_cadence_profile() -> None:
    """Clockwork: a healable HealSpec source MUST declare a cadence
    profile — adding a self-healing feed without one fails the build,
    so cadence can never be an afterthought / a guessed blanket."""
    healable_sources = {s.source for s in HEAL_SPECS.values() if s.healable}
    missing = sorted(healable_sources - set(FEED_PROFILES))
    assert not missing, f"healable feeds with no FeedProfile: {missing}"


def test_profiles_are_frozen_and_evidence_backed() -> None:
    for feed, p in FEED_PROFILES.items():
        assert isinstance(p, FeedProfile)
        assert p.feed == feed, f"{feed}: feed field mismatch ({p.feed})"
        assert p.evidence.strip(), f"{feed}: empty evidence (no-vendor-blame)"
        if p.freshness_max_age_days is not None:
            assert p.freshness_max_age_days > 0
        if p.skip_guard_days is not None:
            assert p.skip_guard_days >= 0


def test_profile_is_immutable() -> None:
    import pydantic
    import pytest
    p = FEED_PROFILES["finra_short_interest"]
    with pytest.raises(pydantic.ValidationError):
        p.freshness_max_age_days = 999  # frozen → must raise


def test_finra_cadence_is_evidence_derived_42_not_guessed_35() -> None:
    """Regression: the short_interest check must read 42 (evidence-
    derived, bi-monthly ~16d + ~13d dissemination lag + slack) from
    the profile — NOT the old guessed 35 that the docstring claimed
    but the constant never applied."""
    p = profile_for("finra_short_interest")
    assert p is not None and p.freshness_max_age_days == 42
    from tpcore.quality.validation.checks.short_interest_freshness import (
        MAX_AGE_DAYS,
    )
    assert MAX_AGE_DAYS == 42


def test_freshness_helper_falls_back_only_when_unprofiled() -> None:
    assert freshness_max_age_days("finra_short_interest", 35) == 42
    assert freshness_max_age_days("does_not_exist", 99) == 99
    # ticker_classifications is coverage-only → no freshness age
    assert freshness_max_age_days("ticker_classifications", 60) == 60
