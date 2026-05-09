"""Earnings-quality screen.

Combines FCF/Net-Income ratio, accruals, receivables-vs-revenue trend,
capex trend, and FCF trend into a HIGH / MEDIUM / LOW grade. Designed to
take the dict returned by ``tpcore.fmp.FMPFundamentalsAdapter`` —
fields the adapter doesn't provide (because FMP free tier caps history
at 5 quarters or because the field is missing on a given filing) are
skipped and recorded in ``notes``.

Grading rubric (from the original docstring sketch, validated against
how Reversion uses the gate):

    HIGH    when fcf_to_ni ≥ 0.9 and accruals < 0.05
    LOW     when fcf_to_ni < 0.6 OR accruals > 0.10 OR
            revenue-recognition-risk fires (receivables grew far faster
            than revenue) OR fcf 3-quarter trend is materially negative
    MEDIUM  otherwise
"""
from __future__ import annotations

from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

ACCRUALS_HIGH_FLOOR = Decimal("0.05")
ACCRUALS_LOW_CEILING = Decimal("0.10")
FCF_NI_HIGH_FLOOR = Decimal("0.90")
FCF_NI_LOW_CEILING = Decimal("0.60")
# Revenue-recognition risk fires when receivables grow > 1.5× revenue growth
# AND revenue is meaningfully growing (avoid noisy signals on flat quarters).
# We compare *ratios* — the textbook earnings-management signal is "receivables
# accelerating faster than revenue is" — not differences in percentage points.
REV_REC_RECEIVABLES_RATIO = Decimal("1.5")
REV_REC_MIN_REVENUE_GROWTH = Decimal("0.02")  # 2%
# FCF trend: bad if the most recent FCF is < 70% of the median of prior periods.
FCF_TREND_BAD_RATIO = Decimal("0.70")


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


def _safe_div(num: Decimal | None, denom: Decimal | None) -> Decimal | None:
    if num is None or denom is None or denom == 0:
        return None
    return Decimal(num) / Decimal(denom)


def _abs(d: Decimal | None) -> Decimal | None:
    return None if d is None else abs(d)


def _median(values: list[Decimal]) -> Decimal | None:
    """Median of a non-empty list of Decimals, else None."""
    n = len(values)
    if n == 0:
        return None
    s = sorted(values)
    if n % 2 == 1:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) / Decimal(2)


