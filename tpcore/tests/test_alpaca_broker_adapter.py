"""Tests for ``tpcore.alpaca.AlpacaPaperBrokerAdapter``.

These tests inject a ``MagicMock`` ``TradingClient`` so we never hit the
network. The mock covers both happy-path responses (filled bracket, filled
limit) and error paths (``APIError`` repeated until the outage classifier
trips the kill-switch tier).
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from alpaca.common.exceptions import APIError
from alpaca.trading.enums import OrderClass as AlpacaOrderClass
from alpaca.trading.enums import OrderSide as AlpacaOrderSide
from alpaca.trading.enums import TimeInForce as AlpacaTIF
from alpaca.trading.requests import (
    LimitOrderRequest,
    MarketOrderRequest,
    StopLossRequest,
    TakeProfitRequest,
)

from tpcore.alpaca import AlpacaPaperBrokerAdapter, BrokerUnavailableError
from tpcore.interfaces.broker import (
    Order,
    OrderClass,
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
)
from tpcore.outage import OutageThresholds
from tpcore.quality.execution_quality import ExecutionQualityScore, ExecutionQualityWriter

# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────


def _fake_alpaca_order(
    *,
    order_id: str = "alp-123",
    client_order_id: str = "AAPL_1700000000_tier1",
    status: str = "filled",
    qty: str = "4",
    filled_qty: str = "4",
    filled_avg_price: str | None = "184.00",
    submitted_at: datetime | None = None,
    filled_at: datetime | None = None,
) -> SimpleNamespace:
    """Mimic the subset of alpaca-py's Order response fields the adapter reads."""
    return SimpleNamespace(
        id=order_id,
        client_order_id=client_order_id,
        status=SimpleNamespace(value=status),
        qty=qty,
        filled_qty=filled_qty,
        filled_avg_price=filled_avg_price,
        submitted_at=submitted_at or datetime(2026, 5, 9, 13, 30, tzinfo=UTC),
        filled_at=filled_at or datetime(2026, 5, 9, 13, 30, 1, tzinfo=UTC),
    )


def _make_adapter(
    client: MagicMock | None = None,
    writer: ExecutionQualityWriter | None = None,
    outage_thresholds: OutageThresholds | None = None,
) -> AlpacaPaperBrokerAdapter:
    """Build an adapter with a mock client and an in-memory quality writer."""
    return AlpacaPaperBrokerAdapter(
        api_key="paper-key",
        api_secret="paper-secret",
        paper=True,
        _client=client or MagicMock(),
        execution_quality_writer=writer or ExecutionQualityWriter(db_pool=None),
        outage_thresholds=outage_thresholds,
    )


def _market_order(**overrides) -> Order:
    base = dict(
        client_order_id="AAPL_1700000000_test",
        symbol="AAPL",
        side=OrderSide.BUY,
        qty=Decimal("4"),
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.DAY,
        engine_id="sigma",
    )
    base.update(overrides)
    return Order(**base)


def _bracket_order() -> Order:
    return _market_order(
        order_class=OrderClass.BRACKET,
        take_profit_limit_price=Decimal("184.00"),
        stop_loss_stop_price=Decimal("174.60"),
    )


# ────────────────────────────────────────────────────────────────────────────
# place_order — request construction
# ────────────────────────────────────────────────────────────────────────────


async def test_place_simple_market_order_uses_market_request() -> None:
    client = MagicMock()
    client.submit_order.return_value = _fake_alpaca_order()
    adapter = _make_adapter(client=client)

    placed = await adapter.place_order(_market_order())

    client.submit_order.assert_called_once()
    req = client.submit_order.call_args.kwargs.get("order_data") or client.submit_order.call_args.args[0]
    assert isinstance(req, MarketOrderRequest)
    assert req.symbol == "AAPL"
    assert req.side is AlpacaOrderSide.BUY
    assert req.time_in_force is AlpacaTIF.DAY
    assert req.order_class is AlpacaOrderClass.SIMPLE
    assert req.client_order_id == "AAPL_1700000000_test"
    assert placed.broker_order_id == "alp-123"
    assert placed.status is OrderStatus.FILLED
    assert placed.filled_at is not None


async def test_place_simple_limit_order_uses_limit_request() -> None:
    client = MagicMock()
    client.submit_order.return_value = _fake_alpaca_order(
        client_order_id="AAPL_1700000000_tier2",
        filled_avg_price="188.00",
    )
    adapter = _make_adapter(client=client)

    order = _market_order(
        client_order_id="AAPL_1700000000_tier2",
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        limit_price=Decimal("188.00"),
        time_in_force=TimeInForce.GTC,
    )
    await adapter.place_order(order)

    req = client.submit_order.call_args.kwargs.get("order_data") or client.submit_order.call_args.args[0]
    assert isinstance(req, LimitOrderRequest)
    assert req.limit_price == 188.0
    assert req.time_in_force is AlpacaTIF.GTC
    assert req.side is AlpacaOrderSide.SELL


