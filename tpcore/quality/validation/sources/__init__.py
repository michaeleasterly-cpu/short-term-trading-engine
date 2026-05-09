"""Source providers for the Data Validation Suite.

Each source has an ABC and a fixture-backed concrete implementation. Future
EDGAR-backed implementations slot in as additional concretes without
disturbing checks, suite, or capital gate.
"""
from __future__ import annotations

from .constituents import (
    ConstituentSource,
    FixtureConstituentSource,
    RemovalEvent,
)
from .delistings import (
    DelistingEvent,
    DelistingsSource,
    FixtureDelistingsSource,
)
from .splits import FixtureSplitsSource, SplitEvent, SplitsSource

__all__ = [
    "ConstituentSource",
    "DelistingEvent",
    "DelistingsSource",
    "FixtureConstituentSource",
    "FixtureDelistingsSource",
    "FixtureSplitsSource",
    "RemovalEvent",
    "SplitEvent",
    "SplitsSource",
]
