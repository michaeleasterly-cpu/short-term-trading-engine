"""#251 Part A1.2 — the never-fail-open ``max(proxy, broker_floor)`` raise.

LIVE-MONEY risk control. The sacred invariant under test: the
concurrent-position check may only ever get TIGHTER (over-count → wrongly
BLOCK = safe); it must NEVER get looser (under-count → wrongly ALLOW past
the per-engine cap = forbidden).

These tests use ``InMemoryRiskStateStore`` + an ``AsyncMock`` broker —
they NEVER touch a real broker/DB/``data/``.
"""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from tpcore.interfaces.broker import OrderSide, Position
from tpcore.risk.governor import (
    InMemoryRiskStateStore,
    RiskDecision,
    RiskGovernor,
    RiskLimits,
)


def _positions(n: int) -> list[Position]:
    """``n`` distinct open positions (cross-engine — no per-engine attribution)."""
    return [
        Position(symbol=f"SYM{i}", qty=Decimal("1"), avg_entry_price=Decimal("10"))
        for i in range(n)
    ]


def _broker(positions: list[Position] | None = None) -> AsyncMock:
    broker = AsyncMock()
    broker.get_positions.return_value = positions if positions is not None else []
    broker.emergency_cancel_all.return_value = 0
    return broker


async def _registered(
    broker: AsyncMock, engine: str, limits: RiskLimits
) -> RiskGovernor:
    gov = RiskGovernor(state_store=InMemoryRiskStateStore(), broker=broker)
    await gov.register_engine(engine, Decimal("100000"), limits=limits)
    return gov


# ── (a) flag True + broker N > proxy → BLOCK at the tighter threshold ────────


async def test_flag_on_broker_higher_than_proxy_blocks_at_tighter_threshold() -> None:
    """proxy=2 (would ALLOW vs cap 5) but broker shows 5 → effective=5 → BLOCK."""
    limits = RiskLimits(max_open_positions=5, reconcile_open_floor=True)
    broker = _broker(_positions(5))
    gov = await _registered(broker, "momentum", limits)
    st = await gov.state_for("momentum")
    await gov._store.put(st.model_copy(update={"open_positions": 2}))  # noqa: SLF001

    res = await gov.check_trade("momentum", Decimal("100"), OrderSide.BUY)

    assert res.decision is RiskDecision.BLOCK
    assert "max concurrent positions" in (res.reason or "")
    # The BLOCK fired off the broker floor, not the (lower) proxy.
    assert "5 ≥ 5" in (res.reason or "")


async def test_flag_on_broker_lower_than_proxy_uses_proxy() -> None:
    """broker under-reports (stale) → max() keeps the proxy; never below proxy."""
    limits = RiskLimits(max_open_positions=5, reconcile_open_floor=True)
    broker = _broker(_positions(1))  # broker stale/low
    gov = await _registered(broker, "momentum", limits)
    st = await gov.state_for("momentum")
    await gov._store.put(st.model_copy(update={"open_positions": 5}))  # noqa: SLF001

    res = await gov.check_trade("momentum", Decimal("100"), OrderSide.BUY)

    assert res.decision is RiskDecision.BLOCK  # proxy 5 ≥ cap 5
    assert "5 ≥ 5" in (res.reason or "")


# ── (b) flag True + broker down/timeout/exception/empty/None → proxy-only ────


@pytest.mark.parametrize(
    "side_effect",
    [
        TimeoutError("broker timeout"),
        ConnectionError("broker down"),
        RuntimeError("broker boom"),
    ],
)
async def test_flag_on_broker_error_concurrent_check_is_proxy_only(
    side_effect: Exception,
) -> None:
    """Any broker error/timeout/exception → broker_floor=0 so the
    CONCURRENT-POSITION check decides exactly as proxy-only would.

    Probed via a SELL (the net-long check is BUY-only, so this isolates
    the §3 'down/timeout/exception → identical to today' row).
    """
    limits = RiskLimits(max_open_positions=5, reconcile_open_floor=True)
    broker = _broker()
    broker.get_positions.side_effect = side_effect
    gov = await _registered(broker, "momentum", limits)
    st = await gov.state_for("momentum")
    await gov._store.put(st.model_copy(update={"open_positions": 3}))  # noqa: SLF001

    res = await gov.check_trade("momentum", Decimal("100"), OrderSide.SELL)

    # proxy 3 < cap 5 → ALLOW exactly as a proxy-only governor would.
    assert res.decision is RiskDecision.ALLOW


