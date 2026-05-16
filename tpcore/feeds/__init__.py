"""Per-feed cadence profiles — single source of truth (#163).

A feed's cadence drives its skip-guard, freshness threshold and
self-heal expectation from one evidence-backed declaration. See
``profile.py`` for the model + registry and the honest scope of
which facets are enforced vs phased.
"""
from __future__ import annotations

from .profile import (
    FEED_PROFILES,
    FeedProfile,
    FeedTrigger,
    Targeting,
    freshness_max_age_days,
    profile_for,
)

__all__ = [
    "FEED_PROFILES",
    "FeedProfile",
    "FeedTrigger",
    "Targeting",
    "freshness_max_age_days",
    "profile_for",
]
