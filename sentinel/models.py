"""Sentinel — Pydantic data models, constants, and pure helpers.

Sentinel is the macro defense engine (§4.6 of ``docs/MASTER_PLAN.md``).
It is a *portfolio allocation* engine like Momentum, not a per-trade
engine — its outputs are basket weights and batch market orders, not
bracket orders with per-name stops.

Five-plug pipeline:
    SetupDetection → LifecycleAnalysis → ExecutionRisk → CapitalGate → AARLogging

Models defined here are the wire format between the plugs.
"""
from __future__ import annotations

from datetime import date as date_t
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from tpcore.backtest.filter_diagnostics import FilterDiagnostics

# ─── Bear Score scoring constants (master plan §4.6) ────────────────────
#
# The raw score sums to a max of 85; we scale to 0-100 for the activation
# gate so downstream thresholds are interpretable. All thresholds below
# match the spec verbatim — bear-regime triggers we tune via parameter
# sweep go elsewhere.

SAHM_RULE_THRESHOLD = Decimal("0.50")
SAHM_RULE_POINTS = 25

# NB: master plan §4.6 quotes ISM-PMI thresholds (< 45 = contraction).
# The FRED indicator actually loaded into platform.macro_data is
# INDPRO (Industrial Production Index, base 2017=100), not the ISM PMI.
# INDPRO trough during COVID April 2020 was ~84.5; pre-recession baseline
# is ~100. Thresholds below are calibrated to that scale — < 90 = COVID-
# depth contraction; 90-95 = moderate recession territory. The spec's PMI
# numbers are preserved in §4.6 as the *semantic* intent (deep / soft
# contraction); the threshold *values* here implement that intent against
# the indicator we actually have.
INDUSTRIAL_PRODUCTION_HARD_THRESHOLD = Decimal("90.0")
INDUSTRIAL_PRODUCTION_HARD_POINTS = 15
INDUSTRIAL_PRODUCTION_SOFT_LOW = Decimal("90.0")
INDUSTRIAL_PRODUCTION_SOFT_HIGH = Decimal("95.0")
INDUSTRIAL_PRODUCTION_SOFT_POINTS = 10

INITIAL_CLAIMS_THRESHOLD = Decimal("260000")  # 260K
INITIAL_CLAIMS_POINTS = 10

YIELD_CURVE_BEAR_STEEPENER_POINTS = 15

# Credit-spread (Moody's Baa - 10Y Treasury, BAA10Y) graduated scorer.
# Replaced the BAMLH0A0HYM2 HY OAS-based sub-scorer on 2026-05-15 after
# FRED truncated BAMLH0A0HYM2 to a rolling 3-year window. BAA10Y has full
# FRED history from 1996, no truncation. Historical reference points:
# GFC peak ~6% (600 bp), calm periods 200-250 bp, COVID peak ~4.9%.
# Graduated thresholds preserve the existing 5-pt budget so RAW_SCORE_MAX
# stays at 85.
CREDIT_SPREAD_WATCH_THRESHOLD = Decimal("3.00")        # 300 bp
CREDIT_SPREAD_WATCH_POINTS = 2
CREDIT_SPREAD_WARNING_THRESHOLD = Decimal("4.00")      # 400 bp
CREDIT_SPREAD_WARNING_POINTS = 3
CREDIT_SPREAD_RECESSION_THRESHOLD = Decimal("5.00")    # 500 bp
CREDIT_SPREAD_RECESSION_POINTS = 5

VIX_PROXY_HIGH_THRESHOLD = Decimal("25.0")
VIX_PROXY_HIGH_PLUS_MA_POINTS = 15
VIX_PROXY_HIGH_ONLY_POINTS = 10

RAW_SCORE_MAX = 85
SCALED_SCORE_MAX = 100

# Activation / deactivation thresholds.
ACTIVATION_SCORE_THRESHOLD = 60         # ≥ this for ACTIVATION_CONSECUTIVE_DAYS
ACTIVATION_CONSECUTIVE_DAYS = 3
ACTIVATION_RALLY_VETO_PCT = Decimal("0.05")  # SPY rally > 5% during the 3-day window vetoes activation
FALSE_SIGNAL_WINDOW_DAYS = 10            # Bear Score crossed 60 but fell back within this window → full exit
DEACTIVATION_REDUCE_PCT = Decimal("0.50")   # first-step reduction when fading
DEACTIVATION_FADE_DAYS = 5               # remaining 50% scaled out over this many trading days

