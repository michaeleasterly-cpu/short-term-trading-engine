"""After-Action Report (AAR) Pydantic models.

Every closed trade emits exactly one AAR. AARs feed Forensics (later) and
the credibility rubric for graduating engines from paper to live.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class ExitReason(str, Enum):
    TAKE_PROFIT = "take_profit"
    TIER1_MID_BAND = "tier1_mid_band"
    TIER2_OPPOSITE_BAND = "tier2_opposite_band"
    STOP_LOSS = "stop_loss"
    TIME_STOP = "time_stop"
    THESIS_BROKEN = "thesis_broken"
    REGIME_FLIP = "regime_flip"
    RISK_GOVERNOR_FORCE_FLAT = "risk_governor_force_flat"
    TAX_HARVEST = "tax_harvest"
    MANUAL = "manual"
    OTHER = "other"


class AfterActionReport(BaseModel):
    """One AAR per closed trade. UTC timestamps throughout."""

    model_config = ConfigDict(extra="forbid", frozen=False)

    engine: str
    trade_id: str
    ticker: str

    entry_ts: datetime
    exit_ts: datetime
    entry_price: Decimal
    exit_price: Decimal
    qty: Decimal

    confidence_at_entry: Decimal = Field(ge=0, le=1)
    confidence_at_exit: Decimal | None = Field(default=None, ge=0, le=1)
    sizing_pct_of_engine_equity: Decimal

    pnl_gross: Decimal
    pnl_net: Decimal
    fees: Decimal = Decimal("0")
    slippage_bps: Decimal | None = None

    regime_tags: list[str] = Field(default_factory=list)
    exit_reason: ExitReason
    rule_compliance: bool = Field(
        description="True iff the trade obeyed every codified rule of the strategy."
    )
    notes: str | None = None