@pytest.mark.parametrize(
    "side_effect",
    [TimeoutError("t"), ConnectionError("c"), RuntimeError("r")],
)
async def test_flag_on_broker_error_buy_fails_closed_never_open(
    side_effect: Exception,
) -> None:
    """Flag-ON + broker error + BUY: net-long can't be verified → BLOCK
    (strictly tighter than pre-A1's raise — never fail open).
    """
    limits = RiskLimits(max_open_positions=5, reconcile_open_floor=True)
    broker = _broker()
    broker.get_positions.side_effect = side_effect
    gov = await _registered(broker, "momentum", limits)
    st = await gov.state_for("momentum")
    await gov._store.put(st.model_copy(update={"open_positions": 3}))  # noqa: SLF001

    res = await gov.check_trade("momentum", Decimal("100"), OrderSide.BUY)

    assert res.decision is RiskDecision.BLOCK
    assert "net-long" in (res.reason or "")


@pytest.mark.parametrize("empty", [[], None])
async def test_flag_on_broker_empty_or_none_is_proxy_only(empty: object) -> None:
    limits = RiskLimits(max_open_positions=5, reconcile_open_floor=True)
    broker = _broker()
    broker.get_positions.return_value = empty
    gov = await _registered(broker, "momentum", limits)
    st = await gov.state_for("momentum")
    await gov._store.put(st.model_copy(update={"open_positions": 3}))  # noqa: SLF001

    res = await gov.check_trade("momentum", Decimal("100"), OrderSide.BUY)

    assert res.decision is RiskDecision.ALLOW  # broker_floor=0 → proxy 3 < 5


async def test_flag_on_broker_error_never_looser_than_proxy_block() -> None:
    """Broker error must NOT relax a proxy that already BLOCKS."""
    limits = RiskLimits(max_open_positions=5, reconcile_open_floor=True)
    broker = _broker()
    broker.get_positions.side_effect = TimeoutError("down")
    gov = await _registered(broker, "momentum", limits)
    st = await gov.state_for("momentum")
    await gov._store.put(st.model_copy(update={"open_positions": 5}))  # noqa: SLF001

    res = await gov.check_trade("momentum", Decimal("100"), OrderSide.BUY)

    assert res.decision is RiskDecision.BLOCK  # proxy 5 ≥ 5, broker-error no-op


# ── (c) property/invariant: effective = max(proxy, broker_floor) ≥ proxy ─────


