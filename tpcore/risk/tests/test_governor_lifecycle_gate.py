"""P2c — RiskGovernor.check_lifecycle gate (2026-05-31).

Verifies the capital gate refuses new orders against tickers whose
``platform.ticker_classifications.issuer_lifecycle_state`` is
terminal (``'deregistered'`` / ``'delist_effective'``). Cheap indexed
read; fires BEFORE the broker round-trip so a known-terminated name
short-circuits without any broker API cost.

NULL state (pre-backfill default for most of the universe today) →
ALLOW, the operator-correct fallback while P2a coverage extends.

All tests hermetic — fake asyncpg.Pool mocked at the row level.
"""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from tpcore.interfaces.broker import OrderSide
from tpcore.risk.governor import (
    InMemoryRiskStateStore,
    RiskDecision,
    RiskGovernor,
)


def _mock_pool_with_lifecycle_state(state: str | None) -> MagicMock:
    """asyncpg.Pool stub whose ``acquire().fetchrow(_SQL, ticker)``
    returns the requested issuer_lifecycle_state."""
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"issuer_lifecycle_state": state})
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire)
    return pool


def _mock_pool_unknown_ticker() -> MagicMock:
    """Ticker not present in classifications — fetchrow returns None."""
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=None)
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire)
    return pool


@pytest.fixture
def fake_broker() -> AsyncMock:
    broker = AsyncMock()
    broker.get_positions.return_value = []
    broker.emergency_cancel_all.return_value = 0
    return broker


# ─── A. deregistered → BLOCK ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_p2c_a_deregistered_state_blocks(
    fake_broker: AsyncMock,
) -> None:
    """A ticker with SEC Form 15 evidence (deregistered) MUST be
    blocked at the capital gate. Operator hard rule: no orders into
    issuers whose SEC reporting obligation is terminated."""
    pool = _mock_pool_with_lifecycle_state("deregistered")
    gov = RiskGovernor(
        state_store=InMemoryRiskStateStore(),
        broker=fake_broker, pool=pool,
    )
    result = await gov.check_lifecycle("ATVI")
    assert result.decision is RiskDecision.BLOCK
    assert "deregistered" in result.reason
    assert "ATVI" in result.reason


# ─── B. delist_effective → BLOCK ─────────────────────────────────────


@pytest.mark.asyncio
async def test_p2c_b_delist_effective_state_blocks(
    fake_broker: AsyncMock,
) -> None:
    pool = _mock_pool_with_lifecycle_state("delist_effective")
    gov = RiskGovernor(
        state_store=InMemoryRiskStateStore(),
        broker=fake_broker, pool=pool,
    )
    result = await gov.check_lifecycle("TUP")
    assert result.decision is RiskDecision.BLOCK
    assert "delist_effective" in result.reason


# ─── C. active → ALLOW ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_p2c_c_active_state_allows(fake_broker: AsyncMock) -> None:
    pool = _mock_pool_with_lifecycle_state("active")
    gov = RiskGovernor(
        state_store=InMemoryRiskStateStore(),
        broker=fake_broker, pool=pool,
    )
    result = await gov.check_lifecycle("AAPL")
    assert result.decision is RiskDecision.ALLOW


# ─── D. NULL state (pre-backfill default) → ALLOW ───────────────────


@pytest.mark.asyncio
async def test_p2c_d_null_state_allows(fake_broker: AsyncMock) -> None:
    """NULL state (~97% of the universe today, pre-backfill) → ALLOW.
    The lifecycle gate is additive; absence of evidence is NOT
    treated as evidence of termination."""
    pool = _mock_pool_with_lifecycle_state(None)
    gov = RiskGovernor(
        state_store=InMemoryRiskStateStore(),
        broker=fake_broker, pool=pool,
    )
    result = await gov.check_lifecycle("AAPL")
    assert result.decision is RiskDecision.ALLOW


# ─── E. delist_pending → ALLOW (reserved state) ─────────────────────


@pytest.mark.asyncio
async def test_p2c_e_delist_pending_allows(fake_broker: AsyncMock) -> None:
    """``delist_pending`` is reserved for the future 8-K Item 3.01
    extractor. The Form 25 hasn't been filed yet, so the security
    still trades on the primary listing — orders are still allowed
    until the delist is actually effective."""
    pool = _mock_pool_with_lifecycle_state("delist_pending")
    gov = RiskGovernor(
        state_store=InMemoryRiskStateStore(),
        broker=fake_broker, pool=pool,
    )
    result = await gov.check_lifecycle("SOMETICKER")
    assert result.decision is RiskDecision.ALLOW


# ─── F. unknown ticker → ALLOW (defer to universe filter) ────────────


@pytest.mark.asyncio
async def test_p2c_f_unknown_ticker_allows(fake_broker: AsyncMock) -> None:
    """A ticker not in ticker_classifications → ALLOW. The lifecycle
    gate is additive; existence is the universe filter's job, not
    this gate's. The operator's universe pipeline catches anything
    that shouldn't be tradeable."""
    pool = _mock_pool_unknown_ticker()
    gov = RiskGovernor(
        state_store=InMemoryRiskStateStore(),
        broker=fake_broker, pool=pool,
    )
    result = await gov.check_lifecycle("WEIRDSYMBOL")
    assert result.decision is RiskDecision.ALLOW


