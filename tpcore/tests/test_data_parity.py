"""Unit tests for the data-provider parity gate (Phase 2).

Pure verdict primitive — synthetic incumbent/candidate sample sets,
no DB/network. Pins the contract that a quietly-worse candidate is
BLOCKED and a non-evaluable comparison is reported honestly (never a
silent pass).
"""
from __future__ import annotations

from datetime import date

from tpcore.parity import (
    FeedClass,
    ParitySample,
    ParityVerdict,
    compare_provider_parity,
)

_D = date(2026, 5, 15)


def _bars(keys: list[str], asof: date, value: float = 100.0) -> list[ParitySample]:
    return [ParitySample(key=k, asof=asof, value=value) for k in keys]


def test_candidate_equal_or_better_passes() -> None:
    inc = _bars(["AAPL|1", "MSFT|1"], _D, 100.0)
    cand = _bars(["AAPL|1", "MSFT|1", "NVDA|1"], _D, 100.0)  # superset, same date/val
    r = compare_provider_parity(
        feed_class=FeedClass.PRICE, incumbent=inc, candidate=cand
    )
    assert r.verdict is ParityVerdict.PASS and r.passed
    assert r.coverage_ratio == 1.0 and r.freshness_lag_days == 0


def test_coverage_shortfall_fails() -> None:
    inc = _bars([f"T{i}|1" for i in range(10)], _D)
    cand = _bars([f"T{i}|1" for i in range(7)], _D)  # dropped 3/10 keys
    r = compare_provider_parity(
        feed_class=FeedClass.PRICE, incumbent=inc, candidate=cand
    )
    assert r.verdict is ParityVerdict.FAIL
    assert r.coverage_ratio == 0.7 and "coverage" in r.evidence


def test_stale_candidate_fails() -> None:
    inc = _bars(["A|2"], date(2026, 5, 15))
    cand = _bars(["A|1"], date(2026, 5, 10))  # 5 trading-ish days behind
    r = compare_provider_parity(
        feed_class=FeedClass.PRICE, incumbent=inc, candidate=cand
    )
    assert r.verdict is ParityVerdict.FAIL
    assert r.freshness_lag_days == 5 and "freshness" in r.evidence


def test_value_divergence_fails_for_value_feed() -> None:
    inc = [ParitySample(key="vix|1", asof=_D, value=20.0)]
    cand = [ParitySample(key="vix|1", asof=_D, value=28.0)]  # 40% off
    r = compare_provider_parity(
        feed_class=FeedClass.MACRO, incumbent=inc, candidate=cand
    )
    assert r.verdict is ParityVerdict.FAIL
    assert r.accuracy_ratio == 0.0 and "accuracy" in r.evidence


def test_filing_class_is_presence_only_value_ignored() -> None:
    # Same keys/freshness but wildly different "values" — FILING skips
    # value comparison (presence is the signal).
    inc = [ParitySample(key="AAPL|8-K|1", asof=_D, value=1.0)]
    cand = [ParitySample(key="AAPL|8-K|1", asof=_D, value=999.0)]
    r = compare_provider_parity(
        feed_class=FeedClass.FILING, incumbent=inc, candidate=cand
    )
    assert r.verdict is ParityVerdict.PASS
    assert r.accuracy_ratio is None  # value comparison skipped


def test_empty_incumbent_is_not_evaluable_not_pass() -> None:
    r = compare_provider_parity(
        feed_class=FeedClass.PRICE, incumbent=[], candidate=_bars(["X|1"], _D)
    )
    assert r.verdict is ParityVerdict.NOT_EVALUABLE and not r.passed


def test_derived_feed_is_not_evaluable() -> None:
    r = compare_provider_parity(
        feed_class=FeedClass.DERIVED,
        incumbent=_bars(["a|1"], _D), candidate=_bars(["a|1"], _D),
    )
    assert r.verdict is ParityVerdict.NOT_EVALUABLE and not r.passed
    assert "derived" in r.evidence
