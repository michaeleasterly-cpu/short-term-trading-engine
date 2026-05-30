"""Tests for the cadence-routed fundamentals_quarterly_completeness invariant.

**P1 update (2026-05-30)** — synthetic rows now carry the
``sec_document_type_primary`` field so the routing path lights up.
Failure reason strings updated to the P1 ``missing_period_<form>``
shape; liveness constants split per-cadence; helper signatures
updated to the keyword-only contract.

Each test injects a precise per-ticker filing pattern into a fake pool
and asserts the check's verdict. The gap-detection math is shared with
the canonical implementation (``_infer_missing_period_ends``) to avoid
bug-for-bug duplication; what tests pin is BEHAVIOR (gap sensitivity,
cadence routing, liveness gate, healer symmetry).
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from tpcore.quality.validation.checks.fundamentals_quarterly_completeness import (
    LIVE_WITHIN_DAYS_ANNUAL,
    LIVE_WITHIN_DAYS_QUARTERLY,
    MAX_ANNUAL_GAP_DAYS,
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
    primary: str = "10-Q",
) -> list[dict[str, Any]]:
    """Build n quarterly filings for ticker starting at `start`.

    Default primary form = '10-Q' (US quarterly filer); pass another
    form to exercise alternate routing.
    """
    return [
        {
            "ticker": ticker,
            "period_end_date": start + timedelta(days=days_step * i),
            "sec_document_type_primary": primary,
        }
        for i in range(n)
    ]


def _annual_filings(
    ticker: str,
    start: date,
    n: int,
    days_step: int = 365,
    primary: str = "20-F",
) -> list[dict[str, Any]]:
    """Build n annual filings for ticker starting at `start`."""
    return [
        {
            "ticker": ticker,
            "period_end_date": start + timedelta(days=days_step * i),
            "sec_document_type_primary": primary,
        }
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
    # P1: reason is now form-tagged.
    assert aapl[0].reason == "missing_period_10-Q"
    assert "1 inferred missing period" in aapl[0].observed


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
    assert "2 inferred missing period" in aapl[0].observed


# ── C6 — liveness gate excludes dark tickers (per-cadence) ───────────


async def test_C6_dark_ticker_excluded_not_flagged() -> None:
    """Per-P1, the liveness gate is cadence-routed. A 10-Q filer
    silent > LIVE_WITHIN_DAYS_QUARTERLY (120) days is dark; gaps
    not flagged."""
    today = datetime.now(UTC).date()
    last = today - timedelta(days=LIVE_WITHIN_DAYS_QUARTERLY + 60)
    rows = [
        {
            "ticker": "DEAD",
            "period_end_date": last - timedelta(days=91 * i),
            "sec_document_type_primary": "10-Q",
        }
        for i in range(4)
    ][::-1]
    # Inject a deliberate gap.
    rows.pop(1)
    result = await check_fundamentals_quarterly_completeness(_Pool(rows))
    dead_failures = [f for f in result.failures if f.ticker == "DEAD"]
    assert dead_failures == [], (
        f"DEAD ticker (last filing {LIVE_WITHIN_DAYS_QUARTERLY + 60}d ago) "
        f"must be excluded by liveness gate, not gap-flagged"
    )


# ── C6b — annual liveness gate is wider (P1 new) ─────────────────────


async def test_C6b_annual_liveness_gate_is_wider() -> None:
    """A 20-F filer 200 days past their last filing is NOT dark
    (LIVE_WITHIN_DAYS_ANNUAL=540 covers it). Pre-P1 the single
    120-day window would have darkened them silently."""
    today = datetime.now(UTC).date()
    rows = [
        {
            "ticker": "AER",
            "period_end_date": today - timedelta(days=560),
            "sec_document_type_primary": "20-F",
        },
        {
            "ticker": "AER",
            "period_end_date": today - timedelta(days=200),
            "sec_document_type_primary": "20-F",
        },
    ]
    result = await check_fundamentals_quarterly_completeness(_Pool(rows))
    # 360-day gap < 450 → no FAIL; 200-day silence < 540 → not dark.
    assert result.passed is True


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


# ── Pure-helper tests (P1 signature: kw-only max_gap_days + period_days)


def test_infer_missing_returns_empty_for_in_threshold_gap() -> None:
    earlier = date(2024, 3, 31)
    later = date(2024, 6, 30)  # 91 days — within threshold
    assert _infer_missing_period_ends(
        earlier, later,
        max_gap_days=MAX_QUARTERLY_GAP_DAYS, period_days=92,
    ) == []


def test_infer_missing_returns_one_for_two_quarter_gap() -> None:
    earlier = date(2024, 3, 31)
    later = date(2024, 9, 30)  # 183 days — one missing quarter
    inferred = _infer_missing_period_ends(
        earlier, later,
        max_gap_days=MAX_QUARTERLY_GAP_DAYS, period_days=92,
    )
    assert len(inferred) == 1


def test_infer_missing_returns_two_for_three_quarter_gap() -> None:
    earlier = date(2024, 3, 31)
    later = date(2024, 12, 31)  # 275 days — two missing quarters
    inferred = _infer_missing_period_ends(
        earlier, later,
        max_gap_days=MAX_QUARTERLY_GAP_DAYS, period_days=92,
    )
    assert len(inferred) == 2


def test_infer_missing_annual_cadence_threshold() -> None:
    """At annual cadence: 380-day gap < 450-day cap → no missing."""
    earlier = date(2024, 1, 1)
    later = date(2025, 1, 15)  # 380 days
    inferred = _infer_missing_period_ends(
        earlier, later,
        max_gap_days=MAX_ANNUAL_GAP_DAYS, period_days=365,
    )
    assert inferred == []


def test_infer_missing_annual_cadence_year_skip() -> None:
    """At annual cadence: 730-day gap > 450 → one missing year."""
    earlier = date(2023, 1, 1)
    later = date(2024, 12, 31)  # 730 days
    inferred = _infer_missing_period_ends(
        earlier, later,
        max_gap_days=MAX_ANNUAL_GAP_DAYS, period_days=365,
    )
    assert len(inferred) == 1


def test_max_quarterly_gap_constant_is_100() -> None:
    # The bound is math-derived (Q4=92 days max + 8-day slack), not
    # tunable. Lock it in.
    assert MAX_QUARTERLY_GAP_DAYS == 100


def test_max_annual_gap_constant_is_450() -> None:
    # P1: 365 + 85 days slack for late 20-F filers without
    # false-firing on a true skip (~730 days).
    assert MAX_ANNUAL_GAP_DAYS == 450


def test_live_within_days_constants_per_cadence() -> None:
    """P1: per-cadence liveness gates."""
    assert LIVE_WITHIN_DAYS_QUARTERLY == 120
    assert LIVE_WITHIN_DAYS_ANNUAL == 540


# ── P1 NEW — 20-F routing positive case ──────────────────────────────


async def test_C11_20f_annual_routing_passes() -> None:
    """P1 fix: a 20-F filer with annual filings PASSES (pre-P1 would
    have FAILED as missing_quarter)."""
    today = datetime.now(UTC).date()
    rows = _annual_filings("AER", today - timedelta(days=730), 3, primary="20-F")
    result = await check_fundamentals_quarterly_completeness(_Pool(rows))
    assert result.passed is True


async def test_C12_40f_annual_routing_passes() -> None:
    """P1 fix: a 40-F (Canadian MJDS) filer routes as annual."""
    today = datetime.now(UTC).date()
    rows = _annual_filings("ASTL", today - timedelta(days=730), 3, primary="40-F")
    result = await check_fundamentals_quarterly_completeness(_Pool(rows))
    assert result.passed is True