# Shallow / deep recession discriminator (spec §4.6).
DEEP_RECESSION_SCORE_THRESHOLD = 80
SHALLOW_OVERRIDE_REDUCE_PCT = Decimal("0.50")  # 50% reduction to SH/PSQ when shallow

# VIX circuit breaker.
VIX_CIRCUIT_BREAKER_THRESHOLD = Decimal("40.0")
VIX_CIRCUIT_BREAKER_REDUCE_PCT = Decimal("0.50")  # 50% cut to inverse ETFs (SH/PSQ/SQQQ)

# SQQQ tactical sleeve.
SQQQ_ELIGIBLE_BEAR_SCORE = 80           # only deploy SQQQ when Bear Score ≥ this
SQQQ_ELIGIBLE_VIX_THRESHOLD = Decimal("30.0")  # AND VIX proxy ≥ this
SQQQ_MAX_HOLD_TRADING_DAYS = 5

# Capital allocation.
PRE_GRADUATION_CAP_PCT = Decimal("0.10")
PERMANENT_CAP_PCT = Decimal("0.20")

# VIX proxy = SPY 20-day realized volatility (annualized, in percent).
VIX_PROXY_LOOKBACK_DAYS = 20
TRADING_DAYS_PER_YEAR = 252

# ─── ETF basket (master plan §4.6) ──────────────────────────────────────
#
# Live basket. If any of these tickers is missing from prices_daily at
# backtest or live invocation, the available subset is renormalized to
# 100% — see ``apply_missing_etf_fallback``. This keeps the engine
# operable even before SH/PSQ/GLD price history lands.

BASKET_TICKERS: tuple[str, ...] = ("SH", "PSQ", "TLT", "GLD", "SQQQ")

BASKET_WEIGHTS_DEFAULT: dict[str, Decimal] = {
    "SH":   Decimal("0.35"),
    "PSQ":  Decimal("0.25"),
    "TLT":  Decimal("0.20"),
    "GLD":  Decimal("0.10"),
    "SQQQ": Decimal("0.10"),
}

# Subsets used by the overrides.
INVERSE_BASKET_TICKERS: frozenset[str] = frozenset({"SH", "PSQ", "SQQQ"})
SH_PSQ_TICKERS: frozenset[str] = frozenset({"SH", "PSQ"})

# Graduation criteria (spec §4.6 — per-cycle, not per-trade).
GRAD_MIN_CYCLES = 1
GRAD_MIN_PROFIT_FACTOR = 1.5
GRAD_MAX_DRAWDOWN = Decimal("0.20")  # 20%


# ─── Bear Score breakdown ───────────────────────────────────────────────


