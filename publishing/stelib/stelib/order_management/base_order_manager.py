"""BaseOrderManager — shared scaffolding for per-trade engine order managers.

Centralizes the three concerns that were byte-identical across
``sigma.order_manager.SigmaOrderManager``,
``reversion.order_manager.ReversionOrderManager`` and
``vector.order_manager.VectorOrderManager``:

* ``__init__`` shape (broker + governor + capital_gate + lifecycle + aar
  + optional aar_writer/parity_harness/pool, with pool falling back to
  ``aar_writer.pool`` when omitted).
* ``_persist_tier1_to_open_orders`` — inserts the row the trade monitor
  reads (`INSERT INTO platform.open_orders ... ON CONFLICT DO UPDATE`).
  Each subclass scopes the insert to its own ``ENGINE_ID``.
* ``_fetch_recent_orders`` — pulls the broker's recent-order list via
  duck-typed ``list_recent_orders``.

Each engine subclass sets the ``ENGINE_ID`` class attribute and implements
``submit_decision`` + ``reconcile`` for its own scale-out shape
(tier-cascade for sigma/reversion, flat-bracket for vector).
"""
from __future__ import annotations

import json
from typing import Any

import structlog

from stelib.aar.writer import AARWriter
from stelib.interfaces.broker import BrokerExecutionInterface, Order
from stelib.lab.context import assert_not_in_lab
from stelib.parity import LivePaperParityHarness
from stelib.risk.governor import RiskGovernor

logger = structlog.get_logger(__name__)


class BaseOrderManager:
    """Shared scaffolding. Subclasses set ``ENGINE_ID`` and add submit/reconcile."""

    ENGINE_ID: str  # subclasses MUST set this; used by persistence + logging.

    def __init__(
        self,
        *,
        broker: BrokerExecutionInterface,
        governor: RiskGovernor,
        capital_gate: Any,
        lifecycle: Any,
        aar: Any,
        aar_writer: AARWriter | None = None,
        parity_harness: LivePaperParityHarness | None = None,
        pool: Any | None = None,
    ) -> None:
        assert_not_in_lab()
        self._broker = broker
        self._governor = governor
        self._capital_gate = capital_gate
        self._lifecycle = lifecycle
        self._aar = aar
        self._aar_writer = aar_writer
        self._parity = parity_harness
        # DB driver pool for platform.open_orders persistence — required for
        # the trade monitor to find Tier 1 rows. None falls back to the
        # aar_writer's pool when available; tests can pass None to skip.
        self._pool = pool or (aar_writer.pool if aar_writer is not None else None)

    async def _persist_tier1_to_open_orders(
        self,
        *,
        tier1_order: Order,
        trade_key: str,
        decision: Any,
        assessment: Any,
    ) -> None:
        """Insert the Tier 1 row that the trade monitor will react to.

        Idempotent on ``(engine, trade_id, order_type)`` — re-running a
        submission overwrites the broker_order_id but keeps the same row,
        so the monitor sees the latest broker ack.
        """
        if self._pool is None:
            logger.warning(
                f"{self.ENGINE_ID}.order_manager.no_pool_persistence_skipped",
                trade_key=trade_key,
                broker_order_id=tier1_order.broker_order_id,
            )
            return
        payload = json.dumps(
            {
                "decision": decision.model_dump(mode="json"),
                "assessment": assessment.model_dump(mode="json"),
            },
            default=str,
        )
        sql = """
            INSERT INTO platform.open_orders
                (engine, trade_id, ticker, order_type,
                 broker_order_id, status, decision_data)
            VALUES ($1, $2, $3, 'tier1', $4, 'pending', $5::jsonb)
            ON CONFLICT (engine, trade_id, order_type)
            DO UPDATE SET
                broker_order_id = EXCLUDED.broker_order_id,
                decision_data = EXCLUDED.decision_data,
                updated_at = now()
        """
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    sql,
                    self.ENGINE_ID,
                    trade_key,
                    decision.ticker,
                    tier1_order.broker_order_id,
                    payload,
                )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                f"{self.ENGINE_ID}.order_manager.open_orders_persist_failed",
                trade_key=trade_key,
                error=str(exc),
            )

    async def _fetch_recent_orders(self) -> list[Order]:
        """Return Orders the broker knows about, both open and recently closed.

        ``BrokerExecutionInterface`` doesn't have a ``list_recent_orders``
        method today, so we duck-type via ``getattr`` to opt in when the
        adapter exposes one (Alpaca paper/live both do).
        """
        list_fn = getattr(self._broker, "list_recent_orders", None)
        if list_fn is None:
            return []
        return await list_fn()


__all__ = ["BaseOrderManager"]
