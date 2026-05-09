"""Moat scorecard — Buffett-style 5-dimension qualitative score."""
from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

DIMENSIONS = {
    "switching_costs": "Cost — financial, time, or operational — for a customer to leave.",
    "network_effects": "Per-user value scales with total user base.",
    "intangible_assets": "Brands, patents, regulatory licenses, proprietary data.",
    "cost_advantages": "Structural cost lead (scale, geography, process IP).",
    "efficient_scale": "Limited demand profitably served only by a few incumbents.",
}


class MoatScore(BaseModel):
    """0-10 score per dimension. ``total`` is the sum (max 50)."""

    model_config = ConfigDict(extra="forbid")

    switching_costs: int = Field(ge=0, le=10)
    network_effects: int = Field(ge=0, le=10)
    intangible_assets: int = Field(ge=0, le=10)
    cost_advantages: int = Field(ge=0, le=10)
    efficient_scale: int = Field(ge=0, le=10)

    @property
    def total(self) -> int:
        return (
            self.switching_costs
            + self.network_effects
            + self.intangible_assets
            + self.cost_advantages
            + self.efficient_scale
        )


class MoatScorecardTemplate(BaseModel):
    """Static descriptions of the 5 moat dimensions, for UI / rubric prompts."""

    model_config = ConfigDict(extra="forbid")

    dimensions: dict[str, str] = Field(default_factory=lambda: dict(DIMENSIONS))


def get_moat_discount(score: MoatScore) -> Decimal:
    """Required margin-of-safety as a function of moat strength.

    Wider moat → smaller discount required. Narrower moat → larger discount.

    TODO: implement piecewise mapping. Indicative bands::

        total >= 40  → 0.10  (10% discount to fair value)
        total >= 30  → 0.20
        total >= 20  → 0.30
        total <  20  → 0.40
    """
    _ = score
    raise NotImplementedError
