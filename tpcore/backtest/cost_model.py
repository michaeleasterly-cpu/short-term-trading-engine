"""Transaction cost model for backtests.

Default: 5 bps slippage per side for liquid stocks. Configurable per call.
"""
from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class SimpleCostModel(BaseModel):
    """Symmetric per-side slippage. Override ``slippage_bps`` for illiquid names."""

    model_config = ConfigDict(extra="forbid")

    slippage_bps: Decimal = Decimal("5")  # 0.05% per side
    commission_per_share: Decimal = Decimal("0")
    min_commission: Decimal = Decimal("0")

    def adjusted_fill_price(self, ref_price: Decimal, side: str) -> Decimal:
        """Apply slippage to ``ref_price``. ``side`` is ``"buy"`` or ``"sell"``."""
        bps = self.slippage_bps / Decimal("10000")
        if side == "buy":
            return ref_price * (Decimal("1") + bps)
        if side == "sell":
            return ref_price * (Decimal("1") - bps)
        raise ValueError(f"unknown side: {side!r}")

    def commission(self, qty: Decimal) -> Decimal:
        c = qty * self.commission_per_share
        return max(c, self.min_commission)
