"""P1 → P3 — fundamentals_quarterly_completeness routing tests.

**P3 rewrite (2026-06-07)** — the gap is now an AUTHORITATIVE
set-difference against ``platform.sec_periodic_filings`` (shared store
``compute_filing_gaps``), not the P1 even-spacing interpolation. The
CADENCE-ROUTING behavior these tests cover (10-Q ⇒ quarterly; 10-K /
20-F / 40-F ⇒ annual; NULL ⇒ METADATA_REQUIRED; non-routed ⇒ OTHER_FORM;
per-cadence liveness; metadata-coverage sentinel; repair-target scoping)
is preserved; the gap tests are re-expressed in the new substrate model
(SEC-filed reportDates vs fundamentals period_end_dates) so a gap is a
real set-difference, not an interpolated guess.

Hermetic — no DB access; a substrate-aware fake pool dispatches each SQL
by substring to the configured (universe / SEC reportDates / fundamentals
have) row sets.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any
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


class _Spec:
    """One issuer's substrate footprint.

    ``primary`` routes cadence; ``sec`` is what SEC FILED (``expected``);
    ``have`` is the present fundamentals period_end_dates. ``cik`` defaults
    present (a CIK-backed issuer); pass ``cik=None`` for a CIK-less name.
    Anchored iff ``sec`` is non-empty.
    """

    def __init__(
        self,
        ticker: str,
        primary: str | None,
        *,
        sec: list[date] | None = None,
        have: list[date] | None = None,
        cik: str | None = "0001",
    ) -> None:
        self.ticker = ticker
        self.primary = primary
        self.cik = cik
        self.sec = sec or []
        self.have = have if have is not None else list(self.sec)
        self.cid = f"c-{ticker}"


def _mock_pool(specs: list[_Spec]) -> MagicMock:
    """Substrate-aware asyncpg.Pool stub dispatching by SQL substring."""
    by_cid = {s.cid: s for s in specs}

    async def _fetch(sql: str, *args: Any) -> list[dict[str, Any]]:
        # Universe SQL (the check's _FILING_DATES_SQL).
        if "WITH liquid AS" in sql:
            out: list[dict[str, Any]] = []
            for s in specs:
                base = {
                    "ticker": s.ticker,
                    "classification_id": s.cid,
                    "cik": s.cik,
                    "sec_document_type_primary": s.primary,
                    "issuer_lifecycle_state": None,
                    "issuer_lifecycle_event_date": None,
                }
                if s.have:
                    for pe in sorted(s.have):
                        out.append({**base, "period_end_date": pe})
                else:
                    out.append({**base, "period_end_date": None})
            return out
        # Store _ANCHORED_SQL.
        if "SELECT DISTINCT classification_id" in sql:
            wanted = set(args[0])
            return [
                {"classification_id": cid}
                for cid in wanted
                if by_cid[cid].sec
            ]
        # Store _EXPECTED_SQL (SEC reportDates).
        if "FROM platform.sec_periodic_filings" in sql:
            wanted = set(args[0])
            out = []
            for cid in wanted:
                for rd in by_cid[cid].sec:
                    out.append({"classification_id": cid, "report_date": rd})
            return out
        # Store _HAVE_SQL (fundamentals period_end_dates).
        if ("FROM platform.fundamentals_quarterly" in sql
                and "classification_id = ANY" in sql):
            wanted = set(args[0])
            out = []
            for cid in wanted:
                for pe in by_cid[cid].have:
                    out.append(
                        {"classification_id": cid, "period_end_date": pe}
                    )
            return out
        # Evidence-join SQL — no dual-source evidence in these tests.
        if "confirmed_data_gap_evidence" in sql:
            return []
        raise AssertionError(f"unexpected SQL: {sql[:80]}")

    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=_fetch)
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire)
    return pool


def _qends(today: date, n: int) -> list[date]:
    """n recent quarter-ends (most recent ~30d ago), oldest first."""
    return [today - timedelta(days=30 + 91 * i) for i in range(n)][::-1]


def _annual(today: date, n: int) -> list[date]:
    """n recent annual reportDates (most recent ~30d ago), oldest first."""
    return [today - timedelta(days=30 + 365 * i) for i in range(n)][::-1]


# ─────────────────────────────────────────────────────────────────────
# TEST-P1-01..05 — 10-Q / 20-F / 40-F cadence routing
# ─────────────────────────────────────────────────────────────────────


# Padding: clean 10-Q filers (full data) to keep the coverage sentinel
# silent in routing/dark tests that aren't about coverage.
def _clean(today: date, n: int) -> list[_Spec]:
    rd = _qends(today, 4)
    return [
        _Spec(f"R{i}", "10-Q", sec=rd, have=rd, cik=f"00{i:04d}")
        for i in range(n)
    ]


@pytest.mark.asyncio
async def test_p1_01_10q_no_gaps_passes() -> None:
    """A 10-Q filer whose fundamentals has every SEC-filed reportDate
    PASSES (anchored=True, missing_periods=())."""
    today = _today()
    rd = _qends(today, 4)
    pool = _mock_pool([_Spec("AAPL", "10-Q", sec=rd, have=rd)])
    result = await check_fundamentals_quarterly_completeness(pool)
    assert result.passed is True
    assert result.failed == 0


@pytest.mark.asyncio
async def test_p1_02_10q_with_gap_fails_missing_period_10q() -> None:
    """A 10-Q filer missing a SEC-filed reportDate FAILS with
    ``missing_period_10-Q`` (the genuine set-difference)."""
    today = _today()
    rd = _qends(today, 4)
    have = [d for d in rd if d != rd[1]]  # missing one filed period
    pool = _mock_pool([_Spec("AAPL", "10-Q", sec=rd, have=have)])
    result = await check_fundamentals_quarterly_completeness(pool)
    assert result.passed is False
    assert any(
        f.reason == "missing_period_10-Q" and f.ticker == "AAPL"
        for f in result.failures
    )


@pytest.mark.asyncio
async def test_p1_03_20f_no_gaps_passes() -> None:
    """A 20-F filer routed annual with all SEC-filed reportDates present
    PASSES — pre-P1 this ticker false-FAILed as missing_quarter."""
    today = _today()
    rd = _annual(today, 3)
    pool = _mock_pool([_Spec("AER", "20-F", sec=rd, have=rd)])
    result = await check_fundamentals_quarterly_completeness(pool)
    assert result.passed is True
    assert result.failed == 0


@pytest.mark.asyncio
async def test_p1_04_20f_with_year_skip_fails_missing_period_20f() -> None:
    """A 20-F filer missing one SEC-filed annual reportDate FAILS with
    ``missing_period_20-F``."""
    today = _today()
    rd = _annual(today, 3)
    have = [d for d in rd if d != rd[0]]  # missing the oldest filed year
    pool = _mock_pool([_Spec("AER", "20-F", sec=rd, have=have)])
    result = await check_fundamentals_quarterly_completeness(pool)
    assert result.passed is False
    assert any(
        f.reason == "missing_period_20-F" and f.ticker == "AER"
        for f in result.failures
    )


@pytest.mark.asyncio
async def test_p1_05_20f_complete_history_passes() -> None:
    """A 20-F filer with two SEC-filed annual reportDates both present
    PASSES (no fabricated gap from a long-but-real annual interval)."""
    today = _today()
    rd = _annual(today, 2)
    pool = _mock_pool([_Spec("AER", "20-F", sec=rd, have=rd)])
    result = await check_fundamentals_quarterly_completeness(pool)
    assert result.passed is True


# ─────────────────────────────────────────────────────────────────────
# TEST-P1-06 — 40-F (Canadian MJDS) routes as annual
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_p1_06_40f_routes_as_annual() -> None:
    today = _today()
    rd = _annual(today, 3)
    pool = _mock_pool([_Spec("ASTL", "40-F", sec=rd, have=rd)])
    result = await check_fundamentals_quarterly_completeness(pool)
    assert result.passed is True


# ─────────────────────────────────────────────────────────────────────
# TEST-P1-07 — NULL doctype → METADATA_REQUIRED (excluded, NOT a FAIL)
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_p1_07_null_doctype_routes_to_metadata_required() -> None:
    """NULL primary form → METADATA_REQUIRED bucket; not a per-ticker
    FAIL. 1 NULL + 4 routed-clean → 1/5 = 20% < 25% → no sentinel."""
    today = _today()
    specs = [
        _Spec("UNKNOWN", None),
        *_clean(today, 4),
    ]
    pool = _mock_pool(specs)
    result = await check_fundamentals_quarterly_completeness(pool)
    assert result.passed is True, [f.observed for f in result.failures]
    assert not any(f.ticker == "UNKNOWN" for f in result.failures)


# ─────────────────────────────────────────────────────────────────────
# TEST-P1-08 — non-routed primary form → excluded_other_form
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_p1_08_six_k_primary_routes_to_other_form() -> None:
    """A '6-K' primary is non-routed → OTHER_FORM bucket; not a
    per-ticker FAIL and not metadata-required."""
    today = _today()
    specs = [_Spec("SIXK", "6-K"), *_clean(today, 5)]
    pool = _mock_pool(specs)
    result = await check_fundamentals_quarterly_completeness(pool)
    assert result.passed is True, [f.observed for f in result.failures]
    assert not any(f.ticker == "SIXK" for f in result.failures)


# ─────────────────────────────────────────────────────────────────────
# TEST-P1-09 — annual liveness window covers a 200-day silent 20-F
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_p1_09_annual_liveness_window_covers_200_day_silent_20f() -> None:
    """A 20-F filer 200 days past their last filing is NOT dark
    (LIVE_WITHIN_DAYS_ANNUAL=540) and PASSES with every SEC reportDate
    present."""
    today = _today()
    rd = [today - timedelta(days=560), today - timedelta(days=200)]
    pool = _mock_pool([_Spec("AER", "20-F", sec=rd, have=rd)])
    result = await check_fundamentals_quarterly_completeness(pool)
    assert result.passed is True


# ─────────────────────────────────────────────────────────────────────
# TEST-P1-10..11 — metadata-coverage structural sentinel
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_p1_10_metadata_sentinel_fires_above_threshold() -> None:
    """30% NULL coverage (> 25% threshold) fires the metadata-coverage
    structural sentinel as a synthetic ``<metadata_coverage>`` failure."""
    today = _today()
    specs = [_Spec(f"NULL{i}", None) for i in range(3)]
    specs += _clean(today, 7)  # 3/10 = 30% NULL → sentinel
    pool = _mock_pool(specs)
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
    """20% NULL coverage (< 25% threshold) does NOT fire the sentinel."""
    today = _today()
    specs = [_Spec("NULL0", None), *_clean(today, 4)]  # 1/5 = 20%
    pool = _mock_pool(specs)
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
    with a genuine set-difference gap; NEVER METADATA_REQUIRED,
    OTHER_FORM, CONFIRMED_DATA_GAP, or sentinel synthetic tickers."""
    today = _today()
    rd = _qends(today, 4)
    specs = [
        # Genuine gap — should be a repair target.
        _Spec("FAIL10Q", "10-Q", sec=rd, have=[d for d in rd if d != rd[1]]),
        _Spec("NULL1", None),       # METADATA_REQUIRED, not a target
        _Spec("SIXK1", "6-K"),      # OTHER_FORM, not a target
        *_clean(today, 5),          # routed-PASS padding
    ]
    pool = _mock_pool(specs)
    targets, lookback = await compute_fundamentals_repair_targets(pool)
    assert targets == ["FAIL10Q"]
    assert "NULL1" not in targets
    assert "SIXK1" not in targets
    assert "<metadata_coverage>" not in targets
    assert lookback > 0


