"""Tests for the zero-tolerance fundamentals_quarterly_completeness invariant.

Each test injects a precise per-ticker filing pattern into a fake pool
and asserts the check's verdict. The gap-detection math is shared with
the canonical implementation (``_infer_missing_period_ends``) to avoid
bug-for-bug duplication; what tests pin is BEHAVIOR (gap sensitivity,
universe boundary, liveness gate, healer symmetry).
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from tpcore.quality.validation.checks.fundamentals_quarterly_completeness import (
    LIVE_WITHIN_DAYS,
    MAX_QUARTERLY_GAP_DAYS,
    _infer_missing_period_ends,
    check_fundamentals_quarterly_completeness,
    compute_fundamentals_repair_targets,
)


class _Conn:
    def __init__(self, owner: _Pool) -> None:
        self._owner = owner

    async def fetch(self, sql: str, *args) -> list[dict[str, Any]]:
        # The check issues a single SQL with args = (tier_max,).
        return self._owner.rows


class _AcquireCM:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _Conn:
        return self._conn

    async def __aexit__(self, *exc) -> None:
        return None


class _Pool:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def acquire(self) -> _AcquireCM:
        return _AcquireCM(_Conn(self))


def _quarterly_filings(
    ticker: str,
    start: date,
    n: int,
    days_step: int = 91,
) -> list[dict[str, Any]]:
    """Build n quarterly filings for ticker starting at `start`."""
    return [
        {"ticker": ticker, "period_end_date": start + timedelta(days=days_step * i)}
        for i in range(n)
    ]


# ── C1 — clean quarterly cadence passes ──────────────────────────────


async def test_C1_clean_quarterly_cadence_passes() -> None:
    today = datetime.now(UTC).date()
    # 8 quarters ending recently (well within live window).
    rows = _quarterly_filings("AAPL", today - timedelta(days=91 * 7), 8)
    result = await check_fundamentals_quarterly_completeness(_Pool(rows))
    assert result.passed is True, [f.observed for f in result.failures]
    assert result.failed == 0
    assert result.name == "fundamentals_quarterly_completeness"


# ── C2 — single missing quarter fails ────────────────────────────────


async def test_C2_single_missing_quarter_fails() -> None:
    today = datetime.now(UTC).date()
    rows = _quarterly_filings("AAPL", today - timedelta(days=91 * 7), 8)
    # Drop a middle filing — gap becomes ~182 days, one missing quarter.
    rows.pop(3)
    result = await check_fundamentals_quarterly_completeness(_Pool(rows))
    assert result.passed is False
    aapl = [f for f in result.failures if f.ticker == "AAPL"]
    assert len(aapl) == 1
    assert aapl[0].reason == "missing_quarter"
    assert "1 inferred missing quarter" in aapl[0].observed


# ── C3 — two consecutive missing quarters fails with N=2 ─────────────


async def test_C3_two_missing_quarters_fails() -> None:
    today = datetime.now(UTC).date()
    rows = _quarterly_filings("AAPL", today - timedelta(days=91 * 9), 10)
    # Drop two consecutive — gap ~273 days, two missing quarters inferred.
    del rows[3:5]
    result = await check_fundamentals_quarterly_completeness(_Pool(rows))
    assert result.passed is False
    aapl = [f for f in result.failures if f.ticker == "AAPL"]
    assert len(aapl) == 1
    assert "2 inferred missing quarter" in aapl[0].observed


# ── C6 — liveness gate excludes dark tickers ─────────────────────────


async def test_C6_dark_ticker_excluded_not_flagged() -> None:
    # Last filing must be > LIVE_WITHIN_DAYS ago → dark; gaps NOT flagged.
    # Build 4 quarters whose LAST one is well before the live cutoff.
    today = datetime.now(UTC).date()
    # Place the LAST filing at today - (LIVE_WITHIN_DAYS + 60); work backward.
    last = today - timedelta(days=LIVE_WITHIN_DAYS + 60)
    rows = [
        {"ticker": "DEAD", "period_end_date": last - timedelta(days=91 * i)}
        for i in range(4)
    ][::-1]
    # Inject a deliberate gap.
    rows.pop(1)
    result = await check_fundamentals_quarterly_completeness(_Pool(rows))
    dead_failures = [f for f in result.failures if f.ticker == "DEAD"]
    assert dead_failures == [], (
        f"DEAD ticker (last filing {LIVE_WITHIN_DAYS + 60}d ago) must be "
        f"excluded by liveness gate, not gap-flagged"
    )


# ── C7 — pre-IPO quarters not demanded ───────────────────────────────


async def test_C7_pre_ipo_quarters_not_demanded() -> None:
    today = datetime.now(UTC).date()
    # Recently-IPOd ticker — only 3 quarters, no gap within active range.
    rows = _quarterly_filings("NEWCO", today - timedelta(days=91 * 2), 3)
    result = await check_fundamentals_quarterly_completeness(_Pool(rows))
    assert result.passed is True


# ── C8 — healer symmetry ─────────────────────────────────────────────


async def test_C8_healer_symmetry_with_check() -> None:
    today = datetime.now(UTC).date()
    rows = _quarterly_filings("AAPL", today - timedelta(days=91 * 7), 8)
    rows.pop(2)
    pool = _Pool(rows)

    result = await check_fundamentals_quarterly_completeness(pool)
    targets, lookback = await compute_fundamentals_repair_targets(pool)

    assert result.passed is False
    assert targets == ["AAPL"]
    assert lookback > 0


# ── C9 — clean state → empty targets ─────────────────────────────────


async def test_C9_clean_state_returns_empty_targets() -> None:
    today = datetime.now(UTC).date()
    rows = _quarterly_filings("AAPL", today - timedelta(days=91 * 7), 8)
    targets, lookback = await compute_fundamentals_repair_targets(_Pool(rows))
    assert targets == []
    assert lookback == 0


# ── C10 — empty universe → sentinel, no targets ──────────────────────


async def test_C10_empty_universe_returns_sentinel_no_targets() -> None:
    pool = _Pool([])
    result = await check_fundamentals_quarterly_completeness(pool)
    assert result.passed is False
    assert result.failures[0].reason == "empty_liquid_universe"

    targets, lookback = await compute_fundamentals_repair_targets(pool)
    assert targets == []
    assert lookback == 0


# ── Pure-helper tests ────────────────────────────────────────────────


def test_infer_missing_returns_empty_for_in_threshold_gap() -> None:
    earlier = date(2024, 3, 31)
    later = date(2024, 6, 30)  # 91 days — within threshold
    assert _infer_missing_period_ends(earlier, later) == []


def test_infer_missing_returns_one_for_two_quarter_gap() -> None:
    earlier = date(2024, 3, 31)
    later = date(2024, 9, 30)  # 183 days — one missing quarter
    inferred = _infer_missing_period_ends(earlier, later)
    assert len(inferred) == 1


def test_infer_missing_returns_two_for_three_quarter_gap() -> None:
    earlier = date(2024, 3, 31)
    later = date(2024, 12, 31)  # 275 days — two missing quarters
    inferred = _infer_missing_period_ends(earlier, later)
    assert len(inferred) == 2


def test_max_quarterly_gap_constant_is_100() -> None:
    # The bound is math-derived (Q4=92 days max + 8-day slack), not
    # tunable. Lock it in.
    assert MAX_QUARTERLY_GAP_DAYS == 100
