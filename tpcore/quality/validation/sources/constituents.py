"""Constituent source — ABC + fixture-backed implementation.

Provides two lists per spec §3.2:
* `list_current_sp500()` — the snapshot of names that should currently be
  present and freshly priced.
* `list_recent_removals()` — names that *were* in the S&P 500 (or simply
  matter for survivorship) and should still appear in `prices_daily`,
  delisted where applicable.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict

from .splits import _default_fixture


class RemovalEvent(BaseModel):
    """One historical S&P 500 removal."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str
    removed_date: date
    reason: str
    expect_delisted: bool = False


class ConstituentSource(ABC):
    @abstractmethod
    def list_current_sp500(self) -> list[str]:
        """Return the current S&P 500 constituent tickers."""
        ...

    @abstractmethod
    def list_recent_removals(self) -> list[RemovalEvent]:
        """Return recent index removals (ticker + effective date + cause)."""
        ...


class FixtureConstituentSource(ConstituentSource):
    def __init__(self, path: Path | None = None) -> None:
        self._path = path or _default_fixture("constituents.yaml")
        self._current, self._removals = self._load()

    def list_current_sp500(self) -> list[str]:
        return list(self._current)

    def list_recent_removals(self) -> list[RemovalEvent]:
        return list(self._removals)

    def _load(self) -> tuple[list[str], list[RemovalEvent]]:
        if not self._path.exists():
            raise FileNotFoundError(self._path)
        raw = yaml.safe_load(self._path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"constituents fixture {self._path} is not a mapping")
        current = raw.get("current_sp500") or []
        if not current:
            raise ValueError(f"constituents fixture {self._path} has empty current_sp500")
        removals_raw = raw.get("recent_removals") or []
        if not removals_raw:
            raise ValueError(f"constituents fixture {self._path} has empty recent_removals")
        removals: list[RemovalEvent] = []
        for entry in removals_raw:
            removals.append(
                RemovalEvent(
                    ticker=entry["ticker"],
                    removed_date=entry["removed_date"],
                    reason=entry["reason"],
                    expect_delisted=bool(entry.get("expect_delisted", False)),
                )
            )
        return list(current), removals


__all__ = ["ConstituentSource", "FixtureConstituentSource", "RemovalEvent"]
