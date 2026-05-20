"""Per-engine broker-floor attribution (TODO.md L585 follow-up to #251 A1).

Builds on the never-fail-open ``max(proxy, broker_floor)`` raise: this
test surface proves the broker-floor count is now PER-ENGINE (positions
attributed via the ``client_order_id`` prefix of recent orders) instead
of the cross-engine ``len(broker_positions)`` total — without ever
relaxing the safety invariant.

Sacred invariant under test (unchanged from A1): the concurrent-position
check may only ever get TIGHTER than the raw proxy; never looser. The
new code path adds attribution but PRESERVES that property — an
unattributed position counts against the engine being gated (over-count
→ tighter → never-fail-open). A buggy filter that returns 0 on a real
position is still bounded by ``max(proxy, broker_floor)`` (proxy wins).
"""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
import structlog

from tpcore.interfaces.broker import Order, OrderSide, OrderType, Position
from tpcore.order_ids import ENGINE_PREFIX, build_cid
from tpcore.risk.governor import (
    InMemoryRiskStateStore,
    RiskDecision,
    RiskGovernor,
    RiskLimits,
)

# ── Helpers ──────────────────────────────────────────────────────────────────


def _position(symbol: str) -> Position:
    return Position(symbol=symbol, qty=Decimal("1"), avg_entry_price=Decimal("10"))


def _order(symbol: str, engine: str | None) -> Order:
    """Build a minimal Order whose client_order_id is either engine-tagged or bare."""
    cid = build_cid(engine, symbol) if engine is not None else f"manual_{symbol}_x"
    return Order(
        client_order_id=cid,
        symbol=symbol,
        side=OrderSide.BUY,
        qty=Decimal("1"),
        order_type=OrderType.MARKET,
    )


def _broker(
    *,
    positions: list[Position] | None = None,
    recent_orders: list[Order] | None | object = None,
) -> AsyncMock:
    """Broker mock. ``recent_orders`` sentinel:

    * ``None`` (default) → AsyncMock auto-creates list_recent_orders
      (returns AsyncMock — iterates to empty; degraded fallback path).
    * ``"missing"`` → wipe list_recent_orders so duck-type degrades.
    * ``list[Order]`` → wire return_value.
    """
    b = AsyncMock()
    b.get_positions.return_value = positions or []
    b.emergency_cancel_all.return_value = 0
    if recent_orders == "missing":
        # Force the duck-type to return None.
        del b.list_recent_orders
    elif isinstance(recent_orders, list):
        b.list_recent_orders = AsyncMock(return_value=recent_orders)
    return b


async def _registered(
    broker: AsyncMock, engine: str, *, cap: int = 5,
) -> RiskGovernor:
    gov = RiskGovernor(state_store=InMemoryRiskStateStore(), broker=broker)
    await gov.register_engine(
        engine,
        Decimal("100000"),
        limits=RiskLimits(max_open_positions=cap, reconcile_open_floor=True),
    )
    return gov


# ── (1) Per-engine attribution by CID prefix ────────────────────────────────


async def test_filter_attributes_by_engine_cid_prefix() -> None:
    """3 positions tagged momentum/reversion/catalyst, gating momentum:
    broker_floor=1 (only the mo_ position counts against momentum)."""
    positions = [_position("AAPL"), _position("MSFT"), _position("NVDA")]
    recent = [
        _order("AAPL", "momentum"),
        _order("MSFT", "reversion"),
        _order("NVDA", "catalyst"),
    ]
    gov = await _registered(
        _broker(positions=positions, recent_orders=recent), "momentum", cap=2,
    )
    # Proxy=0; only 1 mo_ position → effective=max(0, 1)=1 < cap=2 → ALLOW.
    res = await gov.check_trade("momentum", Decimal("100"), OrderSide.SELL)
    assert res.decision is RiskDecision.ALLOW


