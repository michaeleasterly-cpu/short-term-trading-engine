"""Buffett-style owner earnings."""
from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class OwnerEarningsResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner_earnings: Decimal
    owner_earnings_yield: Decimal | None = None  # vs. market cap, if provided
    fair_value_capitalized: Decimal


def compute_owner_earnings(
    net_income: Decimal,
    da: Decimal,
    maintenance_capex: Decimal,
    discount_rate: Decimal = Decimal("0.10"),
) -> OwnerEarningsResult:
    """``owner_earnings = net_income + D&A − maintenance_capex``.

    Capitalize at ``discount_rate`` for a quick fair-value proxy:
    ``fair_value = owner_earnings / discount_rate``.
    TODO: tighten typing and edge-case handling (e.g. maintenance_capex > NI+DA).
    """
    _ = (net_income, da, maintenance_capex, discount_rate)
    raise NotImplementedError