# ─────────────────────────────────────────────────────────────────────
# Excluded-dark coverage — per-cadence darkening (NOT routed-FAIL).
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_p1_dark_20f_filer_600_days_silent_is_excluded_not_failed() -> None:
    """A 20-F filer 600 days past their last filing exceeds
    LIVE_WITHIN_DAYS_ANNUAL=540 → excluded_dark, NOT routed-FAIL — even
    though SEC filed a reportDate fundamentals lacks."""
    today = _today()
    rd = [today - timedelta(days=900), today - timedelta(days=600)]
    specs = [
        _Spec("DARK20F", "20-F", sec=rd, have=[rd[0]]),  # would gap if live
        *_clean(today, 5),
    ]
    pool = _mock_pool(specs)
    result = await check_fundamentals_quarterly_completeness(pool)
    assert result.passed is True
    assert not any(f.ticker == "DARK20F" for f in result.failures)


@pytest.mark.asyncio
async def test_p1_dark_10q_filer_200_days_silent_is_excluded_not_failed() -> None:
    """A 10-Q filer 200 days silent exceeds LIVE_WITHIN_DAYS_QUARTERLY=120
    → excluded_dark, NOT routed-FAIL."""
    today = _today()
    rd = [today - timedelta(days=400), today - timedelta(days=200)]
    specs = [
        _Spec("DARK10Q", "10-Q", sec=rd, have=[rd[0]]),
        *_clean(today, 5),
    ]
    pool = _mock_pool(specs)
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