async def test_place_bracket_order_attaches_tp_and_sl() -> None:
    client = MagicMock()
    client.submit_order.return_value = _fake_alpaca_order()
    adapter = _make_adapter(client=client)

    await adapter.place_order(_bracket_order())

    req = client.submit_order.call_args.kwargs.get("order_data") or client.submit_order.call_args.args[0]
    assert isinstance(req, MarketOrderRequest)
    assert req.order_class is AlpacaOrderClass.BRACKET
    assert isinstance(req.take_profit, TakeProfitRequest)
    assert isinstance(req.stop_loss, StopLossRequest)
    assert req.take_profit.limit_price == 184.0
    assert req.stop_loss.stop_price == 174.60


async def test_place_bracket_requires_both_legs() -> None:
    """A BRACKET order missing take-profit or stop-loss must be rejected
    before we even ping the broker."""
    adapter = _make_adapter()
    with pytest.raises(ValueError, match="bracket"):
        await adapter.place_order(
            _market_order(order_class=OrderClass.BRACKET, take_profit_limit_price=Decimal("184.00"))
        )


# ────────────────────────────────────────────────────────────────────────────
# Sigma integration
# ────────────────────────────────────────────────────────────────────────────


async def test_submit_execution_decision_places_two_orders() -> None:
    from datetime import date as date_t

    from sigma.models import ExecutionDecision

    client = MagicMock()
    client.submit_order.side_effect = [
        _fake_alpaca_order(order_id="tier1-id", client_order_id="AAPL_1700000000_tier1"),
        _fake_alpaca_order(
            order_id="tier2-id",
            client_order_id="AAPL_1700000000_tier2",
            filled_avg_price="188.00",
        ),
    ]
    adapter = _make_adapter(client=client)

    decision = ExecutionDecision(
        ticker="AAPL",
        qty=8,
        tier1_qty=4,
        tier2_qty=4,
        notional_usd=Decimal("1440.00"),
        risk_amount_usd=Decimal("43.20"),
        order_payloads=[
            {
                "symbol": "AAPL",
                "qty": "4",
                "side": "buy",
                "type": "market",
                "time_in_force": "day",
                "order_class": "bracket",
                "take_profit": {"limit_price": "184.00"},
                "stop_loss": {"stop_price": "174.60"},
                "client_order_id": "AAPL_1700000000_tier1",
            },
            {
                "symbol": "AAPL",
                "qty": "4",
                "side": "sell",
                "type": "limit",
                "limit_price": "188.00",
                "time_in_force": "gtc",
                "client_order_id": "AAPL_1700000000_tier2",
            },
        ],
        constructed_at=datetime(2026, 5, 9, 13, 30, tzinfo=UTC),
    )

    placed = await adapter.submit_execution_decision(decision)

    assert client.submit_order.call_count == 2
    assert len(placed) == 2
    assert placed[0].order_class is OrderClass.BRACKET
    assert placed[0].take_profit_limit_price == Decimal("184.00")
    assert placed[1].order_type is OrderType.LIMIT
    assert placed[1].time_in_force is TimeInForce.GTC

    # Touch the unused import so ruff doesn't strip it on autoformat.
    assert date_t.today().year >= 2025


# ────────────────────────────────────────────────────────────────────────────
# Execution quality recording
# ────────────────────────────────────────────────────────────────────────────


async def test_execution_quality_recorded_on_fill() -> None:
    captured: list[ExecutionQualityScore] = []

    class _Recorder(ExecutionQualityWriter):
        async def write(self, score: ExecutionQualityScore) -> bool:
            captured.append(score)
            return True

    client = MagicMock()
    # BUY limit at 184.00 filled at 184.05 → unfavorable for buyer → +bps.
    client.submit_order.return_value = _fake_alpaca_order(
        client_order_id="AAPL_1700000000_tier1", filled_avg_price="184.05"
    )
    adapter = _make_adapter(client=client, writer=_Recorder(db_pool=None))

    order = _market_order(
        client_order_id="AAPL_1700000000_tier1",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        limit_price=Decimal("184.00"),
        time_in_force=TimeInForce.DAY,
    )
    await adapter.place_order(order)

    assert len(captured) == 1
    score = captured[0]
    assert score.broker == "alpaca-paper"
    assert score.paper_or_live == "paper"
    assert score.fill_price == Decimal("184.05")
    assert score.requested_price == Decimal("184.00")
    # Signed convention: positive bps = unfavorable for trader.
    assert score.slippage_bps > Decimal("0")

    # And the inverse: a SELL filled ABOVE the requested limit is favorable,
    # so slippage should be negative.
    captured.clear()
    client.submit_order.return_value = _fake_alpaca_order(
        client_order_id="AAPL_1700000000_tier2", filled_avg_price="188.05"
    )
    sell = _market_order(
        client_order_id="AAPL_1700000000_tier2",
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        limit_price=Decimal("188.00"),
        time_in_force=TimeInForce.GTC,
    )
    await adapter.place_order(sell)
    assert captured[0].slippage_bps < Decimal("0")


