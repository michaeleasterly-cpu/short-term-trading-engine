"""Carver — Pydantic v2 frozen models + module-level constants.

Constants live here so the spec + the plugs + the tests all read from
one source. Updates to caps / vol-target / IDM bounds happen via ECR
MODIFY (Lab-survived dossier) — never hand-tuned past the gate.

See ``docs/superpowers/specs/2026-05-20-carver-design.md`` Section 4 for
the math behind these constants and Section 8 D-CV-3/D-CV-4 for the
decisions.
"""
from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, computed_field, model_validator

# ── Carver-method constants (spec Section 4) ───────────────────────────
FORECAST_TARGET_ABS: int = 10           # rolling abs-forecast target value
FORECAST_CAP_ABS: int = 20              # per-rule + combined hard cap
ANNUALIZED_VOL_TARGET: Decimal = Decimal("0.25")  # Half-Kelly heuristic
IDM_FLOOR: Decimal = Decimal("1.0")     # forecast-diversification multiplier lower bound
IDM_CAP: Decimal = Decimal("2.5")       # FDM upper bound (spec Section 4.1)
MAX_TRADES_PER_INSTRUMENT_PER_YEAR: int = 12  # speed-limit (spec Section 4.2)

# ── Capital / risk caps ────────────────────────────────────────────────
PRE_GRAD_POSITION_CAP_USD: Decimal = Decimal("1500")
MAX_CONCURRENT_POSITIONS: int = 20      # portfolio-scale (vs per-trade 4)
DAILY_LOSS_FREEZE_PCT: Decimal = Decimal("0.05")
DRAWDOWN_BREAKER_LOOKBACK_DAYS: int = 365


class Phase(StrEnum):
    """Lifecycle phases the engine recognizes."""

    SCANNING = "scanning"
    REBALANCE = "rebalance"
    HOLDING = "holding"
    EXIT = "exit"


class CarverForecast(BaseModel):
    """Per-rule scaled + capped forecast for one instrument.

    ``raw`` is the unitless rule output (e.g. EWMAC zero-mean / sigma).
    ``scaled`` is the rule output times the rule's calibration constant
    so the rolling 24-month abs-mean approx ``FORECAST_TARGET_ABS=10``.
    ``capped`` clamps ``scaled`` to
    [-``FORECAST_CAP_ABS``, +``FORECAST_CAP_ABS``] = +/- 20.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    rule: str
    raw: float
    scaled: float
    capped: float | None = None

    @model_validator(mode="before")
    @classmethod
    def _auto_cap(cls, data: object) -> object:
        # Auto-fill the ``capped`` field if caller didn't provide one.
        if not isinstance(data, dict):
            return data
        cap = data.get("capped")
        if cap is None:
            scaled = float(data.get("scaled", 0.0))
            data["capped"] = max(-FORECAST_CAP_ABS, min(FORECAST_CAP_ABS, scaled))
        return data


class CarverAssessment(BaseModel):
    """Per-ticker combined-forecast snapshot — handed to ``execution_risk.decide``.

    ``combined_forecast`` is the equal-weight average times IDM (Forecast
    Diversification Multiplier; bounded [IDM_FLOOR, IDM_CAP]). The derived
    ``combined_capped`` clamps the result to +/- ``FORECAST_CAP_ABS``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: str
    forecasts: list[CarverForecast]
    idm: Decimal
    combined_forecast: float
    instrument_daily_vol_pct: float = 0.0
    instrument_price_usd: Decimal = Decimal("0")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def combined_capped(self) -> float:
        return max(-FORECAST_CAP_ABS, min(FORECAST_CAP_ABS, self.combined_forecast))


class CarverTarget(BaseModel):
    """One row of the target basket (mirrors ``momentum.models.TargetPosition``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: str
    target_shares: int
    target_notional_usd: Decimal
    combined_forecast: float


class RebalanceAction(StrEnum):
    """How a single ticker's order relates to the current basket."""

    OPEN = "open"
    CLOSE = "close"
    INCREASE = "increase"
    DECREASE = "decrease"
    HOLD = "hold"


class RebalanceOrder(BaseModel):
    """A single broker order constructed for this rebalance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: str
    action: RebalanceAction
    side: str  # "buy" | "sell"
    qty: int
    notional_usd: Decimal
    order_payload: dict


class RebalanceDecision(BaseModel):
    """The full output of one rebalance cycle — basket + orders + counts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    targets: list[CarverTarget]
    orders: list[RebalanceOrder]
    n_open: int
    n_close: int
    n_increase: int
    n_decrease: int
    n_hold: int
    total_buy_notional_usd: Decimal


__all__ = [
    "ANNUALIZED_VOL_TARGET",
    "DAILY_LOSS_FREEZE_PCT",
    "DRAWDOWN_BREAKER_LOOKBACK_DAYS",
    "FORECAST_CAP_ABS",
    "FORECAST_TARGET_ABS",
    "IDM_CAP",
    "IDM_FLOOR",
    "MAX_CONCURRENT_POSITIONS",
    "MAX_TRADES_PER_INSTRUMENT_PER_YEAR",
    "PRE_GRAD_POSITION_CAP_USD",
    "CarverAssessment",
    "CarverForecast",
    "CarverTarget",
    "Phase",
    "RebalanceAction",
    "RebalanceDecision",
    "RebalanceOrder",
]
