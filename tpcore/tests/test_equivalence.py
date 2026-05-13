"""Tests for :mod:`tpcore.backtest.equivalence`."""
from __future__ import annotations

from datetime import date

import pytest

from tpcore.backtest import (
    EquivalenceReport,
    assert_trade_lists_equal,
    compare_trade_lists,
)
from tpcore.backtest.search import SearchTrade


def _trade(
    ticker: str = "AAPL",
    entry_date: date = date(2024, 1, 2),
    exit_date: date = date(2024, 1, 30),
    entry_price: float = 100.0,
    exit_price: float = 105.0,
    pnl_pct: float = 0.05,
    direction: str = "LONG",
    exit_reason: str = "scheduled_rebalance",
) -> SearchTrade:
    return SearchTrade(
        ticker=ticker, entry_date=entry_date, exit_date=exit_date,
        entry_price=entry_price, exit_price=exit_price,
        pnl_pct=pnl_pct, direction=direction, exit_reason=exit_reason,
    )


def test_identical_lists_equivalent():
    a = [_trade("AAPL"), _trade(ticker="MSFT")]
    b = [_trade("AAPL"), _trade(ticker="MSFT")]
    report = compare_trade_lists(a, b)
    assert report.equivalent
    assert report.n_baseline == 2
    assert report.n_candidate == 2
    assert report.missing_in_candidate == []
    assert report.extra_in_candidate == []
    assert report.mismatches == []


def test_pnl_pct_within_tolerance_equivalent():
    a = [_trade("AAPL", pnl_pct=0.05)]
    # Candidate jitters by 1e-9 — below default 1e-6 tolerance.
    b = [_trade("AAPL", pnl_pct=0.05 + 1e-9)]
    assert compare_trade_lists(a, b).equivalent


def test_pnl_pct_outside_tolerance_mismatches():
    a = [_trade("AAPL", pnl_pct=0.05)]
    b = [_trade("AAPL", pnl_pct=0.06)]  # 1pp diff — well over tolerance
    report = compare_trade_lists(a, b)
    assert not report.equivalent
    assert len(report.mismatches) == 1
    m = report.mismatches[0]
    assert m.ticker == "AAPL"
    assert m.field == "pnl_pct"
    assert m.baseline_value == 0.05
    assert m.candidate_value == 0.06
    assert m.delta == pytest.approx(0.01)


def test_missing_trade_in_candidate_detected():
    a = [_trade("AAPL"), _trade(ticker="MSFT")]
    b = [_trade("AAPL")]  # missing MSFT
    report = compare_trade_lists(a, b)
    assert not report.equivalent
    assert report.n_baseline == 2
    assert report.n_candidate == 1
    assert len(report.missing_in_candidate) == 1
    assert report.missing_in_candidate[0][0] == "MSFT"


def test_extra_trade_in_candidate_detected():
    a = [_trade("AAPL")]
    b = [_trade("AAPL"), _trade(ticker="GOOGL")]  # extra GOOGL
    report = compare_trade_lists(a, b)
    assert not report.equivalent
    assert len(report.extra_in_candidate) == 1
    assert report.extra_in_candidate[0][0] == "GOOGL"


def test_compare_returns_structured_report():
    a = [_trade("AAPL")]
    b = [_trade(ticker="MSFT")]  # totally different
    report = compare_trade_lists(a, b)
    assert isinstance(report, EquivalenceReport)
    assert not report.equivalent
    # AAPL missing from candidate, MSFT extra
    assert any(k[0] == "AAPL" for k in report.missing_in_candidate)
    assert any(k[0] == "MSFT" for k in report.extra_in_candidate)
    # Summary is a non-trivial string
    s = report.summary()
    assert "DIFFER" in s


def test_assert_raises_on_mismatch_with_diff():
    a = [_trade("AAPL", pnl_pct=0.05)]
    b = [_trade("AAPL", pnl_pct=0.10)]
    with pytest.raises(AssertionError) as exc:
        assert_trade_lists_equal(a, b)
    msg = str(exc.value)
    # AssertionError carries the report.summary() — operator sees the
    # exact mismatch (field, baseline, candidate, delta).
    assert "DIFFER" in msg
    assert "pnl_pct" in msg


def test_assert_passes_on_equivalent_lists():
    a = [_trade("AAPL"), _trade(ticker="MSFT")]
    b = [_trade("AAPL"), _trade(ticker="MSFT")]
    # Should not raise.
    assert_trade_lists_equal(a, b)


def test_custom_tolerances_respected():
    a = [_trade("AAPL", pnl_pct=0.05, entry_price=100.0)]
    b = [_trade("AAPL", pnl_pct=0.05 + 0.001, entry_price=100.0 + 0.5)]
    # Default tolerances → mismatch on both fields.
    assert not compare_trade_lists(a, b).equivalent
    # Loosened tolerances → equivalent.
    assert compare_trade_lists(a, b, tol_pnl_pct=0.01, tol_price=1.0).equivalent
