"""Two-stage discounted-cash-flow model with sensitivity table."""
from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class DCFAssumptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    growth_high: Decimal = Decimal("0.10")  # stage 1 growth
    growth_terminal: Decimal = Decimal("0.025")  # stage 2 / perpetuity growth
    discount_rate: Decimal = Decimal("0.10")
    high_growth_years: int = 5


class DCFResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fair_value_per_share: Decimal
    fair_value_low: Decimal
    fair_value_high: Decimal
    sensitivity: dict[str, dict[str, Decimal]] = Field(
        default_factory=dict,
        description="Nested map: discount_rate -> terminal_growth -> per-share fair value.",
    )


def compute_dcf(
    fcf_ttm: Decimal,
    shares_outstanding: Decimal,
    net_cash: Decimal,
    assumptions: DCFAssumptions,
) -> DCFResult:
    """Two-stage DCF + ±2% sensitivity grid on discount rate and terminal growth.

    TODO: implement standard DCF::

        PV of stage-1 FCFs (years 1..N) + terminal value / (1 + r)^N
        + net cash → equity value → /shares = per-share fair value.

    Build a sensitivity grid by varying discount_rate ∈ {-2%, -1%, base, +1%, +2%}
    and terminal_growth ∈ {-1%, base, +1%}.
    """
    _ = (fcf_ttm, shares_outstanding, net_cash, assumptions)
    raise NotImplementedError
