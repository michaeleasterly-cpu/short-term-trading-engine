"""Splits source — ABC + fixture-backed implementation.

A `SplitEvent` carries the ticker, the session date the split took effect on,
and the ratio decomposed into numerator/denominator (e.g. a 4:1 split is
`ratio_num=4, ratio_den=1` — four post-split shares for every one pre-split
share). The check verifies that the ingestion's split-adjusted close on
``split_date - 1`` matches ``split_date`` (i.e. ratio ~ 1.0); a missed split
would yield a ratio near `ratio_den / ratio_num` (e.g. 0.25 for a 4:1).
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from datetime import date
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict


class SplitEvent(BaseModel):
    """One forward-split event."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str
    split_date: date
    ratio_num: int
    ratio_den: int


class SplitsSource(ABC):
    @abstractmethod
    def list_splits(self) -> list[SplitEvent]: ...


_RATIO_RE = re.compile(r"^\s*(\d+)\s*:\s*(\d+)\s*$")


def _parse_ratio(raw: str) -> tuple[int, int]:
    m = _RATIO_RE.match(raw)
    if not m:
        raise ValueError(f"invalid split ratio {raw!r} — expected 'N:M'")
    num = int(m.group(1))
    den = int(m.group(2))
    if num <= 0 or den <= 0:
        raise ValueError(f"split ratio components must be positive: {raw!r}")
    return num, den


class FixtureSplitsSource(SplitsSource):
    """Loads `SplitEvent` rows from a hand-curated YAML fixture."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or _default_fixture("splits.yaml")
        self._events = self._load()

    def list_splits(self) -> list[SplitEvent]:
        return list(self._events)

    def _load(self) -> list[SplitEvent]:
        if not self._path.exists():
            raise FileNotFoundError(self._path)
        raw = yaml.safe_load(self._path.read_text(encoding="utf-8"))
        if not isinstance(raw, list) or not raw:
            raise ValueError(f"splits fixture {self._path} is empty or not a list")
        events: list[SplitEvent] = []
        for entry in raw:
            num, den = _parse_ratio(entry["ratio"])
            events.append(
                SplitEvent(
                    ticker=entry["ticker"],
                    split_date=entry["split_date"],
                    ratio_num=num,
                    ratio_den=den,
                )
            )
        return events


def _default_fixture(name: str) -> Path:
    """Resolve a fixture file shipped under `tpcore/quality/validation/fixtures/`."""
    return Path(__file__).resolve().parent.parent / "fixtures" / name


__all__ = ["FixtureSplitsSource", "SplitEvent", "SplitsSource"]
