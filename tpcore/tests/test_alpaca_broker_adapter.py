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
        engine_id="reversion",
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


class _StubExecutionDecision:
    """Duck-typed stand-in for an engine's ``ExecutionDecision``.

    ``submit_execution_decision`` only reads ``order_payloads`` (the
    broker is engine-agnostic). tpcore tests must not import an engine
    (layering invariant, 2026-05-16), so the contract is reproduced
    locally instead of importing ``sigma.models``/``reversion.models``.
    """

    def __init__(self, order_payloads: list[dict]) -> None:
        self.order_payloads = order_payloads


async def test_submit_execution_decision_places_two_orders() -> None:
    from datetime import date as date_t

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

    decision = _StubExecutionDecision(
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
    )

    placed = await adapter.submit_execution_decision(decision)

    assert client.submit_order.call_count == 2
    assert len(placed) == 2
    assert placed[0].order_class is OrderClass.BRACKET
    assert placed[0].take_profit_limit_price == Decimal("184.00")
    assert placed[1].order_type is OrderType.LIMIT
    assert placed[1].time_in_force is TimeInForce.GTC

    # Touch the unused import so ruff doesn't strip it on autoformat.
    assert date_t.today().year >= 2025  # noqa: DTZ011


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


# ────────────────────────────────────────────────────────────────────────────
# Transient-error retry on idempotent reads (MED) + the submit-never-retry
# safety invariant. Sleeps are patched out so the suite stays fast.
# ────────────────────────────────────────────────────────────────────────────


def _api_error_with_status(message: str, status_code: int | None) -> APIError:
    """An ``APIError`` whose ``.status_code`` resolves to ``status_code``.

    ``APIError.status_code`` reads ``_http_error.response.status_code`` so we
    inject a duck-typed http_error. ``None`` models a connection-level failure
    (no HTTP response) — which alpaca-py surfaces with no ``_http_error``.
    """
    http_error = None
    if status_code is not None:
        http_error = SimpleNamespace(
            response=SimpleNamespace(status_code=status_code),
            request=SimpleNamespace(),
        )
    return APIError(message, http_error=http_error)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``with_retry``'s backoff instantaneous for these tests."""
    import tpcore.outage.retry as _retry_mod

    async def _instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr(_retry_mod.asyncio, "sleep", _instant)


async def test_transient_5xx_read_is_retried_then_succeeds() -> None:
    """An idempotent read (get_account) that raises a transient 503 twice
    then succeeds returns the success after retrying — pre-fix it raised
    immediately on the first 503."""
    client = MagicMock()
    good = SimpleNamespace(
        id="acct-1",
        cash="10000.00",
        equity="10500.00",
        buying_power="20000.00",
        portfolio_value="10500.00",
        pattern_day_trader=False,
    )
    client.get_account.side_effect = [
        _api_error_with_status("503 Service Unavailable", 503),
        _api_error_with_status("503 Service Unavailable", 503),
        good,
    ]
    adapter = _make_adapter(client=client)

    info = await adapter.get_account()

    assert info.account_id == "acct-1"
    assert client.get_account.call_count == 3  # 1 + 2 retries


async def test_transient_429_read_is_retried() -> None:
    client = MagicMock()
    client.get_all_positions.side_effect = [
        _api_error_with_status("429 Too Many Requests", 429),
        [],
    ]
    adapter = _make_adapter(client=client)

    positions = await adapter.get_positions()

    assert positions == []
    assert client.get_all_positions.call_count == 2


async def test_non_transient_4xx_read_is_not_retried() -> None:
    """A 401 auth error on a read must NOT be retried — it propagates as the
    original APIError immediately (retrying a bad credential just wastes time
    and delays the kill-switch)."""
    client = MagicMock()
    client.get_account.side_effect = _api_error_with_status("401 Unauthorized", 401)
    adapter = _make_adapter(client=client)

    with pytest.raises(APIError):
        await adapter.get_account()
    assert client.get_account.call_count == 1


async def test_exhausted_retries_count_as_one_outage_failure() -> None:
    """Retry sits *inside* one logical ``_call``: a read that exhausts all
    retries counts as exactly ONE consecutive failure, not N — so the
    kill-switch still trips on N *logical* failures, not N transient blips."""
    client = MagicMock()
    client.get_account.side_effect = _api_error_with_status("503", 503)
    thresholds = OutageThresholds(
        availability_consecutive_failures=2,
        kill_consecutive_failures=2,
    )
    adapter = _make_adapter(client=client, outage_thresholds=thresholds)

    # First fully-failed read = 1 consecutive failure (still under kill=2),
    # so it surfaces the original APIError, NOT BrokerUnavailableError.
    with pytest.raises(APIError):
        await adapter.get_account()
    # Second fully-failed read = 2 → kill-switch tier.
    with pytest.raises(BrokerUnavailableError):
        await adapter.get_account()


async def test_order_submit_transient_error_is_NOT_retried() -> None:
    """SAFETY INVARIANT: a transient error on the order-submit path must
    raise after exactly ONE attempt. Retrying a live-money order submit
    risks a double order even with client_order_id. This test must bite if
    anyone later wraps submit in with_retry."""
    client = MagicMock()
    client.submit_order.side_effect = _api_error_with_status("503", 503)
    adapter = _make_adapter(client=client)

    with pytest.raises(APIError):
        await adapter.place_order(_market_order())
    assert client.submit_order.call_count == 1


# ────────────────────────────────────────────────────────────────────────────
# Sub-penny TP/SL quantization (LOW)
# ────────────────────────────────────────────────────────────────────────────


