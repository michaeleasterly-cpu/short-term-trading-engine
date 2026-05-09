"""Delistings source — ABC + fixture-backed implementation.

A `DelistingEvent` carries the primary ticker, an optional list of
`alt_tickers` (the same security under a different symbol — e.g. `SIVB`
before bankruptcy and `SIVBQ` after), the recorded delisting date, and a
free-text reason. The check passes if any of `[ticker] + alt_tickers`
satisfies the four conditions in spec §3.1.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from .splits import _default_fixture


class DelistingEvent(BaseModel):
    """One known delisting."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str
    alt_tickers: list[str] = Field(default_factory=list)
    delisting_date: date
    reason: str
    notes: str | None = None


class DelistingsSource(ABC):
    @abstractmethod
    def list_delistings(self) -> list[DelistingEvent]: ...


class FixtureDelistingsSource(DelistingsSource):
    def __init__(self, path: Path | None = None) -> None:
        self._path = path or _default_fixture("delistings.yaml")
        self._events = self._load()

    def list_delistings(self) -> list[DelistingEvent]:
        return list(self._events)

    def _load(self) -> list[DelistingEvent]:
        if not self._path.exists():
            raise FileNotFoundError(self._path)
        raw = yaml.safe_load(self._path.read_text(encoding="utf-8"))
        if not isinstance(raw, list) or not raw:
            raise ValueError(f"delistings fixture {self._path} is empty or not a list")
        events: list[DelistingEvent] = []
        for entry in raw:
            events.append(
                DelistingEvent(
                    ticker=entry["ticker"],
                    alt_tickers=list(entry.get("alt_tickers") or []),
                    delisting_date=entry["delisting_date"],
                    reason=entry["reason"],
                    notes=entry.get("notes"),
                )
            )
        return events


__all__ = ["DelistingEvent", "DelistingsSource", "FixtureDelistingsSource"]
