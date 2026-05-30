"""P1 — fundamentals_quarterly_completeness cadence-routed tests.

Hermetic tests of the new 5-state routing logic, using a mocked
asyncpg pool that returns synthetic filing rows. NO database access.

Coverage matrix:
  TEST-P1-01  10-Q filer with no gaps             → PASS
  TEST-P1-02  10-Q filer with a >100-day gap      → FAIL (missing_period_10-Q)
  TEST-P1-03  20-F filer with no gaps             → PASS (NOT false-FAIL'd)
  TEST-P1-04  20-F filer with a 470-day gap       → FAIL (missing_period_20-F)
  TEST-P1-05  20-F filer with a 380-day gap       → PASS (within 450-day cap)
  TEST-P1-06  40-F filer routes as annual         → PASS at 365-day gap
  TEST-P1-07  NULL doctype → METADATA_REQUIRED bucket (NOT a per-ticker FAIL)
  TEST-P1-08  '6-K' primary → excluded_other_form bucket (NOT a per-ticker FAIL)
  TEST-P1-09  cadence-routed liveness: 20-F filer 200 days silent → still routed
              (would be dark under pre-P1 LIVE_WITHIN_DAYS=120; under P1 it lives)
  TEST-P1-10  metadata-coverage sentinel fires at >25% NULL
  TEST-P1-11  metadata-coverage sentinel does NOT fire at <25% NULL
  TEST-P1-12  compute_fundamentals_repair_targets returns ONLY routed-FAIL
              tickers (NEVER metadata-required / confirmed-data-gap / sentinels)
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from tpcore.quality.validation.checks.fundamentals_quarterly_completeness import (
    CHECK_NAME,
    LIVE_WITHIN_DAYS_ANNUAL,
    LIVE_WITHIN_DAYS_QUARTERLY,
    MAX_ANNUAL_GAP_DAYS,
    MAX_QUARTERLY_GAP_DAYS,
    METADATA_COVERAGE_FAIL_THRESHOLD,
    check_fundamentals_quarterly_completeness,
    compute_fundamentals_repair_targets,
)


def _today() -> date:
    return datetime.now(UTC).date()


def _mock_pool(rows: list[dict]) -> MagicMock:
    """asyncpg.Pool stub whose ``acquire().fetch(_SQL, $1)`` yields rows."""
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=rows)
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire)
    return pool


def _row(ticker: str, period_end: date, primary: str | None) -> dict:
    return {
        "ticker": ticker,
        "period_end_date": period_end,
        "sec_document_type_primary": primary,
    }


# ─────────────────────────────────────────────────────────────────────
# TEST-P1-01..05 — 10-Q / 20-F / 40-F cadence routing
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_p1_01_10q_no_gaps_passes() -> None:
    """A 10-Q filer with quarterly filings (≤100 day gaps) PASSES,
    contributes to evaluated_routed + by_form['10-Q'] = 1."""
    today = _today()
    # 4 quarterly filings ending within the liveness window.
    rows = [
        _row("AAPL", today - timedelta(days=300), "10-Q"),
        _row("AAPL", today - timedelta(days=210), "10-Q"),
        _row("AAPL", today - timedelta(days=120), "10-Q"),
        _row("AAPL", today - timedelta(days=30),  "10-Q"),
    ]
    pool = _mock_pool(rows)
    # We're sole ticker → coverage_ratio=0 → no sentinel
    result = await check_fundamentals_quarterly_completeness(pool)
    assert result.passed is True
    assert result.failed == 0


@pytest.mark.asyncio
async def test_p1_02_10q_with_gap_fails_missing_period_10q() -> None:
    """A 10-Q filer with a > 100-day gap between filings FAILS with
    ``missing_period_10-Q``."""
    today = _today()
    rows = [
        _row("AAPL", today - timedelta(days=400), "10-Q"),
        # 200-day gap below → missing quarter inferred.
        _row("AAPL", today - timedelta(days=200), "10-Q"),
        _row("AAPL", today - timedelta(days=30),  "10-Q"),
    ]
    pool = _mock_pool(rows)
    result = await check_fundamentals_quarterly_completeness(pool)
    assert result.passed is False
    assert any(
        f.reason == "missing_period_10-Q" and f.ticker == "AAPL"
        for f in result.failures
    )


@pytest.mark.asyncio
async def test_p1_03_20f_no_gaps_passes() -> None:
    """A 20-F filer with annual filings at ~365-day intervals PASSES.
    This is the dispositive P1 fix: pre-P1 this ticker would FAIL as
    missing_quarter; P1 routes by 20-F → annual → 450-day max gap."""
    today = _today()
    rows = [
        _row("AER", today - timedelta(days=730), "20-F"),
        _row("AER", today - timedelta(days=365), "20-F"),
        _row("AER", today - timedelta(days=30),  "20-F"),
    ]
    pool = _mock_pool(rows)
    result = await check_fundamentals_quarterly_completeness(pool)
    assert result.passed is True
    assert result.failed == 0


@pytest.mark.asyncio
async def test_p1_04_20f_with_year_skip_fails_missing_period_20f() -> None:
    """A 20-F filer with a > 450-day gap between filings FAILS with
    ``missing_period_20-F``."""
    today = _today()
    rows = [
        _row("AER", today - timedelta(days=900), "20-F"),
        # 460-day gap below → year-skip inferred.
        _row("AER", today - timedelta(days=440), "20-F"),
        _row("AER", today - timedelta(days=30),  "20-F"),
    ]
    pool = _mock_pool(rows)
    result = await check_fundamentals_quarterly_completeness(pool)
    assert result.passed is False
    assert any(
        f.reason == "missing_period_20-F" and f.ticker == "AER"
        for f in result.failures
    )


@pytest.mark.asyncio
async def test_p1_05_20f_within_annual_cap_passes() -> None:
    """A 20-F filer with a 380-day gap (within 450-day annual cap)
    PASSES — covers the legitimate-late-filer case the constant was
    sized for."""
    today = _today()
    rows = [
        _row("AER", today - timedelta(days=380), "20-F"),
        _row("AER", today - timedelta(days=15),  "20-F"),
    ]
    pool = _mock_pool(rows)
    result = await check_fundamentals_quarterly_completeness(pool)
    assert result.passed is True


# ─────────────────────────────────────────────────────────────────────
# TEST-P1-06 — 40-F (Canadian MJDS) routes as annual
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_p1_06_40f_routes_as_annual() -> None:
    today = _today()
    rows = [
        _row("ASTL", today - timedelta(days=730), "40-F"),
        _row("ASTL", today - timedelta(days=365), "40-F"),
        _row("ASTL", today - timedelta(days=30),  "40-F"),
    ]
    pool = _mock_pool(rows)
    result = await check_fundamentals_quarterly_completeness(pool)
    assert result.passed is True


# ─────────────────────────────────────────────────────────────────────
# TEST-P1-07 — NULL doctype → METADATA_REQUIRED (excluded, NOT a FAIL)
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_p1_07_null_doctype_routes_to_metadata_required() -> None:
    """NULL primary form → METADATA_REQUIRED bucket. Does NOT
    contribute a per-ticker FailureDetail (those are cadence
    failures only). But the coverage-ratio sentinel fires if NULL
    >25%."""
    today = _today()
    rows = [
        _row("UNKNOWN", today - timedelta(days=200), None),
        _row("UNKNOWN", today - timedelta(days=50),  None),
        # Add 4 routed-eligible tickers so the coverage ratio is 1/5=20%
        # and the sentinel does NOT fire (testing only the routing
        # mechanic here, not the sentinel — see TEST-P1-10).
        _row("A", today - timedelta(days=60), "10-Q"),
        _row("B", today - timedelta(days=60), "10-Q"),
        _row("C", today - timedelta(days=60), "10-Q"),
        _row("D", today - timedelta(days=60), "10-Q"),
    ]
    pool = _mock_pool(rows)
    result = await check_fundamentals_quarterly_completeness(pool)
    # No per-ticker FAIL for UNKNOWN (it's excluded, not failed) AND
    # coverage 1/5=20% < 25% threshold → no metadata-coverage sentinel.
    assert result.passed is True
    assert not any(f.ticker == "UNKNOWN" for f in result.failures)


# ─────────────────────────────────────────────────────────────────────
# TEST-P1-08 — non-routed primary form → excluded_other_form
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_p1_08_six_k_primary_routes_to_other_form() -> None:
    """A ticker whose extract_filing_metadata picked '6-K' as primary
    (rare — pathological for the operator's universe). Excluded from
    the routed denominator, NOT a per-ticker FAIL."""
    today = _today()
    rows = [
        _row("SIXK", today - timedelta(days=100), "6-K"),
        # Provide enough routed-eligible to avoid metadata sentinel.
        *(_row(f"R{i}", today - timedelta(days=60), "10-Q") for i in range(5)),
    ]
    pool = _mock_pool(rows)
    result = await check_fundamentals_quarterly_completeness(pool)
    assert result.passed is True
    assert not any(f.ticker == "SIXK" for f in result.failures)


# ─────────────────────────────────────────────────────────────────────
# TEST-P1-09 — annual liveness window covers a 200-day silent 20-F
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_p1_09_annual_liveness_window_covers_200_day_silent_20f() -> None:
    """Pre-P1, a 20-F filer 200 days past their last filing would be
    EXCLUDED_DARK (120-day window). P1 widens the annual window to
    540 so they remain routed and properly judged."""
    today = _today()
    rows = [
        _row("AER", today - timedelta(days=560), "20-F"),
        _row("AER", today - timedelta(days=200), "20-F"),
    ]
    pool = _mock_pool(rows)
    result = await check_fundamentals_quarterly_completeness(pool)
    # 360-day gap < 450 → no FAIL; 200-day silence < 540 → not dark.
    assert result.passed is True


# ─────────────────────────────────────────────────────────────────────
# TEST-P1-10..11 — metadata-coverage structural sentinel
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_p1_10_metadata_sentinel_fires_above_threshold() -> None:
    """30% NULL coverage (> 25% threshold) fires the metadata-coverage
    structural sentinel as a synthetic ``<metadata_coverage>``
    FailureDetail."""
    today = _today()
    # 3 NULL + 7 routed-eligible 10-Q → 3/10 = 30% NULL → sentinel.
    rows = []
    for i in range(3):
        rows.append(_row(f"NULL{i}", today - timedelta(days=60), None))
        rows.append(_row(f"NULL{i}", today - timedelta(days=10), None))
    for i in range(7):
        rows.append(_row(f"R{i}", today - timedelta(days=60), "10-Q"))
        rows.append(_row(f"R{i}", today - timedelta(days=10), "10-Q"))
    pool = _mock_pool(rows)
    result = await check_fundamentals_quarterly_completeness(pool)
    assert result.passed is False
    sentinel_failures = [
        f for f in result.failures
        if f.reason == "metadata_coverage_insufficient"
    ]
    assert len(sentinel_failures) == 1
    assert sentinel_failures[0].ticker == "<metadata_coverage>"


@pytest.mark.asyncio
async def test_p1_11_metadata_sentinel_silent_below_threshold() -> None:
    """20% NULL coverage (< 25% threshold) does NOT fire the
    structural sentinel."""
    today = _today()
    # 1 NULL + 4 routed-eligible 10-Q → 1/5 = 20% NULL → no sentinel.
    rows = [_row("NULL0", today - timedelta(days=10), None)]
    for i in range(4):
        rows.append(_row(f"R{i}", today - timedelta(days=60), "10-Q"))
        rows.append(_row(f"R{i}", today - timedelta(days=10), "10-Q"))
    pool = _mock_pool(rows)
    result = await check_fundamentals_quarterly_completeness(pool)
    assert result.passed is True
    assert not any(
        f.reason == "metadata_coverage_insufficient"
        for f in result.failures
    )


# ─────────────────────────────────────────────────────────────────────
# TEST-P1-12 — repair targets exclude METADATA_REQUIRED + sentinels
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_p1_12_repair_targets_only_routed_fails() -> None:
    """``compute_fundamentals_repair_targets`` returns ONLY tickers
    with cadence-FAIL gaps. It NEVER returns METADATA_REQUIRED,
    OTHER_FORM, CONFIRMED_DATA_GAP, or sentinel synthetic tickers.

    Operator hard rule: ``fundamentals_refresh`` re-pulls would burn
    the SEC budget for nothing on metadata-required tickers (the right
    fix is ``backfill_sec_metadata``, not fundamentals re-pull)."""
    today = _today()
    rows = [
        # Cadence FAIL — should be a repair target.
        _row("FAIL10Q", today - timedelta(days=400), "10-Q"),
        _row("FAIL10Q", today - timedelta(days=200), "10-Q"),
        _row("FAIL10Q", today - timedelta(days=30),  "10-Q"),
        # NULL — METADATA_REQUIRED, NOT a repair target.
        _row("NULL1", today - timedelta(days=10), None),
        # 6-K — OTHER_FORM, NOT a repair target.
        _row("SIXK1", today - timedelta(days=10), "6-K"),
        # Dummy routed-PASS so coverage ratio is fine.
        *(_row(f"R{i}", today - timedelta(days=60), "10-Q") for i in range(5)),
    ]
    pool = _mock_pool(rows)
    targets, lookback = await compute_fundamentals_repair_targets(pool)
    assert targets == ["FAIL10Q"]
    assert "NULL1" not in targets
    assert "SIXK1" not in targets
    assert "<metadata_coverage>" not in targets
    assert lookback > 0


# ─────────────────────────────────────────────────────────────────────
# Excluded-dark coverage — pre-P1 LIVE_WITHIN_DAYS=120 silently
# darkened annual filers; P1 widens per cadence. These tests confirm
# the per-cadence darkening is correct (NOT routed-FAIL).
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_p1_dark_20f_filer_600_days_silent_is_excluded_not_failed() -> None:
    """A 20-F filer 600 days past their last filing exceeds
    LIVE_WITHIN_DAYS_ANNUAL=540 → excluded_dark, NOT routed-FAIL.
    Adding routed-eligible filler so coverage sentinel stays silent."""
    today = _today()
    rows = [
        _row("DARK20F", today - timedelta(days=900), "20-F"),
        _row("DARK20F", today - timedelta(days=600), "20-F"),
        # Fillers so the routed denominator > 0.
        *(_row(f"R{i}", today - timedelta(days=60), "10-Q") for i in range(5)),
    ]
    pool = _mock_pool(rows)
    result = await check_fundamentals_quarterly_completeness(pool)
    assert result.passed is True
    assert not any(f.ticker == "DARK20F" for f in result.failures)


@pytest.mark.asyncio
async def test_p1_dark_10q_filer_200_days_silent_is_excluded_not_failed() -> None:
    """A 10-Q filer 200 days silent exceeds LIVE_WITHIN_DAYS_QUARTERLY=120
    → excluded_dark, NOT routed-FAIL."""
    today = _today()
    rows = [
        _row("DARK10Q", today - timedelta(days=400), "10-Q"),
        _row("DARK10Q", today - timedelta(days=200), "10-Q"),
        # Fillers.
        *(_row(f"R{i}", today - timedelta(days=60), "10-Q") for i in range(5)),
    ]
    pool = _mock_pool(rows)
    result = await check_fundamentals_quarterly_completeness(pool)
    assert result.passed is True
    assert not any(f.ticker == "DARK10Q" for f in result.failures)


# ─────────────────────────────────────────────────────────────────────
# Universe-empty sentinel still works
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_p1_13_empty_universe_sentinel_unchanged() -> None:
    pool = _mock_pool([])
    result = await check_fundamentals_quarterly_completeness(pool)
    assert result.passed is False
    assert result.failed == 1
    assert result.failures[0].ticker == "<universe>"
    assert result.failures[0].reason == "empty_liquid_universe"


# ─────────────────────────────────────────────────────────────────────
# CheckResult.name preserved (consumer contract)
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_p1_14_check_name_consumer_contract_preserved() -> None:
    """Suite + selfheal/per_feed.py + selfheal/registry.py all key
    on the literal check name. P1 MUST NOT rename it."""
    pool = _mock_pool([])
    result = await check_fundamentals_quarterly_completeness(pool)
    assert result.name == CHECK_NAME == "fundamentals_quarterly_completeness"


# ─────────────────────────────────────────────────────────────────────
# Constants pinning (cadence math sanity)
# ─────────────────────────────────────────────────────────────────────


def test_p1_15_cadence_constants_sane() -> None:
    """Sanity guard — annual must be > quarterly on every dimension."""
    assert MAX_ANNUAL_GAP_DAYS > MAX_QUARTERLY_GAP_DAYS
    assert LIVE_WITHIN_DAYS_ANNUAL > LIVE_WITHIN_DAYS_QUARTERLY
    assert 0 < METADATA_COVERAGE_FAIL_THRESHOLD < 1
