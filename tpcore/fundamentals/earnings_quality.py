"""Earnings-quality screen.

Combines FCF/Net-Income ratio, accruals, receivables-vs-revenue trend,
and capex trend into a single HIGH / MEDIUM / LOW grade.
"""
from __future__ import annotations

from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class EarningsQualityGrade(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class EarningsQualityResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fcf_to_ni_ratio: Decimal | None = None
    accruals_ratio: Decimal | None = None
    revenue_recognition_risk: Decimal | None = None
    capex_trend: Decimal | None = None
    fcf_3y_trend: Decimal | None = None
    grade: EarningsQualityGrade
    notes: list[str] = Field(default_factory=list)


def check_earnings_quality(
    net_income: Decimal,
    fcf: Decimal,
    total_assets: Decimal,
    revenue: Decimal,
    receivables: Decimal,
    capex: Decimal,
    fcf_history: list[Decimal],
) -> EarningsQualityResult:
    """Compute an EarningsQualityResult.

    TODO: implement full rubric. Sketch::

        fcf_to_ni  = fcf / net_income
        accruals   = (net_income - fcf) / total_assets
        rev_risk   = (Δreceivables / Δrevenue) over trailing periods
        capex_tr   = slope(capex history)
        fcf_3y_tr  = slope(fcf_history[-3:])

    Grade HIGH when fcf_to_ni >= 0.9 and accruals < 0.05; LOW when fcf_to_ni
    < 0.6 or accruals > 0.10; otherwise MEDIUM.
    """
    _ = (net_income, fcf, total_assets, revenue, receivables, capex, fcf_history)
    raise NotImplementedError
