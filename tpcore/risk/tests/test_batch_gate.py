from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from tpcore.interfaces.broker import OrderSide
from tpcore.risk.batch_gate import gate_batch_order
from tpcore.risk.governor import InMemoryRiskStateStore, RiskGovernor
from tpcore.risk.limits_profile import limits_for


@pytest.fixture
def fake_broker() -> AsyncMock:
    """Minimal broker mirroring ``_broker_with_positions()`` — no positions."""
    broker = AsyncMock()
    broker.get_positions.return_value = []
    broker.emergency_cancel_all.return_value = 0
    return broker


async def test_gate_allows_and_records_position(fake_broker: AsyncMock) -> None:
    gov = RiskGovernor(state_store=InMemoryRiskStateStore(), broker=fake_broker)
    await gov.register_engine("sentinel", Decimal("10000"), limits=limits_for("sentinel"))
    ok = await gate_batch_order(
        gov, "sentinel", ticker="SH", notional=Decimal("3500"), direction=OrderSide.BUY
    )
    assert ok is True
    st = await gov.state_for("sentinel")
    assert st.open_positions == 1


async def test_gate_blocks_on_kill_switch(fake_broker: AsyncMock) -> None:
    store = InMemoryRiskStateStore()
    gov = RiskGovernor(state_store=store, broker=fake_broker)
    await gov.register_engine("sentinel", Decimal("10000"))
    await store.set_kill_switch_all(active=True, reason="test")
    ok = await gate_batch_order(
        gov, "sentinel", ticker="SH", notional=Decimal("3500"), direction=OrderSide.BUY
    )
    assert ok is False
    assert (await gov.state_for("sentinel")).open_positions == 0