def check_earnings_quality(fundamentals: dict[str, Any]) -> EarningsQualityResult:
    """Grade the candidate's earnings quality from a fundamentals dict.

    The dict shape mirrors ``FMPFundamentalsAdapter.get_quarterly_fundamentals``:
    latest period at the top level, optional ``history`` list of prior
    periods (same per-period schema, sorted most-recent-first).

    Missing components are skipped — the result's ``notes`` field records
    exactly which checks didn't run, so the caller can decide whether the
    grade is reliable enough to gate on.
    """
    notes: list[str] = []

    net_income = fundamentals.get("net_income")
    fcf = fundamentals.get("fcf")
    total_assets = fundamentals.get("total_assets")
    revenue = fundamentals.get("revenue")
    receivables = fundamentals.get("receivables")
    capex = fundamentals.get("capex")
    history = fundamentals.get("history") or []

    fcf_to_ni = _safe_div(fcf, net_income)
    if fcf_to_ni is None:
        notes.append("fcf_to_ni: missing net_income or fcf")

    accruals = None
    if net_income is not None and fcf is not None and total_assets is not None and total_assets != 0:
        accruals = (Decimal(net_income) - Decimal(fcf)) / Decimal(total_assets)
    else:
        notes.append("accruals: missing inputs")

    # rev_rec_risk = ratio of (receivables growth) / (revenue growth).
    # > 1.5 with meaningful revenue growth is the earnings-management red flag.
    # Compare YoY (same fiscal quarter, prior year) when we have ≥ 4 history
    # entries — Q-over-Q comparisons are dominated by seasonality and
    # produce a flood of false positives on retailers, AAPL, etc. With only
    # ≤ 5 quarters from the FMP free tier, that's `history[3]`.
    rev_rec_risk = None
    if revenue is not None and receivables is not None and len(history) >= 4:
        prior = history[3]
        prior_rev = prior.get("revenue")
        prior_recv = prior.get("receivables")
        if prior_rev not in (None, 0) and prior_recv not in (None, 0):
            rev_growth = (Decimal(revenue) - Decimal(prior_rev)) / Decimal(prior_rev)
            recv_growth = (Decimal(receivables) - Decimal(prior_recv)) / Decimal(prior_recv)
            if rev_growth > REV_REC_MIN_REVENUE_GROWTH:
                rev_rec_risk = recv_growth / rev_growth
            else:
                notes.append("rev_rec_risk: revenue not growing YoY meaningfully")
        else:
            notes.append("rev_rec_risk: prior-year zeros")
    else:
        notes.append("rev_rec_risk: insufficient YoY history (need ≥ 4 quarters)")

    capex_trend = None
    capex_history = [
        Decimal(p["capex"]) for p in history if p.get("capex") is not None
    ]
    if capex is not None and capex_history:
        prior_med = _median([_abs(c) for c in capex_history if c is not None])
        if prior_med is not None and prior_med != 0:
            capex_trend = (abs(Decimal(capex)) - prior_med) / prior_med
    else:
        notes.append("capex_trend: insufficient history")

    fcf_trend = None
    fcf_history = [Decimal(p["fcf"]) for p in history if p.get("fcf") is not None]
    if fcf is not None and fcf_history:
        prior_med = _median(fcf_history)
        if prior_med is not None and prior_med != 0:
            fcf_trend = (Decimal(fcf) - prior_med) / abs(prior_med)
    else:
        notes.append("fcf_trend: insufficient history")

    # Apply rubric.
    grade = _grade(
        fcf_to_ni=fcf_to_ni,
        accruals=accruals,
        rev_rec_risk=rev_rec_risk,
        fcf_trend=fcf_trend,
    )

    return EarningsQualityResult(
        fcf_to_ni_ratio=fcf_to_ni,
        accruals_ratio=accruals,
        revenue_recognition_risk=rev_rec_risk,
        capex_trend=capex_trend,
        fcf_3y_trend=fcf_trend,
        grade=grade,
        notes=notes,
    )


def _grade(
    *,
    fcf_to_ni: Decimal | None,
    accruals: Decimal | None,
    rev_rec_risk: Decimal | None,
    fcf_trend: Decimal | None,
) -> EarningsQualityGrade:
    """Apply the published rubric. Missing inputs cannot upgrade or downgrade
    on their own — they're treated as 'no signal' for that component."""
    # LOW conditions take precedence.
    if fcf_to_ni is not None and fcf_to_ni < FCF_NI_LOW_CEILING:
        return EarningsQualityGrade.LOW
    if accruals is not None and accruals > ACCRUALS_LOW_CEILING:
        return EarningsQualityGrade.LOW
    if rev_rec_risk is not None and rev_rec_risk > REV_REC_RECEIVABLES_RATIO:
        # Receivables growing > 1.5× revenue growth — earnings-management red flag.
        return EarningsQualityGrade.LOW
    if fcf_trend is not None and fcf_trend < (FCF_TREND_BAD_RATIO - Decimal(1)):
        # Negative trend below −30%.
        return EarningsQualityGrade.LOW

    # HIGH requires both core checks to be present and pass.
    if (
        fcf_to_ni is not None
        and fcf_to_ni >= FCF_NI_HIGH_FLOOR
        and accruals is not None
        and accruals < ACCRUALS_HIGH_FLOOR
    ):
        return EarningsQualityGrade.HIGH
    return EarningsQualityGrade.MEDIUM


__all__ = [
    "EarningsQualityGrade",
    "EarningsQualityResult",
    "check_earnings_quality",
]
