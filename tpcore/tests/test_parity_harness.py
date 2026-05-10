"""Tests for `tpcore.parity.harness.LivePaperParityHarness`."""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from tpcore.interfaces.broker import (
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
)
from tpcore.parity import LivePaperParityHarness

# ────────────────────────────────────────────────────────────────────────────
# Test doubles
# ────────────────────────────────────────────────────────────────────────────


class _StubBroker:
    """Minimal BrokerExecutionInterface fake — returns a pre-canned fill."""

    def __init__(
        self,
        *,
        fill_price: Decimal | None,
        filled_at: datetime | None,
        place_raises: Exception | None = None,
    ) -> None:
        self.fill_price = fill_price
        self.filled_at = filled_at
        self.place_raises = place_raises
        self.placed: list[Order] = []

    async def place_order(self, order: Order) -> Order:
        if self.place_raises is not None:
            raise self.place_raises
        self.placed.append(order)
        return order.model_copy(
            update={
                "broker_order_id": "broker-" + order.client_order_id,
                "status": OrderStatus.FILLED,
                "avg_fill_price": self.fill_price,
                "filled_at": self.filled_at,
                "filled_qty": order.qty,
            }
        )

    async def get_order(self, order_id: str) -> Order:  # pragma: no cover - unused in fast paths
        raise NotImplementedError


class _CapturingPool:
    """Captures the executemany / execute call args for assertion."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    def acquire(self):  # type: ignore[no-untyped-def]
        return _CM(self)

    async def execute(self, sql, *args):  # type: ignore[no-untyped-def]
        self.calls.append((sql, args))


class _CM:
    def __init__(self, pool: _CapturingPool) -> None:
        self.pool = pool

    async def __aenter__(self) -> _CapturingPool:
        return self.pool

    async def __aexit__(self, *exc) -> None:
        return None


def _order(side: OrderSide = OrderSide.BUY, qty: Decimal = Decimal("100")) -> Order:
    return Order(
        client_order_id="vector_AAA_1234567890",
        symbol="AAA",
        side=side,
        qty=qty,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.DAY,
    )


# ────────────────────────────────────────────────────────────────────────────
# Drift calculation
# ────────────────────────────────────────────────────────────────────────────


async def test_buy_with_higher_live_fill_yields_positive_drift() -> None:
    """Live fills $1 above paper on a 100-share BUY → +1bp drift each cent."""
    paper = _StubBroker(
        fill_price=Decimal("100.00"),
        filled_at=datetime(2026, 5, 10, 14, 30, tzinfo=UTC),
    )
    live = _StubBroker(
        fill_price=Decimal("100.10"),
        filled_at=datetime(2026, 5, 10, 14, 30, 1, tzinfo=UTC),
    )
    pool = _CapturingPool()
    h = LivePaperParityHarness(paper, live, pool)
    rec = await h.submit_pair(_order(side=OrderSide.BUY))
    # +0.10 / 100 × 10000 = +10 bps; convention: positive = live worse for buyer.
    assert rec.drift_bps == Decimal("10.00")
    assert rec.paper_fill_price == Decimal("100.00")
    assert rec.live_fill_price == Decimal("100.10")


async def test_sell_drift_sign_flipped() -> None:
    """For SELL orders, positive bps means live worse (filled at a lower price)."""
    paper = _StubBroker(fill_price=Decimal("50.00"), filled_at=datetime(2026, 5, 10, 14, 30, tzinfo=UTC))
    live = _StubBroker(fill_price=Decimal("49.90"), filled_at=datetime(2026, 5, 10, 14, 30, 1, tzinfo=UTC))
    h = LivePaperParityHarness(paper, live, _CapturingPool())
    rec = await h.submit_pair(_order(side=OrderSide.SELL))
    # Live got a worse sell price by 0.10 / 50 × 10000 = +20 bps.
    assert rec.drift_bps == Decimal("20.00")


# ────────────────────────────────────────────────────────────────────────────
# Live qty + non-blocking failure semantics
# ────────────────────────────────────────────────────────────────────────────


async def test_live_clone_uses_live_qty_default_one() -> None:
    paper = _StubBroker(fill_price=Decimal("100"), filled_at=datetime.now(UTC))
    live = _StubBroker(fill_price=Decimal("100"), filled_at=datetime.now(UTC))
    h = LivePaperParityHarness(paper, live, _CapturingPool())
    await h.submit_pair(_order(qty=Decimal("100")))
    assert paper.placed[0].qty == Decimal("100")
    assert live.placed[0].qty == Decimal("1")  # default live_qty


async def test_live_failure_does_not_raise_and_records_paper_only() -> None:
    paper = _StubBroker(fill_price=Decimal("100"), filled_at=datetime(2026, 5, 10, 14, 30, tzinfo=UTC))
    live = _StubBroker(
        fill_price=None, filled_at=None, place_raises=RuntimeError("alpaca live unauthorized")
    )
    pool = _CapturingPool()
    h = LivePaperParityHarness(paper, live, pool)
    rec = await h.submit_pair(_order())
    assert rec.paper_fill_price == Decimal("100")
    assert rec.live_fill_price is None
    assert rec.drift_bps is None
    # Persistence still happened — operators want partial records too.
    assert len(pool.calls) == 1


async def test_persistence_sql_targets_parity_drift_log() -> None:
    paper = _StubBroker(fill_price=Decimal("100"), filled_at=datetime(2026, 5, 10, 14, 30, tzinfo=UTC))
    live = _StubBroker(fill_price=Decimal("100.05"), filled_at=datetime(2026, 5, 10, 14, 30, 1, tzinfo=UTC))
    pool = _CapturingPool()
    h = LivePaperParityHarness(paper, live, pool)
    await h.submit_pair(_order())
    sql, args = pool.calls[0]
    assert "INSERT INTO platform.parity_drift_log" in sql
    assert args[0] == "vector_AAA_1234567890"


async def test_no_pool_skips_persistence_but_still_returns_record() -> None:
    paper = _StubBroker(fill_price=Decimal("100"), filled_at=datetime.now(UTC))
    live = _StubBroker(fill_price=Decimal("100.05"), filled_at=datetime.now(UTC))
    h = LivePaperParityHarness(paper, live, db_pool=None)
    rec = await h.submit_pair(_order())
    assert rec.drift_bps is not None
