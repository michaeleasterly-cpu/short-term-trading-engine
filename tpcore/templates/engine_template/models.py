"""Engine template — Pydantic v2 data models.

Every engine on the platform defines (at minimum):

* ``Phase`` — IntEnum-like state machine for the trade's lifecycle.
* ``PhaseAssessment`` — frozen snapshot the setup_detection / lifecycle
  plugs hand to the execution_risk plug (entry price, stop, target).
* ``ExecutionDecision`` — frozen output of ``execution_risk.decide``;
  carries the sized order payloads the order manager will submit.

Position caps / loss-freeze thresholds are module-level constants so the
capital gate + the docs both pull from one source of truth.
"""
from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

# Plan §4.<N> caps — wire these to your engine spec.
PRE_GRAD_POSITION_CAP_USD: Decimal = Decimal("1500")
MAX_CONCURRENT_POSITIONS: int = 4
DAILY_LOSS_FREEZE_PCT: Decimal = Decimal("0.05")
HARD_STOP_PCT: Decimal = Decimal("0.03")


class Phase(StrEnum):
    """Lifecycle phases the engine recognizes. Override to taste."""

    SCANNING = "scanning"
    ARMED = "armed"
    ACTIVE = "active"
    EXIT = "exit"


class PhaseAssessment(BaseModel):
    """Per-ticker per-bar snapshot handed to ``execution_risk.decide``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str
    phase: Phase
    entry_price: Decimal
    stop_price: Decimal
    # TODO: add engine-specific fields (target_price, ma_20, etc.).


class ExecutionDecision(BaseModel):
    """Sized order payloads ready for the broker."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str
    qty: int
    tier1_qty: int
    tier2_qty: int
    notional_usd: Decimal
    risk_amount_usd: Decimal
    order_payloads: list[dict]
    # ``constructed_at`` left to the engine — tz-aware UTC required.
