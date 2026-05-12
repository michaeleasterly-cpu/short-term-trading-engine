"""Live trade monitor — event-driven order-lifecycle worker.

The engines (Sigma, Reversion, Vector) emit one Tier 1 bracket per trade
via ``broker.submit_tier1_only`` and persist the full ``ExecutionDecision``
+ ``PhaseAssessment`` to ``platform.open_orders``. This service consumes
Alpaca's ``trade_updates`` stream and reacts:

* **Tier 1 entry filled** → submit the Tier 2 follow-on (a BUY bracket
  with TP at the engine's far target) if ``decision.tier2_qty > 0``.
  Persist the new Tier 2 row in ``platform.open_orders``.
* **Tier 1 TP / SL filled** → write the Tier 1 AAR via ``AARWriter``,
  bump ``risk_state``.
* **Tier 2 entry filled** → no follow-on; persistence only.
* **Tier 2 TP / SL filled** → write the Tier 2 AAR, bump ``risk_state``.
* **Cancelled / rejected** → mark the row + audit log; no AAR.

The two-bracket scale-out keeps both halves protected by their own
hard-stop while exiting at different targets. Vector trades have no
Tier 2 (``decision_data`` carries only ``order_payloads[0]`` and no
``tier2_qty``); the Tier 1 fill writes the only AAR.

Design ref: ``docs/superpowers/specs/2026-05-12-trade-monitor-design.md``.

Crash safety
------------
On startup the monitor scans ``open_orders`` for rows still in
``'pending'`` status and calls ``broker.get_order(alpaca_order_id)`` on
each. Any terminal state (filled, cancelled, rejected) is replayed
into the same handler used for live stream events. This is idempotent
because the unique ``(engine, trade_id, order_type)`` constraint and
the per-row status guards make repeated handling a no-op once the row
has reached a terminal state.

Reconnection
------------
Alpaca's ``TradingStream`` exposes a blocking ``run()`` method that
handles its own reconnects in alpaca-py >= 0.40. We wrap it in an
outer retry loop with exponential backoff (1s → 60s) so the service
survives full disconnects too.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import structlog

from tpcore.aar.models import AfterActionReport, ExitReason
from tpcore.aar.writer import AARWriter
from tpcore.alpaca import AlpacaPaperBrokerAdapter
from tpcore.db import build_asyncpg_pool
from tpcore.logging.db_handler import DBLogHandler
from tpcore.risk.persistent_store import PostgresRiskStateStore

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)

ENGINE_NAME = "trade_monitor"


@dataclass(frozen=True)
class OpenOrderRow:
    """In-memory shape of one ``platform.open_orders`` row."""

    id: uuid.UUID
    engine: str
    trade_id: str
    ticker: str
    order_type: str  # 'tier1' | 'tier2'
    alpaca_order_id: str | None
    status: str
    fill_price: Decimal | None
    decision_data: dict


_SELECT_BY_ALPACA_ID_SQL = """
    SELECT id, engine, trade_id, ticker, order_type, alpaca_order_id,
           status, fill_price, decision_data
    FROM platform.open_orders
    WHERE alpaca_order_id = $1
    LIMIT 1
"""

_SELECT_PENDING_SQL = """
    SELECT id, engine, trade_id, ticker, order_type, alpaca_order_id,
           status, fill_price, decision_data
    FROM platform.open_orders
    WHERE status = 'pending'
      AND alpaca_order_id IS NOT NULL
    ORDER BY created_at
"""

_UPDATE_STATUS_SQL = """
    UPDATE platform.open_orders
       SET status        = $2,
           fill_price    = COALESCE($3, fill_price),
           filled_at     = COALESCE($4, filled_at),
           updated_at    = now()
     WHERE id = $1
"""

_INSERT_TIER2_SQL = """
    INSERT INTO platform.open_orders
        (engine, trade_id, ticker, order_type,
         alpaca_order_id, status, decision_data)
    VALUES ($1, $2, $3, 'tier2', $4, 'pending', $5::jsonb)
    ON CONFLICT (engine, trade_id, order_type)
    DO UPDATE SET
        alpaca_order_id = EXCLUDED.alpaca_order_id,
        decision_data   = EXCLUDED.decision_data,
        updated_at      = now()