async def test_per_engine_count_blocks_at_engine_floor_only() -> None:
    """Two mo_ positions + one rv_ position: gating momentum with cap=2
    → broker_floor=2 → BLOCK (NOT 3 from the cross-engine count)."""
    positions = [_position("AAPL"), _position("MSFT"), _position("NVDA")]
    recent = [
        _order("AAPL", "momentum"),
        _order("MSFT", "momentum"),
        _order("NVDA", "reversion"),
    ]
    gov = await _registered(
        _broker(positions=positions, recent_orders=recent), "momentum", cap=2,
    )
    res = await gov.check_trade("momentum", Decimal("100"), OrderSide.SELL)
    assert res.decision is RiskDecision.BLOCK
    # The BLOCK message reflects effective=2 (per-engine), not 3 (cross-engine).
    assert "2 ≥ 2" in (res.reason or "")


async def test_other_engine_position_excluded_from_count() -> None:
    """A symbol owned exclusively by ANOTHER engine is NOT counted against
    the engine being gated. Cross-engine isolation is the whole point."""
    positions = [_position("RV1"), _position("VC1"), _position("CT1")]
    recent = [
        _order("RV1", "reversion"),
        _order("VC1", "vector"),
        _order("CT1", "catalyst"),
    ]
    gov = await _registered(
        _broker(positions=positions, recent_orders=recent), "momentum", cap=5,
    )
    # No momentum-owned positions → broker_floor=0 → ALLOW even though
    # the cross-engine count would be 3.
    res = await gov.check_trade("momentum", Decimal("100"), OrderSide.SELL)
    assert res.decision is RiskDecision.ALLOW


# ── (2) Unattributed → over-count + WARNING ─────────────────────────────────


async def test_unattributed_position_counts_against_engine_and_warns() -> None:
    """A position whose symbol has NO engine-tagged recent order counts
    against the gating engine (over-count → tighter → never-fail-open)
    AND emits tpcore.risk.unattributed_broker_position."""
    positions = [_position("ORPHAN")]
    # Bare manual order — engine prefix not in ENGINE_PREFIX.
    recent = [_order("ORPHAN", None)]
    gov = await _registered(
        _broker(positions=positions, recent_orders=recent), "momentum", cap=1,
    )
    with structlog.testing.capture_logs() as logs:
        res = await gov.check_trade("momentum", Decimal("100"), OrderSide.SELL)
    # broker_floor=1 (unattributed counts against momentum) → 1 ≥ cap 1 → BLOCK.
    assert res.decision is RiskDecision.BLOCK
    assert any(
        entry.get("event") == "tpcore.risk.unattributed_broker_position"
        and "ORPHAN" in (entry.get("symbols") or [])
        for entry in logs
    ), logs


async def test_no_recent_orders_treats_every_position_as_unattributed() -> None:
    """Empty recent_orders list (vs. AsyncMock-default) → every position
    counts against the gating engine + ONE WARNING per gate."""
    positions = [_position("A"), _position("B"), _position("C")]
    gov = await _registered(
        _broker(positions=positions, recent_orders=[]), "momentum", cap=2,
    )
    with structlog.testing.capture_logs() as logs:
        res = await gov.check_trade("momentum", Decimal("100"), OrderSide.SELL)
    # 3 unattributed ≥ cap 2 → BLOCK.
    assert res.decision is RiskDecision.BLOCK
    warns = [
        e for e in logs
        if e.get("event") == "tpcore.risk.unattributed_broker_position"
    ]
    assert len(warns) == 1  # one WARNING per gate, not per position
    assert warns[0]["n_positions"] == 3


# ── (3) Broker without list_recent_orders → degraded fallback ───────────────


async def test_broker_without_list_recent_orders_degrades_to_xengine() -> None:
    """Broker lacking ``list_recent_orders`` (non-Alpaca / smoke fixtures)
    falls back to the pre-change cross-engine count + warns once."""
    positions = [_position("X"), _position("Y"), _position("Z")]
    gov = await _registered(
        _broker(positions=positions, recent_orders="missing"),
        "momentum", cap=2,
    )
    with structlog.testing.capture_logs() as logs:
        res = await gov.check_trade("momentum", Decimal("100"), OrderSide.SELL)
    # Cross-engine count = 3 ≥ cap 2 → BLOCK (still tighter than proxy-only).
    assert res.decision is RiskDecision.BLOCK
    assert any(
        e.get("event") == "tpcore.risk.broker_attribution_unavailable"
        and e.get("n_positions") == 3
        for e in logs
    ), logs


