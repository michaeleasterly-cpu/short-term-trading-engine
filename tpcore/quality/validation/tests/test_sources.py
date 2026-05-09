"""Tests for the fixture-backed sources."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from tpcore.quality.validation.sources.constituents import (
    ConstituentSource,
    FixtureConstituentSource,
    RemovalEvent,
)
from tpcore.quality.validation.sources.delistings import (
    DelistingEvent,
    DelistingsSource,
    FixtureDelistingsSource,
)
from tpcore.quality.validation.sources.splits import (
    FixtureSplitsSource,
    SplitEvent,
    SplitsSource,
)


# ────────────────────────────────────────────────────────────────────────────
# Splits
# ────────────────────────────────────────────────────────────────────────────


def test_splits_loads_yaml(write_yaml) -> None:
    p = write_yaml(
        "splits.yaml",
        """
- ticker: AAPL
  split_date: 2020-08-31
  ratio: "4:1"
- ticker: NVDA
  split_date: 2024-06-10
  ratio: "10:1"
""",
    )
    src = FixtureSplitsSource(path=p)
    events = src.list_splits()
    assert len(events) == 2
    aapl = events[0]
    assert isinstance(aapl, SplitEvent)
    assert aapl.ticker == "AAPL"
    assert aapl.split_date == date(2020, 8, 31)
    assert aapl.ratio_num == 4
    assert aapl.ratio_den == 1


def test_splits_raises_on_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        FixtureSplitsSource(path=tmp_path / "does_not_exist.yaml")


def test_splits_raises_on_empty_fixture(write_yaml) -> None:
    p = write_yaml("splits.yaml", "[]\n")
    with pytest.raises(ValueError):
        FixtureSplitsSource(path=p)


def test_splits_raises_on_malformed_ratio(write_yaml) -> None:
    p = write_yaml(
        "splits.yaml",
        """
- ticker: AAPL
  split_date: 2020-08-31
  ratio: "four-to-one"
""",
    )
    with pytest.raises(ValueError):
        FixtureSplitsSource(path=p)


def test_splits_source_is_subclass_of_abc() -> None:
    assert issubclass(FixtureSplitsSource, SplitsSource)


# ────────────────────────────────────────────────────────────────────────────
# Delistings
# ────────────────────────────────────────────────────────────────────────────


def test_delistings_loads_yaml_with_alts(write_yaml) -> None:
    p = write_yaml(
        "delistings.yaml",
        """
- ticker: SIVBQ
  alt_tickers: [SIVB]
  delisting_date: 2023-03-17
  reason: bankruptcy
- ticker: BBBYQ
  delisting_date: 2023-04-23
  reason: bankruptcy
""",
    )
    src = FixtureDelistingsSource(path=p)
    events = src.list_delistings()
    assert len(events) == 2
    svb = events[0]
    assert isinstance(svb, DelistingEvent)
    assert svb.ticker == "SIVBQ"
    assert svb.alt_tickers == ["SIVB"]
    assert events[1].alt_tickers == []  # default empty


def test_delistings_raises_on_empty(write_yaml) -> None:
    p = write_yaml("delistings.yaml", "[]\n")
    with pytest.raises(ValueError):
        FixtureDelistingsSource(path=p)


def test_delistings_subclass_of_abc() -> None:
    assert issubclass(FixtureDelistingsSource, DelistingsSource)


# ────────────────────────────────────────────────────────────────────────────
# Constituents
# ────────────────────────────────────────────────────────────────────────────


def test_constituents_loads_current_and_removals(write_yaml) -> None:
    p = write_yaml(
        "constituents.yaml",
        """
current_sp500_snapshot_date: 2026-05-10
current_sp500:
  - AAPL
  - MSFT
recent_removals:
  - ticker: SIVBQ
    removed_date: 2023-03-15
    reason: bankruptcy
    expect_delisted: true
""",
    )
    src = FixtureConstituentSource(path=p)
    assert src.list_current_sp500() == ["AAPL", "MSFT"]
    removals = src.list_recent_removals()
    assert len(removals) == 1
    rm = removals[0]
    assert isinstance(rm, RemovalEvent)
    assert rm.ticker == "SIVBQ"
    assert rm.expect_delisted is True


def test_constituents_raises_on_empty_current_sp500(write_yaml) -> None:
    p = write_yaml(
        "constituents.yaml",
        """
current_sp500_snapshot_date: 2026-05-10
current_sp500: []
recent_removals:
  - ticker: SIVBQ
    removed_date: 2023-03-15
    reason: bankruptcy
    expect_delisted: true
""",
    )
    with pytest.raises(ValueError):
        FixtureConstituentSource(path=p)


def test_constituents_raises_on_empty_recent_removals(write_yaml) -> None:
    p = write_yaml(
        "constituents.yaml",
        """
current_sp500_snapshot_date: 2026-05-10
current_sp500:
  - AAPL
recent_removals: []
""",
    )
    with pytest.raises(ValueError):
        FixtureConstituentSource(path=p)


def test_constituents_subclass_of_abc() -> None:
    assert issubclass(FixtureConstituentSource, ConstituentSource)