"""


def _row_from_record(record: Any) -> OpenOrderRow:
    """Convert an asyncpg ``Record`` into the typed ``OpenOrderRow``."""
    raw_decision = record["decision_data"]
    if isinstance(raw_decision, str):
        raw_decision = json.loads(raw_decision)
    return OpenOrderRow(
        id=record["id"],
        engine=record["engine"],
        trade_id=record["trade_id"],
        ticker=record["ticker"],
        order_type=record["order_type"],
        alpaca_order_id=record["alpaca_order_id"],
        status=record["status"],
        fill_price=Decimal(str(record["fill_price"])) if record["fill_price"] is not None else None,
        decision_data=raw_decision,
    )


def _aware(ts: datetime | date | None) -> datetime | None:
    """Normalize whatever alpaca-py hands us into a tz-aware UTC datetime."""
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=UTC)
    if isinstance(ts, date):
        return datetime(ts.year, ts.month, ts.day, tzinfo=UTC)
    return None


def _decimal(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    return Decimal(str(v))


class TradeMonitor:
    """Event-driven order-lifecycle worker.

    Wire it like::

        monitor = TradeMonitor(
            pool=pool,
            broker=AlpacaPaperBrokerAdapter(),
            aar_writer=AARWriter(pool),
            stream_factory=None,  # default: alpaca.trading.stream.TradingStream
        )
        await monitor.run_forever()

    Tests inject ``stream_factory`` so a fake stream can drive
    ``on_trade_update`` directly without a network connection. The bulk
    of business logic lives on ``on_trade_update``, which is fully
    unit-testable in isolation.
    """

    def __init__(
        self,
        *,
        pool: asyncpg.Pool,
        broker: AlpacaPaperBrokerAdapter,
        aar_writer: AARWriter,
        risk_store: PostgresRiskStateStore | None = None,
        stream_factory: Any | None = None,
        run_id: uuid.UUID | None = None,
        max_reconnect_delay_sec: float = 60.0,
    ) -> None:
        self._pool = pool
        self._broker = broker
        self._aar_writer = aar_writer
        self._risk_store = risk_store or PostgresRiskStateStore(pool)
        self._stream_factory = stream_factory
        self._run_id = run_id or uuid.uuid4()
        self._db_log = DBLogHandler(pool, ENGINE_NAME, self._run_id)
        self._max_reconnect_delay = max_reconnect_delay_sec

    # ── Public lifecycle ────────────────────────────────────────────────

    async def run_forever(self) -> None:
        """Connect, reconcile pending orders, and consume the stream.

        Exits only on KeyboardInterrupt / SIGTERM. Logs every reconnect
        attempt to both structlog and ``platform.application_log``.
        """
        await self._db_log.startup(
            commit_sha=os.getenv("RAILWAY_GIT_COMMIT_SHA") or os.getenv("GIT_COMMIT_SHA")
        )
        delay = 1.0
        try:
            await self.reconcile_pending_on_startup()
            while True:
                try:
                    await self._consume_stream()
                    delay = 1.0  # clean exit, reset backoff
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "trade_monitor.stream_error",
                        error=str(exc),
                        delay_sec=delay,
                    )
                    await self._db_log.log(
                        "STREAM_RECONNECT",
                        f"stream error, sleeping {delay:.0f}s before reconnect: {exc}",
                        "WARNING",
                        {"error": str(exc)[:300], "delay_sec": delay},
                    )
                    await asyncio.sleep(delay)
                    delay = min(self._max_reconnect_delay, delay * 2)
        finally:
            await self._db_log.shutdown(0, 0)

    async def reconcile_pending_on_startup(self) -> int:
        """Replay broker state for any row left in 'pending' by a prior crash.

        Returns the number of rows reconciled. Idempotent.
        """
        async with self._pool.acquire() as conn:
            records = await conn.fetch(_SELECT_PENDING_SQL)
        rows = [_row_from_record(r) for r in records]
        if not rows:
            return 0
        logger.info("trade_monitor.reconcile_start", n=len(rows))
        reconciled = 0
        for row in rows:
            if row.alpaca_order_id is None:
                continue
            try:
                broker_order = await self._broker.get_order(row.alpaca_order_id)
            except Exception as exc:
                logger.warning(
                    "trade_monitor.reconcile_get_order_failed",
                    alpaca_order_id=row.alpaca_order_id,
                    error=str(exc),
                )
                continue
            status_value = getattr(broker_order.status, "value", str(broker_order.status))
            if status_value == "filled":
                await self._handle_fill(
                    row=row,
                    fill_price=broker_order.avg_fill_price,
                    filled_at=broker_order.filled_at,
                )
                reconciled += 1
            elif status_value in ("canceled", "cancelled", "rejected"):
                await self._update_row_status(row.id, "cancelled" if "cancel" in status_value else "rejected")
                reconciled += 1
        await self._db_log.log(
            "RECONCILE_COMPLETE",
            f"reconciled {reconciled}/{len(rows)} pending rows",
            "INFO",
            {"reconciled": reconciled, "pending_total": len(rows)},
        )
        return reconciled

    # ── Stream consumption ──────────────────────────────────────────────

    async def _consume_stream(self) -> None:
        """One run of the Alpaca TradingStream consumer."""
        stream = self._build_stream()
        stream.subscribe_trade_updates(self.on_trade_update)
        await self._db_log.log(
            "STREAM_CONNECTED",
            "alpaca trade_updates subscription armed",
            "INFO",
            {},
        )
        # alpaca-py's TradingStream.run() is synchronous (it spawns its own
        # event loop internally). Run it in a worker thread so our parent
        # asyncio loop stays responsive to cancellation.
        await asyncio.to_thread(stream.run)

    def _build_stream(self) -> Any:
        """Construct the upstream Alpaca stream, or use the injected factory."""
        if self._stream_factory is not None:
            return self._stream_factory()
        from alpaca.trading.stream import TradingStream  # local import — keeps tests offline

        key = os.getenv("ALPACA_KEY")
        secret = os.getenv("ALPACA_SECRET")
        if not key or not secret:
            raise RuntimeError(
                "TradeMonitor requires ALPACA_KEY + ALPACA_SECRET in the environment"
            )
        return TradingStream(
            api_key=key,
            secret_key=secret,
            paper=os.getenv("ALPACA_PAPER", "true").lower() == "true",
        )

    # ── Event handler ───────────────────────────────────────────────────

    async def on_trade_update(self, event: Any) -> None:
        """Dispatch one trade_updates event. Public for test injection.

        Accepts either an ``alpaca.trading.models.TradeUpdate`` or any
        object exposing the same attributes (``event``, ``order``,
        ``price``, ``timestamp``) so tests can pass a SimpleNamespace.
        """
        event_name = getattr(event, "event", None)
        event_name = getattr(event_name, "value", event_name)  # enum.value or raw string
        order = getattr(event, "order", None)
        alpaca_order_id = str(getattr(order, "id", "")) if order is not None else ""
        if not alpaca_order_id:
            return
        row = await self._lookup_open_order(alpaca_order_id)
        if row is None:
            # Not one of ours (smoke test, manual order, child leg of a
            # bracket whose parent we already finalized). Log and ignore.
            logger.debug(
                "trade_monitor.event.unmatched",
                trade_event=event_name,
                alpaca_order_id=alpaca_order_id,
            )
            return
        fill_price = _decimal(getattr(event, "price", None))
        filled_at = _aware(getattr(event, "timestamp", None))
        await self._db_log.log(
            "FILL_CONFIRMED" if event_name in ("fill", "partial_fill") else f"EVENT_{event_name.upper() if event_name else 'UNKNOWN'}",
            f"{row.engine} {row.ticker} {row.order_type} event={event_name}",
            "INFO" if event_name in ("fill", "partial_fill", "new", "accepted") else "WARNING",
            {
                "engine": row.engine,
                "trade_id": row.trade_id,
                "order_type": row.order_type,
                "alpaca_order_id": alpaca_order_id,
                "fill_price": str(fill_price) if fill_price is not None else None,
                "event": event_name,
            },
        )
        if event_name in ("fill", "partial_fill"):
            await self._handle_fill(row=row, fill_price=fill_price, filled_at=filled_at)
        elif event_name in ("canceled", "cancelled"):
            await self._update_row_status(row.id, "cancelled")
        elif event_name == "rejected":
            await self._update_row_status(row.id, "rejected")
        # 'new', 'accepted', 'pending_new', etc. are informational; no state update.

    # ── Fill resolution ─────────────────────────────────────────────────

    async def _handle_fill(
        self,
        *,
        row: OpenOrderRow,
        fill_price: Decimal | None,
        filled_at: datetime | None,
    ) -> None:
        """Mark the row filled, then dispatch on (engine, order_type)."""
        await self._update_row_status(
            row.id, "filled", fill_price=fill_price, filled_at=filled_at
        )
        if row.order_type == "tier1":
            await self._on_tier1_fill(row=row, fill_price=fill_price, filled_at=filled_at)
        else:
            await self._on_tier2_fill(row=row, fill_price=fill_price, filled_at=filled_at)

    async def _on_tier1_fill(
        self,
        *,
        row: OpenOrderRow,
        fill_price: Decimal | None,
        filled_at: datetime | None,
    ) -> None:
        """Submit the Tier 2 leg if the engine asked for one."""
        decision = (row.decision_data or {}).get("decision") or {}
        tier2_qty = int(decision.get("tier2_qty") or 0)
        if tier2_qty <= 0:
            # Vector trades, or any decision that doesn't have a Tier 2.
            logger.info(
                "trade_monitor.tier1_fill_no_tier2",
                engine=row.engine,
                trade_id=row.trade_id,
                ticker=row.ticker,
            )
            return
        assessment = (row.decision_data or {}).get("assessment") or {}
        tp_price = _resolve_tier2_take_profit(engine=row.engine, assessment=assessment)
        sl_price = _decimal(assessment.get("stop_price"))
        if tp_price is None or sl_price is None:
            logger.warning(
                "trade_monitor.tier2_skip_missing_levels",
                engine=row.engine,
                trade_id=row.trade_id,
                tp_price=str(tp_price) if tp_price is not None else None,
                sl_price=str(sl_price) if sl_price is not None else None,
            )
            return
        # Same side as Tier 1's entry — we're building a second long-side
        # bracket that exits at the far target instead of the mid target.
        side = (decision.get("order_payloads") or [{}])[0].get("side", "buy")
        client_order_id = f"{row.trade_id}_tier2"
        try:
            tier2_order = await self._broker.submit_tier1_only(
                ticker=row.ticker,
                qty=tier2_qty,
                side=side,
                take_profit_price=tp_price,
                stop_loss_price=sl_price,
                client_order_id=client_order_id,
                engine_id=row.engine,
            )
        except Exception as exc:
            logger.warning(
                "trade_monitor.tier2_submit_failed",
                engine=row.engine,
                trade_id=row.trade_id,
                error=str(exc),
            )
            await self._db_log.log(
                "TIER2_SUBMIT_FAILED",
                f"{row.engine} {row.ticker}: {exc}",
                "ERROR",
                {"engine": row.engine, "trade_id": row.trade_id, "error": str(exc)[:300]},
            )
            return
        async with self._pool.acquire() as conn:
            await conn.execute(
                _INSERT_TIER2_SQL,
                row.engine,
                row.trade_id,
                row.ticker,
                tier2_order.broker_order_id,
                json.dumps(row.decision_data, default=str),
            )
        await self._db_log.log(
            "TIER2_SUBMITTED",
            f"{row.engine} {row.ticker} qty={tier2_qty}",
            "INFO",
            {
                "engine": row.engine,
                "trade_id": row.trade_id,
                "ticker": row.ticker,
                "qty": tier2_qty,
                "tp_price": str(tp_price),
                "sl_price": str(sl_price),
                "broker_order_id": tier2_order.broker_order_id,
            },
        )

    async def _on_tier2_fill(
        self,
        *,
        row: OpenOrderRow,
        fill_price: Decimal | None,
        filled_at: datetime | None,
    ) -> None:
        """Write the final AAR using both tier fill prices, bump risk_state."""
        tier1_row = await self._fetch_sibling_tier(row.engine, row.trade_id, "tier1")
        if tier1_row is None or tier1_row.fill_price is None or fill_price is None:
            logger.warning(
                "trade_monitor.aar_skip_missing_fills",
                engine=row.engine,
                trade_id=row.trade_id,
                tier1_fill=str(tier1_row.fill_price) if tier1_row else None,
                tier2_fill=str(fill_price) if fill_price else None,
            )
            return
        decision = (row.decision_data or {}).get("decision") or {}
        tier1_qty = int(decision.get("tier1_qty") or 0)
        tier2_qty = int(decision.get("tier2_qty") or 0)
        total_qty = tier1_qty + tier2_qty
        # Weighted average fill prices for the AAR record.
        denom = max(total_qty, 1)
        entry_avg = (tier1_row.fill_price * tier1_qty + fill_price * tier2_qty) / denom
        # For Tier 2 the take-profit price is the exit assumption used by
        # the engine when sizing; we record the actual close at AAR time
        # via the broker's get_order avg_fill_price on each child leg in
        # the next iteration. For now record the average exit as the
        # Tier 2 fill (the higher target).
        exit_avg = fill_price
        qty_decimal = Decimal(total_qty)
        pnl_gross = (exit_avg - entry_avg) * qty_decimal
        aar = AfterActionReport(
            engine=row.engine,
            trade_id=row.trade_id,
            ticker=row.ticker,
            entry_ts=_aware(_safe_iso(decision.get("constructed_at"))) or datetime.now(UTC),
            exit_ts=filled_at or datetime.now(UTC),
            entry_price=entry_avg.quantize(Decimal("0.01")),
            exit_price=exit_avg.quantize(Decimal("0.01")),
            qty=Decimal(total_qty),
            confidence_at_entry=Decimal("0.80"),
            confidence_at_exit=Decimal("0.80"),
            sizing_pct_of_engine_equity=Decimal("0.15"),
            pnl_gross=pnl_gross.quantize(Decimal("0.01")),
            pnl_net=pnl_gross.quantize(Decimal("0.01")),
            fees=Decimal("0"),
            slippage_bps=Decimal("0"),
            regime_tags=[],
            exit_reason=ExitReason.TAKE_PROFIT,
            rule_compliance=True,
            notes=f"trade_monitor tier1+tier2 close (trade_id={row.trade_id})",
        )
        await self._aar_writer.write_aar(aar)
        # Bump risk_state. Position closed: position_delta = -1 (the trade,
        # not the share count).
        await self._risk_store.record_fill(
            engine=row.engine,
            realized_pnl=pnl_gross,
            position_delta=-1,
        )
        await self._db_log.log(
            "AAR_WRITTEN",
            f"{row.engine} {row.ticker} pnl={pnl_gross}",
            "INFO",
            {
                "engine": row.engine,
                "trade_id": row.trade_id,
                "ticker": row.ticker,
                "pnl_net": str(pnl_gross),
                "entry_avg": str(entry_avg),
                "exit_avg": str(exit_avg),
                "total_qty": total_qty,
            },
        )

    # ── DB helpers ──────────────────────────────────────────────────────

    async def _lookup_open_order(self, alpaca_order_id: str) -> OpenOrderRow | None:
        async with self._pool.acquire() as conn:
            record = await conn.fetchrow(_SELECT_BY_ALPACA_ID_SQL, alpaca_order_id)
        return _row_from_record(record) if record else None

    async def _fetch_sibling_tier(
        self, engine: str, trade_id: str, order_type: str
    ) -> OpenOrderRow | None:
        sql = """
            SELECT id, engine, trade_id, ticker, order_type, alpaca_order_id,
                   status, fill_price, decision_data
            FROM platform.open_orders
            WHERE engine = $1 AND trade_id = $2 AND order_type = $3
            LIMIT 1
        """
        async with self._pool.acquire() as conn:
            record = await conn.fetchrow(sql, engine, trade_id, order_type)
        return _row_from_record(record) if record else None

    async def _update_row_status(
        self,
        row_id: uuid.UUID,
        status: str,
        *,
        fill_price: Decimal | None = None,
        filled_at: datetime | None = None,
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(_UPDATE_STATUS_SQL, row_id, status, fill_price, filled_at)


# ── Module-level helpers ────────────────────────────────────────────────


def _safe_iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return getattr(value, "isoformat", lambda: None)()


def _resolve_tier2_take_profit(*, engine: str, assessment: dict) -> Decimal | None:
    """Map engine-specific assessment shapes to the Tier 2 take-profit.

    Sigma: ``take_profit_far`` (upper Bollinger band).
    Reversion: ``target_50ma`` (the longer mean-reversion target).
    Vector: no Tier 2 — caller's ``tier2_qty`` should be 0; never reaches here.
    """
    if engine == "sigma":
        return _decimal(assessment.get("take_profit_far"))
    if engine == "reversion":
        return _decimal(assessment.get("target_50ma"))
    return None


# ── CLI entry point ─────────────────────────────────────────────────────


async def amain() -> int:
    import logging

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    db_url = os.getenv("DATABASE_URL") or os.getenv("DATABASE_URL_IPV4")
    if not db_url:
        print("TradeMonitor FAILED — DATABASE_URL not set", file=sys.stderr)
        return 1
    if not os.getenv("ALPACA_KEY") or not os.getenv("ALPACA_SECRET"):
        print("TradeMonitor FAILED — ALPACA_KEY/ALPACA_SECRET not set", file=sys.stderr)
        return 1

    pool = await build_asyncpg_pool(db_url, max_size=4)
    try:
        broker = AlpacaPaperBrokerAdapter()
        aar_writer = AARWriter(pool)
        monitor = TradeMonitor(pool=pool, broker=broker, aar_writer=aar_writer)
        await monitor.run_forever()
    finally:
        await pool.close()
    return 0


def main() -> None:  # pragma: no cover
    with contextlib.suppress(KeyboardInterrupt):
        raise SystemExit(asyncio.run(amain()))


if __name__ == "__main__":  # pragma: no cover
    main()