async def test_list_recent_orders_error_degrades_to_xengine() -> None:
    """``list_recent_orders`` raises → same degraded fallback + warn."""
    positions = [_position("X"), _position("Y")]
    b = _broker(positions=positions, recent_orders=[])
    b.list_recent_orders.side_effect = RuntimeError("alpaca 500")
    gov = await _registered(b, "momentum", cap=1)
    with structlog.testing.capture_logs() as logs:
        res = await gov.check_trade("momentum", Decimal("100"), OrderSide.SELL)
    assert res.decision is RiskDecision.BLOCK  # cross-engine 2 ≥ 1
    assert any(
        e.get("event") == "tpcore.risk.broker_attribution_unavailable"
        and "alpaca 500" in str(e.get("error", ""))
        for e in logs
    ), logs


# ── (4) Buggy / lying filter must not relax proxy ───────────────────────────


async def test_buggy_filter_zero_floor_proxy_still_wins() -> None:
    """If attribution wrongly excludes EVERY real position (e.g. CID
    prefix mistyped) the broker_floor=0 result is bounded by the proxy
    via max() — the proxy still BLOCKs at cap. Never-fail-open."""
    # Position exists, but its recent order is tagged to a DIFFERENT engine
    # → filter excludes it (cross-engine isolation) → broker_floor=0.
    positions = [_position("OWNED_BY_OTHER")]
    recent = [_order("OWNED_BY_OTHER", "reversion")]
    gov = await _registered(
        _broker(positions=positions, recent_orders=recent), "momentum", cap=2,
    )
    # Set proxy to the cap directly.
    st = await gov.state_for("momentum")
    await gov._store.put(st.model_copy(update={"open_positions": 2}))  # noqa: SLF001
    res = await gov.check_trade("momentum", Decimal("100"), OrderSide.SELL)
    # Even though broker_floor=0, proxy 2 ≥ cap 2 wins → BLOCK.
    assert res.decision is RiskDecision.BLOCK
    assert "2 ≥ 2" in (res.reason or "")


# ── (5) Legacy tier-suffix CID → unattributable → WARNING ───────────────────


async def test_legacy_tier_suffix_cid_unattributable_warns() -> None:
    """A legacy ``<TICKER>_<TS>_tier1`` CID (sigma/reversion pre-migration)
    is engine-unknowable per ``parse_cid`` (returns engine=None). The
    corresponding position is treated as unattributed → counts against
    the gating engine + WARNING."""
    positions = [_position("LEGACY")]
    legacy_order = Order(
        client_order_id="LEGACY_1700000000_tier1",  # no canonical engine prefix
        symbol="LEGACY",
        side=OrderSide.BUY,
        qty=Decimal("1"),
        order_type=OrderType.MARKET,
    )
    gov = await _registered(
        _broker(positions=positions, recent_orders=[legacy_order]),
        "momentum", cap=1,
    )
    with structlog.testing.capture_logs() as logs:
        res = await gov.check_trade("momentum", Decimal("100"), OrderSide.SELL)
    assert res.decision is RiskDecision.BLOCK  # 1 unattributed ≥ cap 1
    assert any(
        e.get("event") == "tpcore.risk.unattributed_broker_position"
        and "LEGACY" in (e.get("symbols") or [])
        for e in logs
    ), logs


# ── (6) Never-fail-open invariant across attribution ────────────────────────


