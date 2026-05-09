"""Tests for ``tpcore.fundamentals.earnings_quality.check_earnings_quality``."""
from __future__ import annotations

from decimal import Decimal

from tpcore.fundamentals.earnings_quality import (
    EarningsQualityGrade,
    check_earnings_quality,
)


def _base(**overrides) -> dict:
    base = {
        "net_income": Decimal("100"),
        "fcf": Decimal("95"),
        "total_assets": Decimal("1000"),
        "revenue": Decimal("500"),
        "receivables": Decimal("50"),
        "capex": Decimal("-20"),
        "history": [
            {"revenue": Decimal("490"), "receivables": Decimal("48"),
             "fcf": Decimal("90"), "capex": Decimal("-19")},
            {"revenue": Decimal("480"), "receivables": Decimal("47"),
             "fcf": Decimal("88"), "capex": Decimal("-18")},
        ],
    }
    base.update(overrides)
    return base


def test_high_grade_when_clean_fcf_and_low_accruals() -> None:
    """fcf/ni = 0.95 ≥ 0.90 AND accruals = 0.005 < 0.05 → HIGH."""
    result = check_earnings_quality(_base())
    assert result.grade is EarningsQualityGrade.HIGH
    assert result.fcf_to_ni_ratio == Decimal("100") and False or result.fcf_to_ni_ratio is not None
    assert abs(result.fcf_to_ni_ratio - Decimal("0.95")) < Decimal("0.001")


def test_low_grade_when_fcf_to_ni_below_0_6() -> None:
    """fcf/ni = 0.40 < 0.60 → LOW."""
    result = check_earnings_quality(_base(fcf=Decimal("40")))
    assert result.grade is EarningsQualityGrade.LOW


def test_low_grade_when_accruals_above_0_10() -> None:
    """accruals = (NI − FCF)/TA = (100 − (−5))/1000 = 0.105 > 0.10 → LOW."""
    result = check_earnings_quality(_base(fcf=Decimal("-5")))
    assert result.grade is EarningsQualityGrade.LOW


def test_low_grade_when_revenue_recognition_risk_fires() -> None:
    """Receivables grow >> revenue grows YoY → earnings-management red flag.

    The check uses history[3] (same quarter prior year) to absorb seasonality;
    we provide a 4-entry history with the YoY comparison at the back.
    """
    yoy_q = {"revenue": Decimal("400"), "receivables": Decimal("40"),
             "fcf": Decimal("75"), "capex": Decimal("-15")}
    intermediate = {"revenue": Decimal("420"), "receivables": Decimal("42"),
                    "fcf": Decimal("80"), "capex": Decimal("-16")}
    result = check_earnings_quality(
        _base(
            revenue=Decimal("500"),  # YoY growth = (500-400)/400 = 25%
            receivables=Decimal("100"),  # YoY growth = (100-40)/40 = 150%
            history=[intermediate, intermediate, intermediate, yoy_q],
        )
    )
    assert result.grade is EarningsQualityGrade.LOW


def test_medium_grade_when_components_partially_present() -> None:
    """fcf/ni between 0.60 and 0.90 with no LOW triggers → MEDIUM."""
    result = check_earnings_quality(_base(fcf=Decimal("75")))
    assert result.grade is EarningsQualityGrade.MEDIUM


def test_handles_missing_history_gracefully() -> None:
    """No history → trend / rev-rec checks skipped, but core checks still grade."""
    result = check_earnings_quality(_base(history=[]))
    assert result.grade is EarningsQualityGrade.HIGH
    assert any("rev_rec_risk" in n for n in result.notes)


def test_low_grade_when_fcf_trend_collapses() -> None:
    """Latest FCF ~30% of historical median → fcf_3y_trend < −0.30 → LOW."""
    result = check_earnings_quality(
        _base(
            fcf=Decimal("25"),  # latest very low
            net_income=Decimal("30"),  # keep fcf/ni > 0.6 so we hit the trend gate, not the ratio gate
            total_assets=Decimal("10000"),  # accruals tiny
            history=[
                {"revenue": Decimal("490"), "receivables": Decimal("48"),
                 "fcf": Decimal("100"), "capex": Decimal("-19")},
                {"revenue": Decimal("480"), "receivables": Decimal("47"),
                 "fcf": Decimal("110"), "capex": Decimal("-18")},
            ],
        )
    )
    assert result.grade is EarningsQualityGrade.LOW
