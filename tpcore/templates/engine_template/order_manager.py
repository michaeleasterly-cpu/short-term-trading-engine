"""Engine template — Order Manager.

Inherits shared scaffolding (``__init__``, ``_persist_tier1_to_open_orders``,
``_fetch_recent_orders``) from :class:`tpcore.order_management.BaseOrderManager`.
Each engine implements ``submit_decision`` and ``reconcile`` for its own
scale-out shape (tier-cascade vs flat-bracket vs batch-market).

Flow:
    1. ``submit_decision`` — engine-local capital gate → platform-wide
       ``RiskGovernor`` → broker submit → persist to ``platform.open_orders``
       → ``governor.record_fill`` (position-delta only, P&L is zero pre-exit).
    2. ``reconcile`` — pull broker order history, fire any state transitions
       (Tier 1 fill / Tier 2 fill / hard stop), write the AARs, update
       ``governor`` with realized P&L on closing fills.
"""
from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

import structlog

from tpcore.aar.models import AfterActionReport
from tpcore.interfaces.broker import Order
from tpcore.order_management import BaseOrderManager

if TYPE_CHECKING:  # pragma: no cover
    pass

logger = structlog.get_logger(__name__)

ENGINE_ID = "ENGINE_NAME"


class EngineNameOrderManager(BaseOrderManager):
    """Drives an ENGINE_NAME trade from execution decision through final AAR."""

    ENGINE_ID = ENGINE_ID

    async def submit_decision(self, decision, assessment) -> list[Order] | None:
        """Run ``decision`` through gates and ship it. Returns placed orders or None.

        Outline (see sigma/order_manager.py for the canonical implementation):

            engine_state = await self._governor.state_for(ENGINE_ID)
            if not self._capital_gate.check_trade(...): return None
            expected_edge = ...  # engine-specific edge calculation
            check = await self._governor.check_trade(...)
            if check.decision is RiskDecision.BLOCK: return None
            order = await self._broker.submit_tier1_only(...)
            trade_key = parse_cid(order.client_order_id).trade_key or order.client_order_id
            await self._persist_tier1_to_open_orders(...)
            self._trade_assessments[trade_key] = assessment
            await self._governor.record_fill(...)
            if self._parity is not None: await self._parity.submit_pair(order)
            return [order]
        """
        raise NotImplementedError

    async def reconcile(
        self,
        *,
        sizing_pct_of_engine_equity: Decimal,
        confidence_at_entry: Decimal = Decimal("0.70"),
    ) -> list[AfterActionReport]:
        """Pull broker history, fire tier events, return any new AARs.

        Group orders via :func:`tpcore.order_ids.parse_cid` — see sigma /
        reversion for the tier-cascade pattern, vector for flat-bracket.
        """
        raise NotImplementedError


__all__ = ["ENGINE_ID", "EngineNameOrderManager"]