# ─── G. no DB pool → ALLOW (test-friendly fallback) ─────────────────


@pytest.mark.asyncio
async def test_p2c_g_no_pool_allows(fake_broker: AsyncMock) -> None:
    """When the governor is constructed WITHOUT a DB pool, the
    lifecycle gate short-circuits to ALLOW — mirrors check_cost.
    Tests + smoke runs without a DB stay green."""
    gov = RiskGovernor(
        state_store=InMemoryRiskStateStore(),
        broker=fake_broker, pool=None,
    )
    result = await gov.check_lifecycle("ATVI")
    assert result.decision is RiskDecision.ALLOW


# ─── H. check_trade wires the lifecycle gate ─────────────────────────


@pytest.mark.asyncio
async def test_p2c_h_check_trade_blocks_on_terminated_lifecycle(
    fake_broker: AsyncMock,
) -> None:
    """``check_trade`` MUST call check_lifecycle when a ticker is
    provided — confirms the wiring not just the standalone gate."""
    pool = _mock_pool_with_lifecycle_state("deregistered")
    gov = RiskGovernor(
        state_store=InMemoryRiskStateStore(),
        broker=fake_broker, pool=pool,
    )
    await gov.register_engine("reversion", Decimal("10000"))
    result = await gov.check_trade(
        engine_id="reversion",
        size=Decimal("1000"),
        direction=OrderSide.BUY,
        ticker="ATVI",
    )
    assert result.decision is RiskDecision.BLOCK
    assert "lifecycle" in result.reason


@pytest.mark.asyncio
async def test_p2c_i_check_trade_skips_lifecycle_when_ticker_none(
    fake_broker: AsyncMock,
) -> None:
    """When ``ticker`` is omitted from check_trade (some call sites
    gate non-ticker-aware operations), the lifecycle check is
    skipped — preserves the existing engine-only check path."""
    pool = _mock_pool_with_lifecycle_state("deregistered")
    gov = RiskGovernor(
        state_store=InMemoryRiskStateStore(),
        broker=fake_broker, pool=pool,
    )
    await gov.register_engine("reversion", Decimal("10000"))
    result = await gov.check_trade(
        engine_id="reversion",
        size=Decimal("1000"),
        direction=OrderSide.BUY,
        # ticker NOT passed
    )
    # No ticker → no lifecycle check → not blocked on lifecycle
    # grounds. The deregistered state above is therefore irrelevant.
    assert result.decision is RiskDecision.ALLOW
    # And critically: fetchrow was NEVER called (the gate was skipped).
    conn = pool.acquire.return_value.__aenter__.return_value
    assert conn.fetchrow.await_count == 0


@pytest.mark.asyncio
async def test_p2c_j_check_trade_active_state_passes_through(
    fake_broker: AsyncMock,
) -> None:
    """A check_trade against an active ticker MUST reach ALLOW (not
    blocked by lifecycle gate) — confirms the gate doesn't false-
    positive."""
    pool = _mock_pool_with_lifecycle_state("active")
    gov = RiskGovernor(
        state_store=InMemoryRiskStateStore(),
        broker=fake_broker, pool=pool,
    )
    await gov.register_engine("reversion", Decimal("10000"))
    result = await gov.check_trade(
        engine_id="reversion",
        size=Decimal("1000"),
        direction=OrderSide.BUY,
        ticker="AAPL",
    )
    assert result.decision is RiskDecision.ALLOW


# ─── K. lifecycle gate fires BEFORE broker round-trip ────────────────


@pytest.mark.asyncio
async def test_p2c_k_lifecycle_block_short_circuits_broker_call(
    fake_broker: AsyncMock,
) -> None:
    """Performance + safety: the lifecycle BLOCK MUST return before
    any broker.get_positions() call. Saves a broker API roundtrip
    on every terminated name and avoids consuming the rate-limit
    budget for a known-no-go trade."""
    from tpcore.risk.limits_profile import limits_for

    pool = _mock_pool_with_lifecycle_state("delist_effective")
    gov = RiskGovernor(
        state_store=InMemoryRiskStateStore(),
        broker=fake_broker, pool=pool,
    )
    # Use a profile that DOES have reconcile_open_floor=True (momentum
    # / sentinel) so a non-terminated trade WOULD hit get_positions.
    await gov.register_engine(
        "momentum", Decimal("10000"), limits=limits_for("momentum"),
    )
    result = await gov.check_trade(
        engine_id="momentum",
        size=Decimal("1000"),
        direction=OrderSide.BUY,
        ticker="ATVI",
    )
    assert result.decision is RiskDecision.BLOCK
    # broker.get_positions MUST NOT have been called — the lifecycle
    # gate fires before the reconcile_open_floor block.
    fake_broker.get_positions.assert_not_called()
