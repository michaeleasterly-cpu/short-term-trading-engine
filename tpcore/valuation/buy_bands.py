"""Tiered buy-band generator.

Given a fair-value range and the required moat-based discount, generate a
series of progressively cheaper buy zones (e.g. opportunistic, attractive,
back-up-the-truck).
"""
from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class BuyTier(StrEnum):
    OPPORTUNISTIC = "opportunistic"  # ~ moat discount applied
    ATTRACTIVE = "attractive"  # 1.5x discount
    BACK_UP_TRUCK = "back_up_truck"  # 2x discount


class BuyBand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tier: BuyTier
    upper_price: Decimal
    lower_price: Decimal
    target_position_pct: Decimal


def generate_buy_bands(
    fair_value_range: tuple[Decimal, Decimal],
    moat_discount: Decimal,
    current_spx: Decimal | None = None,
) -> list[BuyBand]:
    """Produce three buy bands from a fair-value range and moat discount.

    ``current_spx`` is optional context for regime adjustment (e.g. tighten
    bands in extreme overbought regimes). TODO: implement tiering and the
    optional regime adjustment.
    """
    _ = (fair_value_range, moat_discount, current_spx)
    raise NotImplementedError
