"""Catalyst — Plug 3: Execution & Risk.

Sizes positions and builds an Alpaca flat-bracket order payload (one
market BUY entry + a TP limit child + a SL stop child, all in one
bracket call — Vector's pattern). Pure construction, no broker
submission.

Sizing
------
Notional = ``engine_equity_usd × SIZING_PCT``, capped at
``PRE_GRAD_POSITION_CAP_USD``. The qty is ``floor(notional / entry)``;
the actual notional used is ``qty × entry`` (so per-trade $ exposure is
always strictly ≤ the cap). Raises :class:`SizingError` when the entry
price is non-positive or the computed qty is zero.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import ROUND_FLOOR, Decimal

import structlog

from catalyst.models import (
    HARD_STOP_PCT,
    PRE_GRAD_POSITION_CAP_USD,
    PROFIT_TARGET_PCT,
    ExecutionDecision,
    SetupCandidate,
)
from tpcore.exceptions import SizingError
from tpcore.interfaces.engine_plug import BaseEnginePlug
from tpcore.order_ids import build_cid

logger = structlog.get_logger(__name__)

SIZING_PCT_OF_ENGINE_EQUITY: Decimal = Decimal("0.10")


class CatalystExecutionRisk(BaseEnginePlug):
    """Plug 3 — sizing + Alpaca bracket payload construction."""

    engine_name = "catalyst"

    def __init__(
        self,
        *,
        max_position_usd: Decimal = PRE_GRAD_POSITION_CAP_USD,
        sizing_pct: Decimal = SIZING_PCT_OF_ENGINE_EQUITY,
    ) -> None:
        self._max_position_usd = max_position_usd
        self._sizing_pct = sizing_pct

    def validate_dependencies(self) -> bool:
        return self._max_position_usd > 0 and self._sizing_pct > 0

    def healthcheck(self) -> dict:
        return {
            "engine": self.engine_name,
            "plug": "execution_risk",
            "ok": True,
            "details": {
                "max_position_usd": str(self._max_position_usd),
                "sizing_pct": str(self._sizing_pct),
            },
        }

    def decide(
        self,
        candidate: SetupCandidate,
        *,
        engine_equity_usd: Decimal,
        size_reduction: Decimal = Decimal("1"),
    ) -> ExecutionDecision | None:
        """Build the bracket payload for ``candidate``. Returns ``None``
        if the candidate gates out (e.g. qty rounds to zero after the
        cap is applied — that's a deliberate skip, not an error)."""
        entry = candidate.last_close
        if entry <= 0:
            raise SizingError(
                f"non-positive entry price for {candidate.ticker}: {entry}")
        target_notional = (engine_equity_usd * self._sizing_pct
                           * size_reduction)
        notional = min(target_notional, self._max_position_usd)
        if notional <= 0:
            return None
        qty_decimal = (notional / entry).quantize(
            Decimal("1"), rounding=ROUND_FLOOR)
        qty = int(qty_decimal)
        if qty <= 0:
            return None
        actual_notional = (entry * qty).quantize(Decimal("0.01"))
        stop_price = (entry * (Decimal("1") - HARD_STOP_PCT)).quantize(
            Decimal("0.01"))
        tp_price = (entry * (Decimal("1") + PROFIT_TARGET_PCT)).quantize(
            Decimal("0.01"))
        risk_amount = ((entry - stop_price) * qty).quantize(Decimal("0.01"))
        cid = build_cid("catalyst", candidate.ticker)
        payload = {
            "client_order_id": cid,
            "symbol": candidate.ticker,
            "qty": qty,
            "side": "buy",
            "type": "market",
            "time_in_force": "day",
            "order_class": "bracket",
            "take_profit": {"limit_price": str(tp_price)},
            "stop_loss": {"stop_price": str(stop_price)},
        }
        return ExecutionDecision(
            ticker=candidate.ticker,
            qty=qty,
            notional_usd=actual_notional,
            risk_amount_usd=risk_amount,
            order_payloads=[payload],
            constructed_at=datetime.now(UTC),
        )


__all__ = ["CatalystExecutionRisk"]
