"""Reversion engine — shared Pydantic models, enums, and constants.

These models flow between the five plugs:
``SetupDetection -> LifecycleAnalysis -> ExecutionRisk -> CapitalGate -> AARLogging``.

Reversion fades both directions — oversold names get bought (LONG),
overbought names get shorted (SHORT). The shipped scheduler currently
defaults to LONG-only for paper-trading reasons (Alpaca paper short-borrow
availability is per-symbol and unstable); the engine is symmetric so flipping
that flag is a one-liner.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

# Plan §4.2 thresholds.
SCORE_STRONG = 70
SCORE_WEAK = 50
HARD_STOP_PCT = Decimal("0.08")  # −8% hard stop on the entry price.
PRE_GRAD_POSITION_CAP_USD = Decimal("2000")
MAX_CONCURRENT_POSITIONS = 5
TIME_STOP_DAYS = 5
MAX_ADX_FOR_REVERSION = 25.0  # ADX(14) > 25 → engine disabled.

# Score thresholds drawn from the master plan rationale.
# Z=3.0 chosen after the 8-year backtest revealed |Z| 2.0–3.0 was the worst-
# performing entry bucket; |Z|≥3.0 is profitable on its own (PF 1.37). See
# `backtests/reversion_diagnosis.txt` and master plan §4.2.
Z_SCORE_THRESHOLD = 3.0
RSI_OVERSOLD = 25.0
RSI_OVERBOUGHT = 75.0

# Default 50-name universe — same shape as Sigma's. Kept as a plain tuple so
# callers can override via constructor without monkey-patching.
REVERSION_TEST_UNIVERSE: tuple[str, ...] = (
    "AAPL", "MSFT", "AMZN", "GOOGL", "META",
    "TSLA", "NVDA", "JPM", "V", "WMT",
)


class Direction(str, Enum):
    """Trade direction. LONG fades oversold, SHORT fades overbought."""

    LONG = "long"
    SHORT = "short"


class Phase(str, Enum):
    """Lifecycle phases per plan §4.2."""

    SETUP = "setup"
    ACTIVE = "active"
    REVERTING = "reverting"
    EXHAUSTED = "exhausted"


class SetupCandidate(BaseModel):
    """Output of SetupDetection — one row per qualifying ticker."""

    model_config = ConfigDict(extra="forbid")

    ticker: str
    as_of: date
    direction: Direction
    reversion_score: float = Field(
        ge=0, le=100,
        description="Composite of Statistical Extremity (0-45), Exhaustion Confirmation (0-30), Market Context (0-25).",
    )
    statistical_extremity: float = Field(ge=0, le=45)
    exhaustion_confirmation: float = Field(ge=0, le=30)
    market_context: float = Field(ge=0, le=25)
    z_score: float = Field(description="Close vs 20-day MA in stddev units. Negative = oversold.")
    rsi_14: float = Field(ge=0, le=100)
    bb_breach_consecutive_days: int = Field(ge=0)
    volume_ratio: float = Field(description="Today's volume ÷ 20-day average.")
    adx_14: float
    has_reversal_candle: bool
    has_rsi_divergence: bool
    suggested_entry_price: Decimal
    target_20ma: Decimal
    target_50ma: Decimal
    notes: str | None = None


class PhaseAssessment(BaseModel):
    """Output of LifecycleAnalysis — phase + concrete entry/stop/TP levels."""

    model_config = ConfigDict(extra="forbid")

    ticker: str
    as_of: date
    direction: Direction
    phase: Phase
    entry_price: Decimal
    stop_price: Decimal
    target_20ma: Decimal = Field(description="Tier 1 — 75% scale-out at the 20-day MA.")
    target_50ma: Decimal = Field(description="Tier 2 — 25% remainder at the 50-day MA.")
    tier1_filled: bool = False
    remaining_shares: int = Field(default=0, ge=0)
    bars_held: int = Field(default=0, ge=0, description="Trading days since entry; informs the 5-day time stop.")
    earnings_quality_blocked: bool = Field(
        default=False,
        description="True iff the earnings-quality gate fired LOW and the trade was suppressed.",
    )
    notes: str | None = None


class ExecutionDecision(BaseModel):
    """Output of ExecutionRisk — Alpaca paper order payloads + sizing facts.

    ``order_payloads`` always contains exactly two entries, in tier order:
        index 0 — Tier 1 bracket (75% qty, TP=20-day MA, SL=hard stop)
        index 1 — Tier 2 limit   (25% qty, limit=50-day MA, GTC)
    """

    model_config = ConfigDict(extra="forbid")

    ticker: str
    direction: Direction
    qty: int = Field(description="Total shares across both tiers.")
    tier1_qty: int = Field(description="Shares on the bracket (20-day MA TP).")
    tier2_qty: int = Field(description="Shares on the GTC limit (50-day MA TP).")
    notional_usd: Decimal
    risk_amount_usd: Decimal
    order_payloads: list[dict]
    constructed_at: datetime
