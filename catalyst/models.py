"""Catalyst engine — Pydantic v2 data models + module-level constants.

Catalyst is a per-trade swing engine. Each fired SetupCandidate becomes a
single day-market BUY with a flat bracket: take-profit (+12%) + hard
stop (-7%). The trailing stop discipline is handled by the lifecycle
plug between bars (mirrors Vector's pattern, not the Sigma tier-cascade).
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from tpcore.backtest.filter_diagnostics import FilterDiagnostics

# Universe used by the catalyst scan. Liquid, fundamentally trackable
# mid/large caps with active insider-filing coverage — same shape as
# Vector's pilot universe so the two engines see the same backtest
# substrate and a graduation comparison is apples-to-apples.
CATALYST_TEST_UNIVERSE: tuple[str, ...] = (
    "AAPL", "MSFT", "AMZN", "GOOGL", "META",
    "TSLA", "NVDA", "JPM", "V", "WMT",
    "XOM", "CAT", "BA", "MCD", "DIS",
)

# Cluster-detection thresholds (the insider-cluster leg).
CATALYST_CLUSTER_WINDOW_DAYS: int = 30          # rolling lookback window
CATALYST_MIN_DISTINCT_INSIDERS: int = 3         # cluster floor (≥3 distinct buyers)
CATALYST_MIN_AGGREGATE_USD: Decimal = Decimal("250000")  # cluster dollar floor

# Universe-liquidity gate.
MIN_PRICE: Decimal = Decimal("10")
MIN_AVG_VOLUME: int = 1_000_000

# Trend filter — accept only names trading above their 50-SMA.
SMA_TREND_PERIOD: int = 50

# Exit mechanics (flat-bracket per-trade).
HARD_STOP_PCT: Decimal = Decimal("0.07")        # −7% stop
PROFIT_TARGET_PCT: Decimal = Decimal("0.12")    # +12% target
TRAILING_STOP_PCT: Decimal = Decimal("0.05")    # −5% trail from peak
TRAILING_STOP_TRIGGER_PCT: Decimal = Decimal("0.08")  # arm trail at +8%

# Capital gate + position controls.
PRE_GRAD_POSITION_CAP_USD: Decimal = Decimal("1500")
MAX_CONCURRENT_POSITIONS: int = 4
DAILY_LOSS_FREEZE_PCT: Decimal = Decimal("0.05")

# Graduation rubric (read by the capital gate's assert_can_graduate).
GRAD_MIN_TRADES: int = 30
GRAD_MIN_WIN_RATE: float = 0.55
GRAD_MIN_AVG_RETURN: float = 0.03


class Phase(StrEnum):
    """Lifecycle phases — flat-bracket swing (mirrors Vector)."""

    ENTRY = "entry"
    HOLDING = "holding"
    EARLY_CUT = "early_cut"
    EXIT = "exit"


class InsiderCluster(BaseModel):
    """Per-ticker insider-cluster summary — pure construction, no DB."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str
    as_of: date
    window_days: int
    distinct_insiders: int = Field(ge=0)
    aggregate_value_usd: Decimal = Field(ge=Decimal("0"))
    n_buy_transactions: int = Field(ge=0)


class SetupCandidate(BaseModel):
    """Output of SetupDetection — one row per qualifying ticker.

    ``cluster_density`` is the engine's pre-registered primary signal —
    a dollar-weighted cluster intensity (aggregate insider-BUY $ value
    in the window, scaled by the distinct-insider count so a single big
    block from one CEO scores lower than a quorum of officers buying).
    """

    model_config = ConfigDict(extra="forbid")

    ticker: str
    as_of: date
    cluster: InsiderCluster
    cluster_density: float = Field(
        ge=0.0,
        description="Aggregate BUY $ × distinct-insiders (USD × count); "
                    "the engine's single pre-registered ranking score.",
    )
    last_close: Decimal
    sma_50: Decimal
    avg_volume: int
    notes: str | None = None

    filter_diagnostics: FilterDiagnostics | None = Field(
        default=None,
        description="Pass/block counters per filter gate from the scan run.",
    )


class PhaseAssessment(BaseModel):
    """Output of LifecycleAnalysis — phase + concrete entry/stop/target levels."""

    model_config = ConfigDict(extra="forbid")

    ticker: str
    as_of: date
    phase: Phase
    entry_price: Decimal
    stop_price: Decimal
    profit_target_price: Decimal
    days_held: int = Field(default=0, ge=0)
    trailing_armed: bool = Field(default=False)
    trail_high_water: Decimal | None = None
    early_cut_applied: bool = Field(default=False)
    notes: str | None = None


class ExecutionDecision(BaseModel):
    """Output of ExecutionRisk — single bracket per ticker.

    Catalyst uses Vector's flat-bracket shape: a market entry with the
    TP + SL submitted in the same Alpaca bracket call. ``order_payloads``
    is a one-element list (compatible with the broker adapter the
    per-trade engines already exercise).
    """

    model_config = ConfigDict(extra="forbid")

    ticker: str
    qty: int
    notional_usd: Decimal
    risk_amount_usd: Decimal = Field(
        description="qty * (entry - stop). Capital at risk if the stop fills.",
    )
    order_payloads: list[dict]
    constructed_at: datetime