@pytest.mark.parametrize("proxy", range(0, 9))
@pytest.mark.parametrize("broker_n", [0, 1, 2, 5, 8, 12, "error"])
async def test_invariant_effective_never_below_proxy(
    proxy: int, broker_n: object
) -> None:
    """For ALL (proxy≥0, broker_floor≥0) incl. broker-error→0:
    the gate decision is never LOOSER than a proxy-only gate.

    Operationalised: compare the flag-ON decision against a flag-OFF
    (proxy-only) governor with identical state. Flag-ON must BLOCK
    whenever proxy-only BLOCKs, and may additionally BLOCK — never the
    reverse (that would be effective < proxy → fail open).
    """
    cap = 6

    def on_broker() -> AsyncMock:
        b = _broker()
        if broker_n == "error":
            b.get_positions.side_effect = RuntimeError("boom")
        else:
            b.get_positions.return_value = _positions(int(broker_n))
        return b

    # The proxy-only oracle: a flag-OFF governor with a HEALTHY broker.
    # (A broker error only affects A1's floor step; the pre-existing
    # BUY net-long check raising on a broker error is out of A1 scope —
    # the oracle isolates the concurrent-position decision.)
    on = await _registered(
        on_broker(),
        "momentum",
        RiskLimits(max_open_positions=cap, reconcile_open_floor=True),
    )
    off = await _registered(
        _broker(_positions(0)),
        "reversion",
        RiskLimits(max_open_positions=cap, reconcile_open_floor=False),
    )
    for gov, eng in ((on, "momentum"), (off, "reversion")):
        s = await gov.state_for(eng)
        await gov._store.put(s.model_copy(update={"open_positions": proxy}))  # noqa: SLF001

    # SELL isolates the concurrent-position check (the §3 invariant under
    # test); the BUY net-long fail-closed-on-error path is covered
    # separately by test_flag_on_broker_error_buy_fails_closed_never_open.
    on_res = await on.check_trade("momentum", Decimal("100"), OrderSide.SELL)
    off_res = await off.check_trade("reversion", Decimal("100"), OrderSide.SELL)

    proxy_blocks = off_res.decision is RiskDecision.BLOCK
    on_blocks = on_res.decision is RiskDecision.BLOCK

    # Never-fail-open: if proxy-only BLOCKs, flag-ON must also BLOCK.
    if proxy_blocks:
        assert on_blocks, (
            f"FAIL-OPEN: proxy={proxy} broker={broker_n} — proxy-only BLOCKs "
            "but flag-ON ALLOWs (effective < proxy)"
        )
    # The expected effective floor (broker-error/empty → 0).
    floor = 0 if broker_n == "error" else int(broker_n)
    effective = max(proxy, floor)
    assert effective >= proxy
    if effective >= cap:
        assert on_blocks
    else:
        assert not on_blocks


# ── (d) flag-False engine → byte-identical to pre-A1 (raw proxy) ─────────────


@pytest.mark.parametrize("engine", ["reversion", "vector", "unprofiled_default"])
async def test_flag_off_is_byte_identical_to_pre_a1(engine: str) -> None:
    """A flag-OFF engine ignores the broker entirely for the concurrent
    check: huge broker count must NOT block when the raw proxy is under cap.
    """
    limits = RiskLimits(max_open_positions=8, reconcile_open_floor=False)
    broker = _broker(_positions(50))  # would dominate IF the flag leaked
    gov = await _registered(broker, engine, limits)
    st = await gov.state_for(engine)
    await gov._store.put(st.model_copy(update={"open_positions": 7}))  # noqa: SLF001

    res = await gov.check_trade(engine, Decimal("100"), OrderSide.BUY)

    # Pre-A1 behaviour: proxy 7 < cap 8 → ALLOW (broker's 50 ignored).
    assert res.decision is RiskDecision.ALLOW


async def test_flag_off_still_blocks_on_raw_proxy() -> None:
    """Sanity: flag-OFF still BLOCKs when the raw proxy hits the cap."""
    limits = RiskLimits(max_open_positions=8, reconcile_open_floor=False)
    gov = await _registered(_broker(), "reversion", limits)
    st = await gov.state_for("reversion")
    await gov._store.put(st.model_copy(update={"open_positions": 8}))  # noqa: SLF001

    res = await gov.check_trade("reversion", Decimal("100"), OrderSide.BUY)

    assert res.decision is RiskDecision.BLOCK
    assert "8 ≥ 8" in (res.reason or "")


# ── (f) NO second broker round-trip ─────────────────────────────────────────


async def test_no_second_broker_round_trip_flag_on_buy() -> None:
    """check_trade calls get_positions AT MOST once (reuse, not re-fetch)."""
    limits = RiskLimits(max_open_positions=5, reconcile_open_floor=True)
    broker = _broker(_positions(2))
    gov = await _registered(broker, "momentum", limits)

    await gov.check_trade("momentum", Decimal("100"), OrderSide.BUY)

    assert broker.get_positions.await_count <= 1
    assert broker.get_positions.await_count == 1  # exactly once on a BUY


async def test_no_broker_round_trip_flag_off_keeps_existing_buy_behaviour() -> None:
    """Flag-OFF BUY still does the existing single net-long fetch (unchanged)."""
    limits = RiskLimits(max_open_positions=8, reconcile_open_floor=False)
    broker = _broker(_positions(1))
    gov = await _registered(broker, "reversion", limits)

    await gov.check_trade("reversion", Decimal("100"), OrderSide.BUY)

    assert broker.get_positions.await_count == 1
