"""Sigma engine — shared Pydantic models and enums.

These models flow between the five plugs:
``SetupDetection -> LifecycleAnalysis -> ExecutionRisk -> CapitalGate -> AARLogging``.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

# Universe used by the Phase 1 scan. Per the Phase 1 spec: hardcoded 10 names.
SIGMA_TEST_UNIVERSE: tuple[str, ...] = (
    "AAPL", "MSFT", "AMZN", "GOOGL", "META",
    "TSLA", "NVDA", "JPM", "V", "WMT",
)

# Plan §4.1 thresholds.
SCORE_STRONG = 70
SCORE_WEAK = 50
HARD_STOP_PCT = Decimal("0.03")  # −3% stop
PRE_GRAD_POSITION_CAP_USD = Decimal("1500")
MAX_CONCURRENT_POSITIONS = 4


class Phase(str, Enum):
    """Lifecycle phases per plan §4.1."""

    SETUP = "setup"
    APPROACHING = "approaching"
    ACTIVE = "active"
    EXHAUSTION = "exhaustion"


class SetupCandidate(BaseModel):
    """Output of SetupDetection — one row per qualifying ticker."""

    model_config = ConfigDict(extra="forbid")

    ticker: str
    as_of: date
    sigma_score: float = Field(
        ge=0, le=100,
        description="Composite of Channel Quality (0-40), Entry Precision (0-35), Market Context (0-25).",
    )
    channel_quality: float = Field(ge=0, le=40)
    entry_precision: float = Field(ge=0, le=35)
    market_context: float = Field(ge=0, le=25)
    band_proximity: float = Field(
        ge=-0.5, le=1.5,
        description="Where the close sits in the BB channel: 0.0 = at lower band, 1.0 = at upper band.",
    )
    bb_width_percentile: float = Field(ge=0, le=1)
    adx: float
    suggested_entry_price: Decimal
    bb_upper: Decimal
    bb_lower: Decimal
    bb_mid: Decimal


class PhaseAssessment(BaseModel):
    """Output of LifecycleAnalysis — phase + concrete entry/stop/TP levels.

    The two tier-tracking fields default to the pre-fill state. After Tier 1
    (mid-band) fills, callers should re-run the lifecycle plug's
    ``handle_tier1_fill`` to mark ``tier1_filled=True`` and record the
    remaining share count to be exited at ``take_profit_far``.
    """

    model_config = ConfigDict(extra="forbid")

    ticker: str
    as_of: date
    phase: Phase
    entry_price: Decimal
    stop_price: Decimal
    take_profit_mid: Decimal = Field(description="Mid-band — 50% scale-out level.")
    take_profit_far: Decimal = Field(description="Opposite (upper) band — final exit.")
    tier1_filled: bool = False
    remaining_shares: int = Field(
        default=0,
        ge=0,
        description="Shares still open after Tier 1 fills (Tier 2 leg). 0 before fill.",
    )
    notes: str | None = None


class ExecutionDecision(BaseModel):
    """Output of ExecutionRisk — Alpaca paper order payloads (two orders for the
    50/50 scale-out per plan §4.1) + sizing facts.

    ``order_payloads`` always contains exactly two entries, in tier order:
        index 0 — Tier 1 bracket (50% qty, TP=mid-band, SL=hard stop)
        index 1 — Tier 2 limit   (remaining qty, limit=upper band, GTC)

    Quantities sum to ``qty`` (odd shares go to Tier 1).
    """

    model_config = ConfigDict(extra="forbid")

    ticker: str
    qty: int = Field(description="Total shares across both tiers.")
    tier1_qty: int = Field(description="Shares on the bracket (mid-band TP).")
    tier2_qty: int = Field(description="Shares on the GTC limit (upper-band TP).")
    notional_usd: Decimal
    risk_amount_usd: Decimal = Field(description="Notional * stop_pct — capital at risk if stop fills.")
    order_payloads: list[dict] = Field(description="Two-element list: [tier1 bracket, tier2 limit].")
    constructed_at: datetime
