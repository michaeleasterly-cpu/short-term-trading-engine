"""Canonical risk gate for engines WITHOUT an OrderManager.

Per-trade engines gate inside BaseOrderManager.submit_decision. Batch
engines (momentum/sentinel) submit in a per-name scheduler loop; this is
the single shared function they call before each broker.place_order so
the governor enforcement is identical everywhere (persona: one canonical
way, not N variants). On ALLOW it records the opened position so
open_positions / loss caps become real for batch engines too.
"""
from __future__ import annotations

from decimal import Decimal

import structlog

from stelib.interfaces.broker import OrderSide
from stelib.risk.governor import RiskDecision, RiskGovernor

logger = structlog.get_logger(__name__)


async def gate_batch_order(
    governor: RiskGovernor,
    engine_id: str,
    *,
    ticker: str,
    notional: Decimal,
    direction: OrderSide,
    expected_edge_pct: Decimal | None = None,
) -> bool:
    """True iff the order passed the governor (and was recorded as open).

    A False return means SKIP this name and continue the rebalance — a
    blocked name must not abort the whole batch.
    """
    check = await governor.check_trade(
        engine_id=engine_id,
        size=notional,
        direction=direction,
        ticker=ticker,
        expected_edge_pct=expected_edge_pct,
    )
    if check.decision is RiskDecision.BLOCK:
        logger.warning(
            "tpcore.risk.batch_order_blocked",
            engine=engine_id, ticker=ticker,
            notional=str(notional), reason=check.reason,
        )
        return False
    await governor.record_fill(
        engine_id=engine_id, realized_pnl=Decimal("0"), position_delta=1,
    )
    return True