async def test_execution_quality_skipped_when_not_filled() -> None:
    captured: list[ExecutionQualityScore] = []

    class _Recorder(ExecutionQualityWriter):
        async def write(self, score: ExecutionQualityScore) -> bool:
            captured.append(score)
            return True

    client = MagicMock()
    client.submit_order.return_value = _fake_alpaca_order(
        status="new", filled_qty="0", filled_avg_price=None, filled_at=None
    )
    adapter = _make_adapter(client=client, writer=_Recorder(db_pool=None))

    await adapter.place_order(_market_order())
    assert captured == []


# ────────────────────────────────────────────────────────────────────────────
# Outage handling
# ────────────────────────────────────────────────────────────────────────────


async def test_repeated_api_errors_trip_kill_switch() -> None:
    """After enough consecutive ``APIError``s the adapter raises
    ``BrokerUnavailableError`` based on ``classify_outage``."""
    client = MagicMock()
    client.submit_order.side_effect = APIError("504 Gateway Timeout")

    # Tight thresholds so we don't have to call N=10 times.
    thresholds = OutageThresholds(
        availability_consecutive_failures=2,
        kill_consecutive_failures=3,
    )
    adapter = _make_adapter(client=client, outage_thresholds=thresholds)

    # First two failures bubble as APIError (still under kill threshold).
    for _ in range(2):
        with pytest.raises(APIError):
            await adapter.place_order(_market_order())

    # Third hits the kill-switch tier → BrokerUnavailableError.
    with pytest.raises(BrokerUnavailableError):
        await adapter.place_order(_market_order())


async def test_successful_call_resets_failure_counter() -> None:
    client = MagicMock()
    client.submit_order.side_effect = [APIError("transient"), _fake_alpaca_order()]
    thresholds = OutageThresholds(
        availability_consecutive_failures=2,
        kill_consecutive_failures=2,
    )
    adapter = _make_adapter(client=client, outage_thresholds=thresholds)

    with pytest.raises(APIError):
        await adapter.place_order(_market_order())
    # Next call succeeds and resets the counter; another single error must NOT
    # immediately raise BrokerUnavailableError.
    await adapter.place_order(_market_order())

    client.submit_order.side_effect = APIError("transient")
    with pytest.raises(APIError):
        await adapter.place_order(_market_order())


# ────────────────────────────────────────────────────────────────────────────
# Account / positions / cancel / get_order / cancel_all
# ────────────────────────────────────────────────────────────────────────────


async def test_get_account_translates_response() -> None:
    client = MagicMock()
    client.get_account.return_value = SimpleNamespace(
        id="acct-1",
        cash="10000.00",
        equity="10500.00",
        buying_power="20000.00",
        portfolio_value="10500.00",
        pattern_day_trader=False,
    )
    adapter = _make_adapter(client=client)

    info = await adapter.get_account()
    assert info.account_id == "acct-1"
    assert info.cash == Decimal("10000.00")
    assert info.paper is True


async def test_get_positions_translates_response() -> None:
    client = MagicMock()
    client.get_all_positions.return_value = [
        SimpleNamespace(
            symbol="AAPL",
            qty="4",
            avg_entry_price="180.00",
            market_value="736.00",
            unrealized_pl="16.00",
            cost_basis="720.00",
        )
    ]
    adapter = _make_adapter(client=client)
    positions = await adapter.get_positions()
    assert len(positions) == 1
    assert positions[0].symbol == "AAPL"
    assert positions[0].qty == Decimal("4")


async def test_cancel_order_calls_sdk() -> None:
    client = MagicMock()
    adapter = _make_adapter(client=client)
    await adapter.cancel_order("alp-123")
    client.cancel_order_by_id.assert_called_once_with("alp-123")


async def test_get_order_translates_response() -> None:
    client = MagicMock()
    client.get_order_by_id.return_value = _fake_alpaca_order(order_id="alp-123")
    adapter = _make_adapter(client=client)
    fetched = await adapter.get_order("alp-123")
    assert fetched.broker_order_id == "alp-123"
    assert fetched.status is OrderStatus.FILLED


async def test_emergency_cancel_all_returns_count() -> None:
    client = MagicMock()
    client.cancel_orders.return_value = [
        SimpleNamespace(id="o1", status=200),
        SimpleNamespace(id="o2", status=200),
        SimpleNamespace(id="o3", status=422),  # rejected — only 200s should count
    ]
    adapter = _make_adapter(client=client)
    cancelled = await adapter.emergency_cancel_all()
    assert cancelled == 2
