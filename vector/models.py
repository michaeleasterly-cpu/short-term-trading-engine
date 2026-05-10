"""Vector engine — shared Pydantic models, enums, and constants per plan §4.3.

Models flow through the five plugs:
``SetupDetection -> LifecycleAnalysis -> ExecutionRisk -> CapitalGate -> AARLogging``.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

# Universe used by the Phase 4 scan. Liquid, fundamentally trackable mid/large caps.
VECTOR_TEST_UNIVERSE: tuple[str, ...] = (
    "AAPL", "MSFT", "AMZN", "GOOGL", "META",
    "TSLA", "NVDA", "JPM", "V", "WMT",
    "XOM", "CAT", "BA", "MCD", "DIS",
)

# Plan §4.3 thresholds.
SCORE_STRONG = 65
SCORE_WEAK = 50
HARD_STOP_PCT = Decimal("0.07")  # −7% stop
PROFIT_TARGET_PCT = Decimal("0.15")  # +15% target
TRAILING_STOP_PCT = Decimal("0.05")  # −5% trail from peak
TRAILING_STOP_TRIGGER_PCT = Decimal("0.10")  # arm trail at +10%
PRE_GRAD_POSITION_CAP_USD = Decimal("2000")
MAX_CONCURRENT_POSITIONS = 5
DAILY_LOSS_FREEZE_PCT = Decimal("0.05")

# Crash-guard thresholds (plan §4.3 mandatory).
VIX_SCALE_DOWN_50 = Decimal("25")  # VIX > 25 → 50% size
VIX_SCALE_DOWN_25 = Decimal("30")  # VIX > 30 → 25% size
VIX_BLOCK_NEW = Decimal("28")  # VIX > 28 → no new entries (hard cutoff per spec)
SPY_DRAWDOWN_FREEZE_PCT = Decimal("0.10")  # SPY −10% in 20 days → 10-day cooldown
ENGINE_DRAWDOWN_FREEZE_PCT = Decimal("0.10")  # engine −10% rolling 20-day → 10-day freeze


class Phase(StrEnum):
    """Lifecycle phases per plan §4.3."""

    ENTRY = "entry"  # days 1–3 — validate the breakout / pullback
    HOLDING = "holding"  # trend established, ride
    EARLY_CUT = "early_cut"  # closed below 10-MA in first 3 days → reduce 50%
    EXIT = "exit"  # target hit, trailing stop hit, or stop hit


class SetupCandidate(BaseModel):
    """Output of SetupDetection — one row per qualifying ticker."""

    model_config = ConfigDict(extra="forbid")

    ticker: str
    as_of: date
    swing_score: float = Field(
        ge=0, le=100,
        description="Composite of Technical (0–40) + Catalyst (0–35) + Sentiment (0–25).",
    )
    technical: float = Field(ge=0, le=40)
    catalyst: float = Field(ge=0, le=35)
    sentiment: float = Field(ge=0, le=25)

    last_close: Decimal
    sma_50: Decimal
    sma_200: Decimal
    avg_volume: int
    market_cap_usd: Decimal | None = None
    vix_at_entry: float | None = Field(
        default=None,
        description="Spot VIX on the candidate date — drives size scaling in ExecutionRisk.",
    )
    spy_in_uptrend: bool = Field(
        default=True,
        description="SPY > 50-MA AND 50-MA > 200-MA. Required by the trend filter.",
    )
    earnings_growth_yoy: float | None = Field(
        default=None,
        description="YoY net-income growth used as MVP catalyst proxy (plan §4.3 calls "
        "for EPS-beats-estimate; deferred to FMP earnings_surprise endpoint).",
    )
    pullback_or_breakout: str | None = Field(
        default=None,
        description="'pullback_to_10ma' | 'pullback_to_20ma' | 'breakout_above_50ma' | None.",
    )
    notes: str | None = None


class PhaseAssessment(BaseModel):
    """Output of LifecycleAnalysis — phase + concrete entry/stop/target levels.

    Vector exits via either profit target (+15%), trailing stop after +10%, or
    hard stop −7%. The trailing stop arms after the position is up
    ``TRAILING_STOP_TRIGGER_PCT`` from entry; from then on, ``trail_high_water``
    tracks the highest close and the exit fires if close drops more than
    ``TRAILING_STOP_PCT`` below it.
    """

    model_config = ConfigDict(extra="forbid")

    ticker: str
    as_of: date
    phase: Phase
    entry_price: Decimal
    stop_price: Decimal
    profit_target_price: Decimal
    days_held: int = Field(default=0, ge=0)
    trailing_armed: bool = Field(
        default=False,
        description="True once close has reached entry × (1 + TRAILING_STOP_TRIGGER_PCT).",
    )
    trail_high_water: Decimal | None = Field(
        default=None,
        description="Highest close since entry, used to compute the trailing stop.",
    )
    early_cut_applied: bool = Field(
        default=False,
        description="True iff close < 10-MA in days 1–3 → 50% size reduction has been applied.",
    )
    notes: str | None = None


class ExecutionDecision(BaseModel):
    """Output of ExecutionRisk — Alpaca paper bracket order + sizing facts.

    Vector uses a single bracket per position: market entry at next open,
    take-profit limit at +15%, stop-loss at −7%. ``order_payloads`` is a
    one-element list (matching Sigma/Reversion's shape so
    ``AlpacaPaperBrokerAdapter.submit_execution_decision`` works as-is);
    the single payload includes ``client_order_id`` for the order manager
    to track. The trailing-stop logic is handled by LifecycleAnalysis
    re-evaluating each session — the bracket's stop-loss is the static
    −7% floor.
    """

    model_config = ConfigDict(extra="forbid")

    ticker: str
    qty: int
    notional_usd: Decimal
    risk_amount_usd: Decimal = Field(description="qty * (entry - stop). Capital at risk if stop fills.")
    vix_size_factor: Decimal = Field(
        description="Multiplier applied to base size: 1.0 (low VIX), 0.5 (VIX>25), 0.25 (VIX>30)."
    )
    order_payloads: list[dict] = Field(
        description="Single-element list containing the bracket payload (entry + TP + SL).",
    )
    constructed_at: datetime