@pytest.mark.parametrize("proxy", [0, 1, 3, 5])
@pytest.mark.parametrize("attributed_n", [0, 1, 3, 5])
@pytest.mark.parametrize("crossengine_extra", [0, 2])
async def test_never_fail_open_invariant_across_attribution(
    proxy: int, attributed_n: int, crossengine_extra: int,
) -> None:
    """For ANY (proxy, attributed-to-engine, other-engine-extras):
    the flag-ON decision must NEVER be LOOSER than a proxy-only gate.

    Operationalised: an oracle flag-OFF governor with the same proxy is
    compared against the new attribution path. If the oracle BLOCKs, we
    must also BLOCK. If we ALLOW, the oracle must also ALLOW.
    """
    cap = 5
    mo_positions = [_position(f"MO{i}") for i in range(attributed_n)]
    other_positions = [_position(f"RV{i}") for i in range(crossengine_extra)]
    positions = mo_positions + other_positions
    recent = (
        [_order(p.symbol, "momentum") for p in mo_positions]
        + [_order(p.symbol, "reversion") for p in other_positions]
    )
    on = await _registered(
        _broker(positions=positions, recent_orders=recent), "momentum", cap=cap,
    )
    st = await on.state_for("momentum")
    await on._store.put(st.model_copy(update={"open_positions": proxy}))  # noqa: SLF001

    # Oracle: proxy-only governor (flag-OFF, no broker).
    off = RiskGovernor(state_store=InMemoryRiskStateStore(), broker=_broker())
    await off.register_engine(
        "reversion", Decimal("100000"),
        limits=RiskLimits(max_open_positions=cap, reconcile_open_floor=False),
    )
    off_st = await off.state_for("reversion")
    await off._store.put(off_st.model_copy(update={"open_positions": proxy}))  # noqa: SLF001

    on_res = await on.check_trade("momentum", Decimal("100"), OrderSide.SELL)
    off_res = await off.check_trade("reversion", Decimal("100"), OrderSide.SELL)

    proxy_blocks = off_res.decision is RiskDecision.BLOCK
    on_blocks = on_res.decision is RiskDecision.BLOCK

    # Never-fail-open: if proxy-only BLOCKs, flag-ON must also BLOCK.
    if proxy_blocks:
        assert on_blocks, (
            f"FAIL-OPEN: proxy={proxy} attrib={attributed_n} "
            f"xengine={crossengine_extra} — proxy-only BLOCKs but flag-ON ALLOWs"
        )
    # Sanity: expected effective = max(proxy, attributed_n) — other-engine
    # positions are excluded (whole point of per-engine attribution).
    effective = max(proxy, attributed_n)
    assert effective >= proxy
    if effective >= cap:
        assert on_blocks
    else:
        assert not on_blocks


# ── (7) Drift sentinel: every dispatchable engine has a CID prefix ──────────


def test_every_dispatchable_engine_has_a_cid_prefix() -> None:
    """Per-engine attribution depends on every dispatchable engine having
    a registered ``ENGINE_PREFIX``. This sentinel reds CI the moment a
    new engine is added to ``tpcore.engine_profile._PROFILE`` without a
    matching ``order_ids.ENGINE_PREFIX`` entry — otherwise the
    governor's attribution would silently degrade for that engine."""
    from tpcore.engine_profile import _PROFILE, LifecycleState

    # Skip the two structural non-engine entries + the RETIRED sigma —
    # those never submit orders so attribution is irrelevant.
    excluded = {"allocator", "lab"}
    missing: list[str] = []
    for name, prof in _PROFILE.items():
        if name in excluded:
            continue
        if prof.lifecycle_state is LifecycleState.RETIRED:
            # sigma is retained in ENGINE_PREFIX for historical attribution,
            # but we don't require it; the assertion below covers it
            # incidentally because it IS in ENGINE_PREFIX.
            continue
        if name not in ENGINE_PREFIX:
            missing.append(name)
    assert not missing, (
        f"engines in _PROFILE without an ENGINE_PREFIX: {missing}. "
        "Per-engine broker-floor attribution would silently degrade for "
        "these. Add entries to tpcore/order_ids.py::ENGINE_PREFIX."
    )
