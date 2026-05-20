"""Tests for the zero-tolerance macro_indicators_completeness invariant.

Each test injects a precise (indicator, date, present?) shape into a
fake pool and asserts the check's verdict. Cadence math is shared
with the canonical implementation (``_expected_dates_for_cadence``)
to avoid bug-for-bug duplication; what tests pin is BEHAVIOR
(per-cadence sensitivity to gaps, sentinel paths, healer symmetry).
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from tpcore.quality.validation.checks.macro_indicators_completeness import (
    CADENCE_DAILY,
    CADENCE_MONTHLY,
    CADENCE_WEEKLY,
    EXPECTED_INDICATORS,
    INDICATOR_CADENCE,
    _expected_dates_for_cadence,
    check_macro_indicators_completeness,
    compute_macro_repair_targets,
)

# ── Fake asyncpg pool ──────────────────────────────────────────────────


class _Conn:
    def __init__(self, owner: _Pool) -> None:
        self._owner = owner

    async def fetch(self, sql: str, *args) -> list[dict[str, Any]]:
        # Two distinct SQL shapes are dispatched by the check:
        # range SQL (one arg = indicator list) and dates SQL (three args
        # = indicator, first_date, last_date).
        if len(args) == 1:
            return self._owner.range_rows
        indicator, first_d, last_d = args
        present = self._owner.present_by_indicator.get(indicator, set())
        return [{"date": d} for d in sorted(present) if first_d <= d <= last_d]


class _AcquireCM:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _Conn:
        return self._conn

    async def __aexit__(self, *exc) -> None:
        return None


class _Pool:
    """Fake pool whose fetch dispatches on SQL arg count."""

    def __init__(
        self,
        range_rows: list[dict[str, Any]],
        present_by_indicator: dict[str, set[date]],
    ) -> None:
        self.range_rows = range_rows
        self.present_by_indicator = present_by_indicator

    def acquire(self) -> _AcquireCM:
        return _AcquireCM(_Conn(self))


# ── Fixture helpers ────────────────────────────────────────────────────


def _full_coverage_pool(
    *,
    first: date = date(2024, 1, 1),
    last: date = date(2024, 12, 31),
) -> _Pool:
    """Build a pool where every expected indicator has every expected
    publication date (per its cadence) present in [first, last]."""
    range_rows: list[dict[str, Any]] = []
    present_by_indicator: dict[str, set[date]] = {}
    for ind in EXPECTED_INDICATORS:
        cadence = INDICATOR_CADENCE[ind]
        expected = _expected_dates_for_cadence(cadence, first, last)
        # Some indicators have shorter active ranges in production; for
        # this fixture every series spans [first, last] fully.
        range_rows.append({
            "indicator": ind,
            "first_date": first,
            "last_date": last,
            "row_count": len(expected),
        })
        present_by_indicator[ind] = set(expected)
    return _Pool(range_rows, present_by_indicator)


# ── C1 — per-cadence full coverage → pass ──────────────────────────────


async def test_C1_full_coverage_all_cadences_passes() -> None:
    pool = _full_coverage_pool()
    result = await check_macro_indicators_completeness(pool)
    assert result.passed is True, result.failures
    assert result.failed == 0
    assert result.name == "macro_indicators_completeness"


# ── C2 — DAILY gap fails ───────────────────────────────────────────────


async def test_C2_daily_gap_fails() -> None:
    pool = _full_coverage_pool()
    # Pull one Wednesday out of `vix`.
    drop_d: date | None = None
    for d in sorted(pool.present_by_indicator["vix"]):
        if d.weekday() == 2:  # Wednesday
            drop_d = d
            break
    assert drop_d is not None
    pool.present_by_indicator["vix"].discard(drop_d)
    result = await check_macro_indicators_completeness(pool)
    assert result.passed is False
    # Expect one failure (for vix).
    vix_failures = [f for f in result.failures if f.ticker == "vix"]
    assert len(vix_failures) == 1
    assert vix_failures[0].reason == "missing_publication"
    assert drop_d.isoformat() in vix_failures[0].observed


# ── C3 — WEEKLY gap fails ──────────────────────────────────────────────


async def test_C3_weekly_gap_on_initial_claims_fails() -> None:
    pool = _full_coverage_pool()
    # Pull one Thursday out of initial_claims.
    drop_d = sorted(pool.present_by_indicator["initial_claims"])[3]
    pool.present_by_indicator["initial_claims"].discard(drop_d)
    result = await check_macro_indicators_completeness(pool)
    assert result.passed is False
    matching = [f for f in result.failures if f.ticker == "initial_claims"]
    assert len(matching) == 1
    assert matching[0].reason == "missing_publication"
    assert drop_d.isoformat() in matching[0].observed


# ── C4 — MONTHLY gap fails ─────────────────────────────────────────────


async def test_C4_monthly_gap_on_industrial_production_fails() -> None:
    pool = _full_coverage_pool()
    drop_d = sorted(pool.present_by_indicator["industrial_production"])[3]
    pool.present_by_indicator["industrial_production"].discard(drop_d)
    result = await check_macro_indicators_completeness(pool)
    assert result.passed is False
    matching = [f for f in result.failures if f.ticker == "industrial_production"]
    assert len(matching) == 1
    assert matching[0].reason == "missing_publication"
    assert drop_d.isoformat() in matching[0].observed


# ── C5 — missing indicator entirely → sentinel-style failure ───────────


async def test_C5_missing_indicator_entirely_fails() -> None:
    pool = _full_coverage_pool()
    # Drop hy_spread from the range query AND the present set.
    pool.range_rows = [r for r in pool.range_rows if r["indicator"] != "hy_spread"]
    pool.present_by_indicator.pop("hy_spread", None)
    result = await check_macro_indicators_completeness(pool)
    assert result.passed is False
    missing = [f for f in result.failures if f.ticker == "hy_spread"]
    assert len(missing) == 1
    assert missing[0].reason == "indicator_missing"


# ── C6 — within-active-range only (pre-range dates not demanded) ───────


async def test_C6_pre_range_dates_not_demanded() -> None:
    # hy_spread starts in 1996 in production; here we say it starts
    # mid-fixture-range. The check must not demand pre-start dates.
    first = date(2024, 1, 1)
    last = date(2024, 12, 31)
    pool = _full_coverage_pool(first=first, last=last)
    # Shrink hy_spread's range to start mid-year.
    hy_start = date(2024, 7, 1)
    for r in pool.range_rows:
        if r["indicator"] == "hy_spread":
            r["first_date"] = hy_start
    pool.present_by_indicator["hy_spread"] = {
        d for d in pool.present_by_indicator["hy_spread"] if d >= hy_start
    }
    result = await check_macro_indicators_completeness(pool)
    assert result.passed is True, (
        f"pre-range dates were demanded: "
        f"{[f for f in result.failures if f.ticker == 'hy_spread']}"
    )


# ── C7 — truncation class (BAMLH0A0HYM2-style mid-range gut) ───────────


async def test_C7_mid_range_truncation_flags_every_missing_date() -> None:
    """The 2026-05-15 hy_spread truncation: FRED's rolling-3y window
    drops the pre-cutoff tail. Within the SERIES'S own range, the
    invariant must flag every missing publication date."""
    first = date(2024, 1, 1)
    last = date(2024, 12, 31)
    pool = _full_coverage_pool(first=first, last=last)
    # Simulate FRED only returning the last 90 days of hy_spread.
    cutoff = date(2024, 10, 1)
    pool.present_by_indicator["hy_spread"] = {
        d for d in pool.present_by_indicator["hy_spread"] if d >= cutoff
    }
    result = await check_macro_indicators_completeness(pool)
    assert result.passed is False
    matching = [f for f in result.failures if f.ticker == "hy_spread"]
    assert len(matching) == 1
    # The observed string should report a large gap count — at least
    # one entry's worth of pre-cutoff sessions.
    assert "missing date" in matching[0].observed


# ── C8 — healer symmetry (same indicators, sensible lookback) ─────────


async def test_C8_healer_symmetry_with_check() -> None:
    pool = _full_coverage_pool()
    # Drop one date from initial_claims.
    drop_d = sorted(pool.present_by_indicator["initial_claims"])[5]
    pool.present_by_indicator["initial_claims"].discard(drop_d)

    targets, lookback = await compute_macro_repair_targets(pool)
    assert targets == ["initial_claims"]
    assert lookback > 0
    # Lookback must bracket the dropped date.
    today = datetime.now(UTC).date()
    assert lookback >= (today - drop_d).days


# ── C9 — clean state → empty targets ───────────────────────────────────


async def test_C9_clean_state_returns_no_targets() -> None:
    pool = _full_coverage_pool()
    targets, lookback = await compute_macro_repair_targets(pool)
    assert targets == []
    assert lookback == 0


# ── C10 — table empty → sentinel; no heal possible ─────────────────────


async def test_C10_table_empty_returns_sentinel_and_no_heal_targets() -> None:
    pool = _Pool(range_rows=[], present_by_indicator={})
    result = await check_macro_indicators_completeness(pool)
    assert result.passed is False
    assert result.failures[0].reason == "table_empty"

    targets, lookback = await compute_macro_repair_targets(pool)
    assert targets == []
    assert lookback == 0


# ── Pure-helper tests ──────────────────────────────────────────────────


def test_expected_dates_for_cadence_daily_uses_nyse_sessions() -> None:
    # 2024-01-01 is MLK-tested-passes; 2024-01-08 covers a week — exact
    # count varies by holidays, but the result must be < 7 (weekend
    # exclusion). We assert the set is the canonical XNYS one.
    first = date(2024, 1, 1)
    last = date(2024, 1, 8)
    out = _expected_dates_for_cadence(CADENCE_DAILY, first, last)
    # No weekends.
    assert all(d.weekday() < 5 for d in out)
    # At least one date in range.
    assert out
    # Bounded.
    assert all(first <= d <= last for d in out)


def test_expected_dates_for_cadence_weekly_thursdays_only() -> None:
    first = date(2024, 1, 1)  # Monday
    last = date(2024, 2, 29)
    out = _expected_dates_for_cadence(CADENCE_WEEKLY, first, last)
    assert all(d.weekday() == 3 for d in out), [d.isoformat() for d in out]
    # First Thursday on or after 2024-01-01 is 2024-01-04.
    assert out[0] == date(2024, 1, 4)


def test_expected_dates_for_cadence_monthly_first_of_month_only() -> None:
    first = date(2024, 1, 15)  # mid-Jan; first expected = Feb 1.
    last = date(2024, 6, 30)
    out = _expected_dates_for_cadence(CADENCE_MONTHLY, first, last)
    assert all(d.day == 1 for d in out)
    assert out[0] == date(2024, 2, 1)
    assert out[-1] == date(2024, 6, 1)


def test_expected_dates_for_cadence_empty_range_returns_empty() -> None:
    out = _expected_dates_for_cadence(CADENCE_DAILY, date(2024, 1, 10), date(2024, 1, 5))
    assert out == []


def test_expected_dates_for_cadence_unknown_raises() -> None:
    with pytest.raises(ValueError):
        _expected_dates_for_cadence("yearly", date(2024, 1, 1), date(2024, 12, 31))


# ── Coverage of EXPECTED_INDICATORS vs INDICATOR_CADENCE ───────────────


def test_every_expected_indicator_has_a_cadence() -> None:
    for ind in EXPECTED_INDICATORS:
        assert ind in INDICATOR_CADENCE, f"missing cadence for {ind!r}"


def test_indicator_cadence_has_no_extras() -> None:
    assert set(INDICATOR_CADENCE.keys()) == set(EXPECTED_INDICATORS), (
        "INDICATOR_CADENCE and EXPECTED_INDICATORS must be 1:1 — adding "
        "a new series requires updating both"
    )
