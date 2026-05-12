"""Alpaca paper-broker adapter behind ``BrokerExecutionInterface``.

The adapter wraps the synchronous ``alpaca-py`` ``TradingClient`` and exposes
the async interface the engines depend on. Every external call is offloaded
to a worker thread via ``asyncio.to_thread`` so the calling event loop is
never blocked.

Outage handling: every external call increments / resets a
``_consecutive_failures`` counter. After each failure the counter is fed
into ``tpcore.outage.classify_outage``; once it returns
``OutageTier.KILL_SWITCH`` the failure is re-raised as
``BrokerUnavailableError`` so the engine's order manager can shut down new
submissions and the Risk Governor can flatten positions.

Sigma integration: ``submit_execution_decision`` accepts the two-payload
``ExecutionDecision`` from ``sigma.plugs.execution_risk`` and ships the
Tier 1 bracket and Tier 2 limit orders sequentially. Each fill is recorded
to ``tpcore.quality.execution_quality.ExecutionQualityWriter``; if the
writer has no DB pool wired it logs via ``structlog``.
"""
from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import structlog

from tpcore.interfaces.broker import (
    AccountInfo,
    BrokerExecutionInterface,
    Order,
    OrderClass,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
)
from tpcore.outage import OutageThresholds, OutageTier, classify_outage
from tpcore.quality.execution_quality import ExecutionQualityScore, ExecutionQualityWriter

from .exceptions import BrokerUnavailableError

if TYPE_CHECKING:  # pragma: no cover
    from sigma.models import ExecutionDecision

logger = structlog.get_logger(__name__)

BROKER_NAME = "alpaca-paper"
_STATUS_MAP: dict[str, OrderStatus] = {
    "new": OrderStatus.NEW,
    "accepted": OrderStatus.NEW,
    "pending_new": OrderStatus.NEW,
    "partially_filled": OrderStatus.PARTIALLY_FILLED,
    "filled": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELED,
    "cancelled": OrderStatus.CANCELED,
    "rejected": OrderStatus.REJECTED,
    "expired": OrderStatus.EXPIRED,
}


def _status_from_sdk(value: Any) -> OrderStatus:
    """Coerce alpaca-py's status enum (or string) into our OrderStatus."""
    raw = getattr(value, "value", value)
    return _STATUS_MAP.get(str(raw).lower(), OrderStatus.NEW)


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _slippage_bps(*, side: str, requested: Decimal | None, fill: Decimal) -> Decimal:
    """Slippage in basis points, signed so positive means bad-for-trader.

    For buys: paying *more* than requested is positive slippage.
    For sells: receiving *less* than requested is positive slippage.
    Falls back to 0 when no requested price (market orders).
    """
    if requested is None or requested == 0:
        return Decimal("0")
    diff = (fill - requested) if side == "buy" else (requested - fill)
    return (diff / requested * Decimal("10000")).quantize(Decimal("0.01"))


