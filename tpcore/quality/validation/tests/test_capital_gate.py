"""Tests for `assert_passed` and its error classes."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tpcore.quality.validation.capital_gate import (
    ValidationFailedError,
    ValidationStaleError,
    assert_passed,
)


class _DQLogFakePool:
    """Fake pool that serves rows from `platform.data_quality_log` only."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, tuple]] = []

    def acquire(self) -> _AcquireCM:
        return _AcquireCM(self)

    async def fetch(self, sql: str, *args) -> list[dict[str, Any]]:
        self.calls.append((sql, args))
        return list(self.rows)


class _AcquireCM:
    def __init__(self, pool: _DQLogFakePool) -> None:
        self.pool = pool

    async def __aenter__(self) -> _DQLogFakePool:
        return self.pool

    async def __aexit__(self, *exc) -> None:
        return None


def _row(source: str, ts: datetime, stale: bool) -> dict[str, Any]:
    return {"source": source, "timestamp": ts, "stale": stale}


def _all_three(ts: datetime, stale: bool = False) -> list[dict[str, Any]]:
    """Returns all 10 expected sources at the same timestamp.

    Function name is historical (was 3 sources pre-2026-05-14) — kept to
    minimize test churn after D3-1 expanded EXPECTED_SOURCES to derive
    from suite.KNOWN_CHECK_NAMES.
    """
    from tpcore.quality.validation.suite import KNOWN_CHECK_NAMES
    return [
        _row(f"validation.{name}", ts, stale) for name in KNOWN_CHECK_NAMES
    ]


# ────────────────────────────────────────────────────────────────────────────
# Pass
# ────────────────────────────────────────────────────────────────────────────


async def test_assert_passed_returns_silently_when_recent_and_clean() -> None:
    ts = datetime.now(UTC) - timedelta(days=1)
    pool = _DQLogFakePool(_all_three(ts, stale=False))
    await assert_passed(pool)  # no exception


# ────────────────────────────────────────────────────────────────────────────
# Stale
# ────────────────────────────────────────────────────────────────────────────


async def test_assert_passed_raises_stale_when_no_rows() -> None:
    pool = _DQLogFakePool([])
    with pytest.raises(ValidationStaleError):
        await assert_passed(pool)


async def test_assert_passed_raises_stale_when_run_older_than_max_age() -> None:
    ts = datetime.now(UTC) - timedelta(days=14)
    pool = _DQLogFakePool(_all_three(ts, stale=False))
    with pytest.raises(ValidationStaleError):
        await assert_passed(pool, max_age_days=7)


# ────────────────────────────────────────────────────────────────────────────
# Failed
# ────────────────────────────────────────────────────────────────────────────


async def test_assert_passed_raises_failed_when_one_check_stale() -> None:
    """All 10 sources present, but one is marked stale → ValidationFailedError."""
    ts = datetime.now(UTC) - timedelta(days=1)
    rows = _all_three(ts, stale=False)
    # Mutate one row to be stale (post-2026-05-14 D3-1: pick any of the 10).
    for r in rows:
        if r["source"] == "validation.constituent":
            r["stale"] = True
    pool = _DQLogFakePool(rows)
    with pytest.raises(ValidationFailedError) as excinfo:
        await assert_passed(pool)
    assert "validation.constituent" in str(excinfo.value)


async def test_assert_passed_raises_failed_when_a_source_missing_from_latest_run() -> None:
    """Latest timestamp is missing at least one of the 10 expected sources → failure."""
    ts = datetime.now(UTC) - timedelta(days=1)
    rows = [r for r in _all_three(ts, stale=False) if r["source"] != "validation.splits"]
    pool = _DQLogFakePool(rows)
    with pytest.raises(ValidationFailedError):
        await assert_passed(pool)


# ────────────────────────────────────────────────────────────────────────────
# Most-recent-run isolation: an older successful run doesn't rescue a recent failure.
# ────────────────────────────────────────────────────────────────────────────


async def test_assert_passed_uses_only_most_recent_run() -> None:
    recent = datetime.now(UTC) - timedelta(days=1)
    older = recent - timedelta(days=8)
    rows = [
        # Older run: all clean
        *_all_three(older, stale=False),
        # Recent run: one failed
        _row("validation.delistings", recent, stale=True),
        _row("validation.constituent", recent, stale=False),
        _row("validation.splits", recent, stale=False),
    ]
    pool = _DQLogFakePool(rows)
    with pytest.raises(ValidationFailedError):
        await assert_passed(pool)
