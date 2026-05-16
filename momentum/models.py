"""Momentum engine — shared Pydantic models and constants.

Five-plug pipeline:
    SetupDetection → LifecycleAnalysis → ExecutionRisk → CapitalGate → AARLogging

Unlike Sigma/Reversion/Vector (per-position, daily-scale), Momentum operates
*portfolio-level* at monthly rebalance cadence. The plug outputs reflect
this: SetupDetection returns a *list* of qualifying tickers, LifecycleAnalysis
decides whether today is a rebalance day, ExecutionRisk produces a *batch* of
order payloads (one per delta-from-current-portfolio), AARLogging writes one
AAR per ticker per rebalance.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from tpcore.backtest.filter_diagnostics import FilterDiagnostics

# Universe tradeability primitives live in tpcore (moved 2026-05-16 to
# enforce the layering invariant — tpcore.universe.prescreener needs
# them and tpcore must never import an engine). Re-exported so every
# momentum call site is unchanged (engine→tpcore is the correct
# direction). Listed in __all__ so the re-export is not "unused".
from tpcore.universe.tradeability import (
    MAX_TIER_FOR_TRADING,
    MIN_PRICE_FLOOR,
    TICKER_SEPARATOR_CHARS,
    WARRANT_MIN_TICKER_LEN,
    WARRANT_SUFFIXES,
    is_tradeable_common_stock,
)

__all__ = [
    "MAX_TIER_FOR_TRADING",
    "MIN_PRICE_FLOOR",
    "TICKER_SEPARATOR_CHARS",
    "WARRANT_MIN_TICKER_LEN",
    "WARRANT_SUFFIXES",
    "is_tradeable_common_stock",
]

# ─── Strategy parameters (live defaults; backtest can override in-process) ──
# 12-1 momentum: lookback over the prior ~12 calendar months, skipping the
# most recent ~1 month to dodge short-term reversal.
LOOKBACK_DAYS = 231       # ~ 11 trading months of return-of-returns
SKIP_DAYS = 21            # ~ 1 trading month skipped before measurement
HOLD_DAYS = 21            # ~ 1 trading month between rebalances
TOP_DECILE_PCT = 0.10     # top 10% of ranked universe enters the portfolio
PER_NAME_CAP_PCT = Decimal("0.01")  # 1% of equity max per ticker (anti-concentration)

# ─── Graduation gate (paper → live) ─────────────────────────────────────────
# Looser than the other engines because monthly rebalance accumulates fewer
# trade-events per unit time. 6 rebalances = 6 months of paper trading.
GRAD_MIN_REBALANCES = 6
GRAD_MIN_SHARPE = 1.0
GRAD_MIN_PROFIT_FACTOR = 1.5


class RebalanceAction(StrEnum):
    """What ExecutionRisk says the order manager should do with a ticker."""

    OPEN = "open"       # not currently held; open at target_notional
    INCREASE = "increase"  # currently held below target; add shares
    DECREASE = "decrease"  # currently held above target; sell shares
    CLOSE = "close"     # currently held; not in new target → sell all
    HOLD = "hold"       # currently held at target — no order needed


class MomentumCandidate(BaseModel):
    """Output of SetupDetection — one row per qualifying ticker.

    The score is the raw 12-1 return; ExecutionRisk re-applies any rank
    filters (top decile) at sizing time so the same candidate set can be
    inspected without committing to a portfolio size yet.
    """

    model_config = ConfigDict(extra="forbid")

    ticker: str
    as_of: date
    momentum_score: float = Field(
        description="12-1 month total return: price(t-skip)/price(t-skip-lookback) - 1.",
    )
    last_close: Decimal
    tier: int = Field(ge=1, le=5, description="Liquidity tier (1=tightest spread).")
    filter_diagnostics: FilterDiagnostics | None = Field(
        default=None,
        description="Per-filter pass/block counters from the scan that produced this candidate.",
    )


class RebalancePlan(BaseModel):
    """Output of LifecycleAnalysis — overall decision for today.

    ``is_rebalance_day`` tells the scheduler whether to fall through to the
    sizing/order steps or short-circuit to a quiet "no action today" exit.
    """

    model_config = ConfigDict(extra="forbid")

    as_of: date
    is_rebalance_day: bool
    reason: str = Field(description="Plain-English why (e.g. 'first trading day of month').")


class TargetPosition(BaseModel):
    """One row in the proposed post-rebalance portfolio.

    The diff against current Alpaca holdings becomes the order list.
    """

    model_config = ConfigDict(extra="forbid")

    ticker: str
    target_notional_usd: Decimal
    target_shares: int = Field(ge=0)
    last_close: Decimal
    momentum_score: float


class RebalanceOrder(BaseModel):
    """Single Alpaca order payload produced by ExecutionRisk.

    Market orders only — Momentum doesn't use stops between rebalances; the
    discipline is "hold to next rebalance, recompute then." Bracket/limit
    orders would add complexity (and tax/slippage) without strategy benefit.
    """

    model_config = ConfigDict(extra="forbid")

    ticker: str
    action: RebalanceAction
    qty: int = Field(gt=0)
    side: str = Field(description="'buy' or 'sell'")
    order_payload: dict = Field(description="Alpaca v2 POST /v2/orders body.")
    notional_usd: Decimal
    constructed_at: datetime


class RebalanceDecision(BaseModel):
    """ExecutionRisk's full output for one rebalance day — the batch of orders
    plus the target portfolio they implement. Persisted as a unit so the AAR
    plug can attribute fills back to the rebalance that scheduled them."""

    model_config = ConfigDict(extra="forbid")

    as_of: date
    targets: list[TargetPosition]
    orders: list[RebalanceOrder]
    total_buy_notional_usd: Decimal
    total_sell_notional_usd: Decimal
    n_open: int = Field(ge=0)
    n_close: int = Field(ge=0)
    n_increase: int = Field(ge=0)
    n_decrease: int = Field(ge=0)
    n_hold: int = Field(ge=0)