async def test_bracket_tp_sl_quantized_to_two_decimals() -> None:
    """A TP of 10.123 / SL of 9.874 must reach Alpaca as 10.12 / 9.87 —
    Alpaca rejects sub-penny prices; the adapter is the last line of
    defense. ROUND_HALF_UP: .124->.12, .875->.88, .126->.13."""
    client = MagicMock()
    client.submit_order.return_value = _fake_alpaca_order()
    adapter = _make_adapter(client=client)

    await adapter.place_order(
        _market_order(
            order_class=OrderClass.BRACKET,
            take_profit_limit_price=Decimal("10.123"),
            stop_loss_stop_price=Decimal("9.876"),
        )
    )

    req = client.submit_order.call_args.kwargs.get("order_data") or client.submit_order.call_args.args[0]
    assert req.take_profit.limit_price == 10.12
    assert req.stop_loss.stop_price == 9.88  # 9.876 → ROUND_HALF_UP → 9.88


async def test_limit_price_quantized_to_two_decimals() -> None:
    client = MagicMock()
    client.submit_order.return_value = _fake_alpaca_order()
    adapter = _make_adapter(client=client)

    order = _market_order(
        order_type=OrderType.LIMIT,
        limit_price=Decimal("188.005"),
    )
    await adapter.place_order(order)

    req = client.submit_order.call_args.kwargs.get("order_data") or client.submit_order.call_args.args[0]
    assert isinstance(req, LimitOrderRequest)
    assert req.limit_price == 188.01  # ROUND_HALF_UP


async def test_subpenny_price_cannot_reach_broker() -> None:
    """Belt-and-suspenders: whatever the upstream caller sends, the request
    object handed to alpaca-py never carries more than 2 decimal places."""
    client = MagicMock()
    client.submit_order.return_value = _fake_alpaca_order()
    adapter = _make_adapter(client=client)

    await adapter.place_order(
        _market_order(
            order_class=OrderClass.BRACKET,
            take_profit_limit_price=Decimal("123.456789"),
            stop_loss_stop_price=Decimal("0.001"),
        )
    )
    req = client.submit_order.call_args.kwargs.get("order_data") or client.submit_order.call_args.args[0]
    for px in (req.take_profit.limit_price, req.stop_loss.stop_price):
        # No more than 2 decimal places once back in Decimal space.
        assert Decimal(str(px)) == Decimal(str(px)).quantize(Decimal("0.01"))


# ────────────────────────────────────────────────────────────────────────────
# Structural guard on the _call_read seam (live-money double-order foot-gun).
#
# _call (submit path) is NEVER retried; _call_read (idempotent reads) is
# wrapped in with_retry. The split was only naming/docstring-guarded — a
# future dev routing a submit-like method through _call_read would silently
# get live-money retry. The guard makes a mis-route fail LOUDLY at the call.
# ────────────────────────────────────────────────────────────────────────────


async def test_call_read_rejects_non_allowlisted_op() -> None:
    """A mis-routed submit-like op passed to _call_read must RAISE, not
    silently retry. Pre-guard this call would NOT raise (it would happily
    wrap the func in with_retry) — proving the guard is what stops it."""
    adapter = _make_adapter()
    sentinel = MagicMock(return_value="should-never-run")

    with pytest.raises(ValueError, match="not an allowlisted idempotent read"):
        await adapter._call_read(sentinel, op="submit_order")  # noqa: SLF001

    # The func must never have been invoked — the guard rejects BEFORE the
    # retry-wrapped attempt runs.
    sentinel.assert_not_called()


async def test_call_read_accepts_every_allowlisted_op() -> None:
    """Every op in the frozen allowlist must pass the guard (regression-safe:
    the existing read call sites must still work through the guard)."""
    from tpcore.alpaca.broker_adapter import _IDEMPOTENT_READ_OPS

    adapter = _make_adapter()
    for op in _IDEMPOTENT_READ_OPS:
        result = await adapter._call_read(lambda: "ok", op=op)  # noqa: SLF001
        assert result == "ok"


def test_static_scan_every_call_read_caller_is_allowlisted() -> None:
    """CI backstop: parse broker_adapter.py, find every ``self._call_read(``
    call site, and assert each passes an ``op=`` tag drawn from the frozen
    allowlist. Bites if someone adds a _call_read route without updating the
    allowlist (the live-money double-order regression)."""
    import ast
    import inspect

    from tpcore.alpaca import broker_adapter as mod
    from tpcore.alpaca.broker_adapter import _IDEMPOTENT_READ_OPS

    source = inspect.getsource(mod)
    tree = ast.parse(source)

    callers: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        # Match ``self._call_read(...)`` / ``adapter._call_read(...)``.
        if not (isinstance(fn, ast.Attribute) and fn.attr == "_call_read"):
            continue
        op_kw = next((kw for kw in node.keywords if kw.arg == "op"), None)
        assert op_kw is not None, (
            f"_call_read call at line {node.lineno} passes no op= tag — "
            "every read caller MUST declare its idempotent-read op identity"
        )
        assert isinstance(op_kw.value, ast.Constant) and isinstance(op_kw.value.value, str), (
            f"_call_read call at line {node.lineno} op= is not a string literal"
        )
        callers.append(op_kw.value.value)

    # The method definition itself is not a call; we expect exactly the
    # six production read call sites.
    assert len(callers) >= 6, f"expected ≥6 _call_read callers, found {callers}"
    for op in callers:
        assert op in _IDEMPOTENT_READ_OPS, (
            f"_call_read called with non-allowlisted op {op!r} — a new read "
            f"route must be added to _IDEMPOTENT_READ_OPS (live-money guard)"
        )