class BearScoreBreakdown(BaseModel):
    """Per-indicator point allocation for one observation date.

    All numeric fields are integer points; ``raw_total`` sums them (0-85);
    ``score`` is ``raw_total`` rescaled to 0-100 (the gate threshold of
    60 applies to ``score``, not ``raw_total``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    as_of: date_t
    sahm_pts: int = Field(ge=0, le=SAHM_RULE_POINTS)
    industrial_production_pts: int = Field(ge=0, le=INDUSTRIAL_PRODUCTION_HARD_POINTS)
    initial_claims_pts: int = Field(ge=0, le=INITIAL_CLAIMS_POINTS)
    yield_curve_pts: int = Field(ge=0, le=YIELD_CURVE_BEAR_STEEPENER_POINTS)
    credit_spread_pts: int = Field(ge=0, le=CREDIT_SPREAD_RECESSION_POINTS)
    vix_pts: int = Field(ge=0, le=VIX_PROXY_HIGH_PLUS_MA_POINTS)
    raw_total: int = Field(ge=0, le=RAW_SCORE_MAX)
    score: int = Field(ge=0, le=SCALED_SCORE_MAX)
    # Observability — populated where the upstream data was usable; helps
    # the operator audit a low score with missing sub-indicators.
    indicators_missing: tuple[str, ...] = Field(default_factory=tuple)
    # Per-day FilterDiagnostics for the six Bear Score sub-scorers.
    # ``candidates_passed`` = # of sub-scorers that fired (raw > 0); each
    # ``_blocked`` field = 1 iff that sub-scorer contributed zero. The
    # scheduler lifts this dict onto SIGNAL events' ``extra_data`` so the
    # operator can see *why* a given day did/didn't activate.
    filter_diagnostics: FilterDiagnostics | None = None


# ─── Lifecycle phase + per-day state ────────────────────────────────────


class SentinelPhase(StrEnum):
    DORMANT = "DORMANT"   # Bear Score < threshold; no allocation
    WATCH = "WATCH"       # ≥ threshold but not yet 3 consecutive days
    ACTIVE = "ACTIVE"     # confirmed; full allocation deployed
    FADING = "FADING"     # Bear Score dropped below threshold; scaling out
    EXITED = "EXITED"     # allocation back to zero after a cycle


class SentinelState(BaseModel):
    """Per-day state snapshot. Persisted to ``platform.application_log``
    via AARLogging so backtest + live share the same wire format.

    The ``cycle_id`` ties together one full DORMANT → ACTIVE → EXITED
    arc; it's allocated on the first day of WATCH and stays constant
    until EXITED, then a new id is allocated on the next WATCH entry.
    """

    model_config = ConfigDict(extra="forbid")

    as_of: date_t
    phase: SentinelPhase
    bear_score: int = Field(ge=0, le=SCALED_SCORE_MAX)
    consecutive_days_above_threshold: int = Field(ge=0)
    days_in_phase: int = Field(ge=0)
    cycle_id: int | None = None

    # Override / breaker flags (set by LifecycleAnalysis, consumed by
    # ExecutionRisk). Defaults are off — execution renders a plain basket.
    shallow_recession_override: bool = False
    vix_circuit_breaker: bool = False
    sqqq_eligible: bool = False
    sqqq_days_held: int = 0
    # SPY counter-trend rally during the activation window (used to
    # decide whether WATCH → ACTIVE is allowed); informational once
    # ACTIVE.
    spy_rally_pct_in_window: Decimal = Decimal("0")
    # Fading scale-out: 0.0 = full allocation; rises toward 1.0 over the
    # FADE schedule. Multiplied into the basket weights at order time.
    fade_factor: Decimal = Decimal("0")


# ─── Execution outputs ──────────────────────────────────────────────────


class SentinelTarget(BaseModel):
    """Target basket position for one ETF on one as-of date."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: str
    target_weight: Decimal = Field(ge=0, le=1)
    target_notional_usd: Decimal = Field(ge=0)
    target_shares: int = Field(ge=0)
    last_price: Decimal = Field(gt=0)