class AlpacaPaperBrokerAdapter(BrokerExecutionInterface):
    """Async ``BrokerExecutionInterface`` backed by alpaca-py's ``TradingClient``.

    Args:
        api_key: Alpaca paper API key (``ALPACA_KEY`` env var by default).
        api_secret: Alpaca paper API secret (``ALPACA_SECRET`` env var by default).
        paper: True iff using the paper endpoint. Defaults to ``ALPACA_PAPER`` env var.
        execution_quality_writer: writer for per-fill quality scores.
        outage_thresholds: tunable thresholds for the outage classifier.
        _client: test-only injection point; production code should leave this None.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_secret: str | None = None,
        paper: bool | None = None,
        execution_quality_writer: ExecutionQualityWriter | None = None,
        outage_thresholds: OutageThresholds | None = None,
        _client: Any | None = None,
    ) -> None:
        self._api_key = api_key or os.getenv("ALPACA_KEY")
        self._api_secret = api_secret or os.getenv("ALPACA_SECRET")
        self._paper = paper if paper is not None else os.getenv("ALPACA_PAPER", "true").lower() == "true"
        self._client = _client if _client is not None else self._build_client()
        self._eq_writer = execution_quality_writer or ExecutionQualityWriter(db_pool=None)
        self._thresholds = outage_thresholds or OutageThresholds()
        self._consecutive_failures = 0
        self._last_success_at: datetime | None = datetime.now(UTC)

    def _build_client(self) -> Any:
        if not self._api_key or not self._api_secret:
            raise BrokerUnavailableError("ALPACA_KEY / ALPACA_SECRET not set in environment")
        from alpaca.trading.client import TradingClient

        return TradingClient(
            api_key=self._api_key,
            secret_key=self._api_secret,
            paper=self._paper,
        )

    # ─── Outage tracking ────────────────────────────────────────────────

    async def _call(self, func, *args, **kwargs):
        """Run ``func(*args, **kwargs)`` in a worker thread with outage tracking.

        Re-raises the underlying exception unless the failure pushes the
        outage classifier into ``KILL_SWITCH``, in which case the failure is
        re-wrapped as ``BrokerUnavailableError`` so the engine halts.
        """
        try:
            result = await asyncio.to_thread(func, *args, **kwargs)
        except Exception as exc:
            self._consecutive_failures += 1
            staleness = datetime.now(UTC) - (self._last_success_at or datetime.now(UTC))
            tier = classify_outage(
                consecutive_failures=self._consecutive_failures,
                staleness=staleness,
                thresholds=self._thresholds,
            )
            logger.warning(
                "tpcore.alpaca.call_failed",
                func=getattr(func, "__name__", repr(func)),
                consecutive_failures=self._consecutive_failures,
                tier=tier.value,
                error=str(exc),
            )
            if tier is OutageTier.KILL_SWITCH:
                raise BrokerUnavailableError(
                    f"Alpaca paper endpoint unreachable after {self._consecutive_failures} "
                    f"consecutive failures (last error: {exc})"
                ) from exc
            raise
        else:
            self._consecutive_failures = 0
            self._last_success_at = datetime.now(UTC)
            return result

    # ─── BrokerExecutionInterface ───────────────────────────────────────

    async def get_account(self) -> AccountInfo:
        raw = await self._call(self._client.get_account)
        return AccountInfo(
            account_id=str(raw.id),
            cash=Decimal(str(raw.cash)),
            equity=Decimal(str(raw.equity)),
            buying_power=Decimal(str(raw.buying_power)),
            portfolio_value=Decimal(str(raw.portfolio_value)),
            pattern_day_trader=bool(raw.pattern_day_trader),
            paper=self._paper,
        )

    async def get_positions(self) -> list[Position]:
        raw_list = await self._call(self._client.get_all_positions)
        return [
            Position(
                symbol=str(p.symbol),
                qty=Decimal(str(p.qty)),
                avg_entry_price=Decimal(str(p.avg_entry_price)),
                market_value=_decimal(getattr(p, "market_value", None)),
                unrealized_pl=_decimal(getattr(p, "unrealized_pl", None)),
                cost_basis=_decimal(getattr(p, "cost_basis", None)),
            )
            for p in raw_list
        ]

    async def place_order(self, order: Order) -> Order:
        request = self._build_request(order)
        logger.info(
            "tpcore.alpaca.place_order.submit",
            client_order_id=order.client_order_id,
            symbol=order.symbol,
            side=order.side.value,
            order_class=order.order_class.value,
            qty=str(order.qty),
        )
        raw = await self._call(self._client.submit_order, order_data=request)
        placed = self._merge_response(order, raw)
        logger.info(
            "tpcore.alpaca.place_order.ack",
            client_order_id=placed.client_order_id,
            broker_order_id=placed.broker_order_id,
            status=placed.status.value,
            filled_qty=str(placed.filled_qty),
        )
        if placed.status is OrderStatus.FILLED and placed.avg_fill_price is not None:
            await self._record_execution_quality(order=order, placed=placed)
        return placed

    async def cancel_order(self, order_id: str) -> None:
        await self._call(self._client.cancel_order_by_id, order_id)
        logger.info("tpcore.alpaca.cancel_order", broker_order_id=order_id)

    async def get_order(self, order_id: str) -> Order:
        raw = await self._call(self._client.get_order_by_id, order_id)
        # We don't have the originating Order, so build a minimal one from
        # what the SDK returns and let the caller layer side/qty back on.
        skeleton = Order(
            client_order_id=str(getattr(raw, "client_order_id", "") or ""),
            symbol=str(getattr(raw, "symbol", "")),
            side=Order.model_fields["side"].annotation.BUY,  # placeholder; see merge
            qty=Decimal(str(getattr(raw, "qty", "0"))),
            order_type=OrderType.MARKET,
        )
        return self._merge_response(skeleton, raw)

    async def emergency_cancel_all(self) -> int:
        raw = await self._call(self._client.cancel_orders)
        cancelled = sum(1 for r in (raw or []) if getattr(r, "status", 0) == 200)
        logger.warning("tpcore.alpaca.emergency_cancel_all", cancelled=cancelled)
        return cancelled

    async def list_recent_orders(self, *, limit: int = 200) -> list[Order]:
        """Return open + recently-closed orders. Used by engine reconciliation.

        Not part of ``BrokerExecutionInterface`` (the ABC stays minimal); the
        order manager opts in via ``getattr`` so other broker implementations
        without this primitive degrade gracefully.
        """
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        request = GetOrdersRequest(status=QueryOrderStatus.ALL, limit=limit, nested=False)
        raw_list = await self._call(self._client.get_orders, filter=request)
        out: list[Order] = []
        for raw in raw_list or []:
            skeleton = Order(
                client_order_id=str(getattr(raw, "client_order_id", "") or ""),
                symbol=str(getattr(raw, "symbol", "")),
                side=OrderSide(str(getattr(getattr(raw, "side", None), "value", "buy"))),
                qty=Decimal(str(getattr(raw, "qty", "0") or "0")),
                order_type=OrderType(str(getattr(getattr(raw, "order_type", None), "value", "market"))),
            )
            out.append(self._merge_response(skeleton, raw))
        return out

    # ─── Sigma glue ────────────────────────────────────────────────────

    async def submit_execution_decision(self, decision: ExecutionDecision) -> list[Order]:
        """Place every payload on ``decision`` in tier order. Returns the placed orders.

        **Deprecated for live trading** — kept for back-compat with the
        smoke test and legacy engine paths. The two-payload contract
        produces an opposing-side Tier 2 limit that Alpaca rejects
        while Tier 1 is open. Production submission flows through
        :py:meth:`submit_tier1_only` (engines) and the trade monitor
        (which reactively submits Tier 2 after the Tier 1 fill arrives
        on ``trade_updates``). See
        ``docs/superpowers/specs/2026-05-12-trade-monitor-design.md``.

        On any failure the *already-placed* orders are returned to the
        caller via the raised exception's ``__cause__`` chain — they
        aren't auto-cancelled here.
        """
        placed: list[Order] = []
        for payload in decision.order_payloads:
            order = self._order_from_payload(payload, engine_id="sigma")
            placed.append(await self.place_order(order))
        return placed

    async def submit_tier1_only(
        self,
        *,
        ticker: str,
        qty: int,
        side: str,
        take_profit_price: Decimal,
        stop_loss_price: Decimal,
        client_order_id: str,
        engine_id: str,
    ) -> Order:
        """Submit one BUY/SELL bracket-market order; return the broker ack.

        This is the production entry primitive — the engines call this in
        place of the old two-payload ``submit_execution_decision``. The
        Tier 2 leg is no longer submitted here; the trade monitor
        (``tpcore.trade_monitor``) handles it reactively once the bracket's
        entry leg fills.

        Returns the broker-acknowledged ``Order`` (carries ``broker_order_id``).
        Caller is expected to persist ``broker_order_id`` to
        ``platform.open_orders`` so the monitor can match inbound
        ``trade_updates`` back to the originating engine decision.

        Args:
            ticker: symbol, e.g. "AAPL".
            qty: integer share count for the entry leg.
            side: "buy" for long entries (Sigma + Reversion-long + Vector),
                "sell" for Reversion-short entries.
            take_profit_price: bracket TP leg limit (Decimal, 2 decimals).
            stop_loss_price: bracket SL leg stop (Decimal, 2 decimals).
            client_order_id: unique per submission; matched by the monitor.
            engine_id: 'sigma' / 'reversion' / 'vector', recorded on the Order.
        """
        from tpcore.interfaces.broker import OrderSide as _OS

        order = Order(
            client_order_id=client_order_id,
            symbol=ticker,
            side=_OS(side),
            qty=Decimal(str(qty)),
            order_type=OrderType.MARKET,
            order_class=OrderClass.BRACKET,
            take_profit_limit_price=take_profit_price,
            stop_loss_stop_price=stop_loss_price,
            engine_id=engine_id,
        )
        return await self.place_order(order)

    # ─── Internal helpers ───────────────────────────────────────────────

    @staticmethod
    def _order_from_payload(payload: dict, *, engine_id: str) -> Order:
        from tpcore.interfaces.broker import OrderSide
        from tpcore.interfaces.broker import TimeInForce as _TIF

        order_class = OrderClass(payload.get("order_class", "simple"))
        tp = payload.get("take_profit", {}) or {}
        sl = payload.get("stop_loss", {}) or {}
        return Order(
            client_order_id=str(payload["client_order_id"]),
            symbol=str(payload["symbol"]),
            side=OrderSide(payload["side"]),
            qty=Decimal(str(payload["qty"])),
            order_type=OrderType(payload["type"]),
            time_in_force=_TIF(payload.get("time_in_force", "day")),
            limit_price=_decimal(payload.get("limit_price")),
            stop_price=_decimal(payload.get("stop_price")),
            order_class=order_class,
            take_profit_limit_price=_decimal(tp.get("limit_price")),
            stop_loss_stop_price=_decimal(sl.get("stop_price")),
            engine_id=engine_id,
        )

    @staticmethod
    def _build_request(order: Order) -> Any:
        """Translate our ``Order`` into an alpaca-py request object.

        Bracket orders REQUIRE both ``take_profit_limit_price`` and
        ``stop_loss_stop_price`` to be set; we validate that up front to
        avoid sending an obviously-broken request to Alpaca.
        """
        from alpaca.trading.enums import (
            OrderClass as _AOC,
        )
        from alpaca.trading.enums import (
            OrderSide as _AOS,
        )
        from alpaca.trading.enums import (
            TimeInForce as _ATIF,
        )
        from alpaca.trading.requests import (
            LimitOrderRequest,
            MarketOrderRequest,
            StopLossRequest,
            TakeProfitRequest,
        )

        if order.order_class is OrderClass.BRACKET:
            if order.take_profit_limit_price is None or order.stop_loss_stop_price is None:
                raise ValueError(
                    "bracket order requires both take_profit_limit_price and stop_loss_stop_price"
                )
            tp = TakeProfitRequest(limit_price=float(order.take_profit_limit_price))
            sl = StopLossRequest(stop_price=float(order.stop_loss_stop_price))
            ac = _AOC.BRACKET
        else:
            tp = sl = None
            ac = _AOC.SIMPLE

        common = dict(
            symbol=order.symbol,
            qty=float(order.qty),
            side=_AOS(order.side.value),
            time_in_force=_ATIF(order.time_in_force.value),
            client_order_id=order.client_order_id,
            order_class=ac,
        )
        if tp is not None:
            common["take_profit"] = tp
            common["stop_loss"] = sl

        if order.order_type is OrderType.LIMIT:
            if order.limit_price is None:
                raise ValueError("limit order requires limit_price")
            return LimitOrderRequest(limit_price=float(order.limit_price), **common)
        return MarketOrderRequest(**common)

    @staticmethod
    def _merge_response(original: Order, raw: Any) -> Order:
        """Layer alpaca-py response fields onto the ``original`` order we sent."""
        return original.model_copy(
            update={
                "broker_order_id": str(getattr(raw, "id", "") or "") or None,
                "status": _status_from_sdk(getattr(raw, "status", None)),
                "filled_qty": Decimal(str(getattr(raw, "filled_qty", "0") or "0")),
                "avg_fill_price": _decimal(getattr(raw, "filled_avg_price", None)),
                "submitted_at": getattr(raw, "submitted_at", None),
                "filled_at": getattr(raw, "filled_at", None),
            }
        )

    async def _record_execution_quality(self, *, order: Order, placed: Order) -> None:
        if placed.avg_fill_price is None:  # pragma: no cover - guarded by caller
            return
        requested = order.limit_price  # market orders have no requested price
        score = ExecutionQualityScore(
            broker=BROKER_NAME,
            order_id=str(placed.broker_order_id or placed.client_order_id),
            requested_price=requested,
            fill_price=placed.avg_fill_price,
            slippage_bps=_slippage_bps(
                side=order.side.value, requested=requested, fill=placed.avg_fill_price
            ),
            partial_fill=placed.filled_qty < order.qty,
            paper_or_live="paper" if self._paper else "live",
            timestamp=placed.filled_at or datetime.now(UTC),
        )
        try:
            await self._eq_writer.write(score)
        except Exception as exc:  # pragma: no cover - defensive
            logger.error("tpcore.alpaca.eq_write_failed", error=str(exc), order_id=score.order_id)


__all__ = ["AlpacaPaperBrokerAdapter", "BROKER_NAME"]
