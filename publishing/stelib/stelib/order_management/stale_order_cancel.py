"""Shared stale-order cancellation for batch engines (momentum / sentinel).

Lean P5 #1 (LIVE-MONEY). Consolidates the byte-identical
``_cancel_stale_*_orders`` previously copy-pasted into
``momentum/scheduler.py`` and ``sentinel/scheduler.py``. The ONLY divergence
between the two engines was the structlog event namespace and the
client-order-id prefix — both lifted to explicit parameters here. No engine
imports (one-way engine→tpcore layering).

**This cancels real broker orders.** Behavior — the set of cancelled
broker-order IDs, the return count, and the emitted structlog event NAMES —
is byte-equivalent to the prior per-engine implementations and is pinned by
``tpcore/tests/test_stale_order_cancel.py``.
"""

from __future__ import annotations

import structlog

logger = structlog.get_logger(__name__)

# Open statuses worth cancelling: NEW / PARTIALLY_FILLED (cancel cancels the
# remainder) / ACCEPTED / PENDING_NEW. Already-filled / cancelled / rejected
# are terminal and left alone.
_OPEN_STATUSES = {"new", "partially_filled", "accepted", "pending_new"}


async def cancel_stale_orders(
    broker,  # noqa: ANN001 — tpcore must not import the engine broker type
    *,
    order_prefix: str,
    log_namespace: str,
) -> int:
    """Cancel any open orders whose ``client_order_id`` starts with
    ``order_prefix`` so positions held_for_orders are released before the new
    rebalance.

    Returns the number of orders cancelled. Silently degrades when the broker
    doesn't expose ``list_recent_orders`` (non-Alpaca brokers).
    """
    list_fn = getattr(broker, "list_recent_orders", None)
    if list_fn is None:
        return 0
    try:
        recent = await list_fn(limit=500)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"{log_namespace}.list_orders_failed", error=str(exc)[:200])
        return 0
    cancelled = 0
    for o in recent:
        cid = (o.client_order_id or "").lower()
        if not cid.startswith(order_prefix):
            continue
        status_val = getattr(o.status, "value", str(o.status)).lower()
        if status_val not in _OPEN_STATUSES:
            continue
        if not o.broker_order_id:
            continue
        try:
            await broker.cancel_order(o.broker_order_id)
            cancelled += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"{log_namespace}.cancel_failed",
                broker_order_id=o.broker_order_id,
                client_order_id=o.client_order_id,
                error=str(exc)[:200],
            )
    if cancelled:
        logger.info(f"{log_namespace}.stale_orders_cancelled", n=cancelled)
    return cancelled
