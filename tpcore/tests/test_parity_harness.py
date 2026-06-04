"""Tests for `tpcore.parity.harness.LivePaperParityHarness`."""
from __future__ import annotations

import json
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
    """Captures the executemany / execute call args for assertion.

    Optional ``spread_row``: when set, ``fetchrow`` returns this when the
    harness asks for the latest ``spread_observations`` snapshot.
    """

    def __init__(self, spread_row: dict | None = None) -> None:
        # ``calls`` captures the persistence INSERT (Plan 2: the parity row
        # now lands in data_quality_log via write_row → conn.fetchrow with an
        # INSERT…RETURNING). ``fetchrow_calls`` captures the spread lookup.
        self.calls: list[tuple[str, tuple]] = []
        self.fetchrow_calls: list[tuple[str, tuple]] = []
        self._spread_row = spread_row

    def acquire(self):  # type: ignore[no-untyped-def]
        return _CM(self)

    async def execute(self, sql, *args):  # type: ignore[no-untyped-def]
        self.calls.append((sql, args))

    async def fetchrow(self, sql, *args):  # type: ignore[no-untyped-def]
        if "INSERT INTO platform.data_quality_log" in sql:
            self.calls.append((sql, args))
            return {"?column?": 1}
        self.fetchrow_calls.append((sql, args))
        return self._spread_row


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


async def test_persistence_sql_targets_data_quality_log_parity_kind() -> None:
    paper = _StubBroker(fill_price=Decimal("100"), filled_at=datetime(2026, 5, 10, 14, 30, tzinfo=UTC))
    live = _StubBroker(fill_price=Decimal("100.05"), filled_at=datetime(2026, 5, 10, 14, 30, 1, tzinfo=UTC))
    pool = _CapturingPool()
    h = LivePaperParityHarness(paper, live, pool)
    await h.submit_pair(_order())
    sql, args = pool.calls[0]
    # Plan 2: parity rows land in data_quality_log (kind='parity_drift').
    # write_row binds (kind, source, timestamp, latency_ms, missing_bars,
    #                  stale, confidence, notes_jsonb).
    assert "INSERT INTO platform.data_quality_log" in sql
    assert args[0] == "parity_drift"
    assert args[1] == "vector_AAA_1234567890"
    notes = json.loads(args[7])
    assert notes["client_order_id"] == "vector_AAA_1234567890"


async def test_no_pool_skips_persistence_but_still_returns_record() -> None:
    paper = _StubBroker(fill_price=Decimal("100"), filled_at=datetime.now(UTC))
    live = _StubBroker(fill_price=Decimal("100.05"), filled_at=datetime.now(UTC))
    h = LivePaperParityHarness(paper, live, db_pool=None)
    rec = await h.submit_pair(_order())
    assert rec.drift_bps is not None


# ────────────────────────────────────────────────────────────────────────────
# B7 — spread snapshot at order time
# ────────────────────────────────────────────────────────────────────────────


async def test_persist_includes_spread_snapshot_when_observation_exists() -> None:
    """The latest ``spread_observations`` row for the ticker is queried and
    threaded into both the returned record and the INSERT."""
    snapshot_at = datetime(2026, 5, 12, 19, 55, tzinfo=UTC)
    pool = _CapturingPool(spread_row={"spread_pct": Decimal("0.0017"), "observed_at": snapshot_at})
    paper = _StubBroker(fill_price=Decimal("100"), filled_at=datetime(2026, 5, 12, 19, 55, tzinfo=UTC))
    live = _StubBroker(fill_price=Decimal("100.05"), filled_at=datetime(2026, 5, 12, 19, 55, 1, tzinfo=UTC))
    h = LivePaperParityHarness(paper, live, pool)
    rec = await h.submit_pair(_order())
    # Record carries the snapshot.
    assert rec.spread_at_order_pct == Decimal("0.0017")
    assert rec.spread_observed_at == snapshot_at
    # Lookup happened against the right table.
    assert pool.fetchrow_calls, "harness should have queried platform.spread_observations"
    lookup_sql, lookup_args = pool.fetchrow_calls[0]
    assert "platform.spread_observations" in lookup_sql
    assert lookup_args == ("AAA",)
    # INSERT notes jsonb carries the snapshot values (Plan 2: data_quality_log
    # kind='parity_drift'; spread fields are inside notes, not positional cols).
    insert_args = pool.calls[0][1]
    notes = json.loads(insert_args[7])
    # write_row serializes notes with json.dumps(default=str); Decimal/datetime
    # become their str() form.
    assert notes["spread_at_order_pct"] == "0.0017"
    assert notes["spread_observed_at"] == str(snapshot_at)


async def test_persist_records_null_spread_when_no_observation_yet() -> None:
    """No row → null spread fields in the notes jsonb, no crash (Plan 2:
    data_quality_log kind='parity_drift')."""
    pool = _CapturingPool(spread_row=None)
    paper = _StubBroker(fill_price=Decimal("100"), filled_at=datetime(2026, 5, 12, 19, 55, tzinfo=UTC))
    live = _StubBroker(fill_price=Decimal("100.05"), filled_at=datetime(2026, 5, 12, 19, 55, 1, tzinfo=UTC))
    h = LivePaperParityHarness(paper, live, pool)
    rec = await h.submit_pair(_order())
    assert rec.spread_at_order_pct is None
    assert rec.spread_observed_at is None
    insert_args = pool.calls[0][1]
    notes = json.loads(insert_args[7])
    assert notes["spread_at_order_pct"] is None
    assert notes["spread_observed_at"] is None