class SentinelOrder(BaseModel):
    """One ETF order in the rebalance batch.

    ``side`` and ``qty`` are derived from the diff between the target and
    the current basket holdings; ``qty=0`` orders are filtered out before
    submission.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: str
    side: str  # 'buy' or 'sell'
    qty: int = Field(gt=0)
    notional_usd: Decimal = Field(gt=0)


class SentinelDecision(BaseModel):
    """Output of ExecutionRisk — the basket the scheduler should reach.

    ``targets`` covers every ticker in the *available* basket subset
    (the master spec minus any missing ETFs). ``orders`` is the diff vs
    the current basket holdings (qty=0 deltas filtered).
    """

    model_config = ConfigDict(extra="forbid")

    as_of: date_t
    state: SentinelState
    allocation_cap_pct: Decimal = Field(ge=0, le=1)
    deployable_equity_usd: Decimal = Field(ge=0)
    targets: list[SentinelTarget]
    orders: list[SentinelOrder]
    missing_etfs: tuple[str, ...] = Field(default_factory=tuple)


# ─── Pure helpers (shared between live + backtest) ──────────────────────


def apply_missing_etf_fallback(
    weights: dict[str, Decimal],
    available_tickers: frozenset[str],
) -> dict[str, Decimal]:
    """Drop tickers without price history, renormalize the rest to 1.0.

    Originally added as a workaround when SH/PSQ/GLD were missing from
    ``platform.prices_daily``. As of 2026-05-15 those three are backfilled
    (Alpaca SIP from 2016-01) and the renormalization no longer triggers
    for the canonical basket. Kept in place as a forward-looking safety
    net — if a basket ETF is ever delisted or its data goes stale, the
    engine degrades gracefully instead of erroring. Returns a fresh dict;
    never mutates ``weights``. Returns ``{}`` when no requested ticker is
    available (caller treats as DORMANT).
    """
    kept = {t: w for t, w in weights.items() if t in available_tickers}
    s = sum(kept.values())
    if s <= 0:
        return {}
    return {t: (w / s) for t, w in kept.items()}


def apply_basket_overrides(
    weights: dict[str, Decimal],
    *,
    shallow_recession: bool,
    vix_circuit_breaker: bool,
    sqqq_eligible: bool,
) -> dict[str, Decimal]:
    """Apply the master-plan §4.6 overrides to a basket weight dict.

    Sequence:

    1. SQQQ ineligibility — drop SQQQ if Bear Score < 80 or VIX < 30
       (caller computes the boolean and passes it in).
    2. Shallow-recession override — cut SH/PSQ by 50% if
       ``shallow_recession`` is True.
    3. VIX circuit breaker — cut all inverse ETFs (SH/PSQ/SQQQ — whatever
       survives step 1) by 50% if ``vix_circuit_breaker`` is True.
    4. Renormalize the surviving weights to 1.0 (the freed-up weight
       flows pro-rata into TLT/GLD per the spec's "increase TLT/GLD"
       intent).

    Returns a fresh dict.
    """
    out: dict[str, Decimal] = dict(weights)
    if not sqqq_eligible and "SQQQ" in out:
        del out["SQQQ"]
    if shallow_recession:
        for t in SH_PSQ_TICKERS:
            if t in out:
                out[t] = out[t] * (Decimal("1") - SHALLOW_OVERRIDE_REDUCE_PCT)
    if vix_circuit_breaker:
        for t in INVERSE_BASKET_TICKERS:
            if t in out:
                out[t] = out[t] * (Decimal("1") - VIX_CIRCUIT_BREAKER_REDUCE_PCT)
    # Renormalize.
    s = sum(out.values())
    if s <= 0:
        return {}
    return {t: (w / s) for t, w in out.items()}


__all__ = [
    # Constants
    "SAHM_RULE_THRESHOLD", "SAHM_RULE_POINTS",
    "INDUSTRIAL_PRODUCTION_HARD_THRESHOLD", "INDUSTRIAL_PRODUCTION_HARD_POINTS",
    "INDUSTRIAL_PRODUCTION_SOFT_LOW", "INDUSTRIAL_PRODUCTION_SOFT_HIGH",
    "INDUSTRIAL_PRODUCTION_SOFT_POINTS",
    "INITIAL_CLAIMS_THRESHOLD", "INITIAL_CLAIMS_POINTS",
    "YIELD_CURVE_BEAR_STEEPENER_POINTS",
    "CREDIT_SPREAD_WATCH_THRESHOLD", "CREDIT_SPREAD_WATCH_POINTS",
    "CREDIT_SPREAD_WARNING_THRESHOLD", "CREDIT_SPREAD_WARNING_POINTS",
    "CREDIT_SPREAD_RECESSION_THRESHOLD", "CREDIT_SPREAD_RECESSION_POINTS",
    "VIX_PROXY_HIGH_THRESHOLD", "VIX_PROXY_HIGH_PLUS_MA_POINTS",
    "VIX_PROXY_HIGH_ONLY_POINTS",
    "RAW_SCORE_MAX", "SCALED_SCORE_MAX",
    "ACTIVATION_SCORE_THRESHOLD", "ACTIVATION_CONSECUTIVE_DAYS",
    "ACTIVATION_RALLY_VETO_PCT", "FALSE_SIGNAL_WINDOW_DAYS",
    "DEACTIVATION_REDUCE_PCT", "DEACTIVATION_FADE_DAYS",
    "DEEP_RECESSION_SCORE_THRESHOLD", "SHALLOW_OVERRIDE_REDUCE_PCT",
    "VIX_CIRCUIT_BREAKER_THRESHOLD", "VIX_CIRCUIT_BREAKER_REDUCE_PCT",
    "SQQQ_ELIGIBLE_BEAR_SCORE", "SQQQ_ELIGIBLE_VIX_THRESHOLD",
    "SQQQ_MAX_HOLD_TRADING_DAYS",
    "PRE_GRADUATION_CAP_PCT", "PERMANENT_CAP_PCT",
    "VIX_PROXY_LOOKBACK_DAYS", "TRADING_DAYS_PER_YEAR",
    "BASKET_TICKERS", "BASKET_WEIGHTS_DEFAULT",
    "INVERSE_BASKET_TICKERS", "SH_PSQ_TICKERS",
    "GRAD_MIN_CYCLES", "GRAD_MIN_PROFIT_FACTOR", "GRAD_MAX_DRAWDOWN",
    # Models
    "BearScoreBreakdown", "SentinelPhase", "SentinelState",
    "SentinelTarget", "SentinelOrder", "SentinelDecision",
    # Helpers
    "apply_missing_etf_fallback", "apply_basket_overrides",
]
