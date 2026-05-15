"""Engine template — Scheduler entry point.

Composes the 5 plugs + order_manager and exposes ``run_once`` so the
:mod:`ops.engine_service` daemon can invoke the engine on the
``DAILY_SCAN_COMPLETE`` trigger.

For per-trade engines (sigma/reversion/vector), ``run_once`` typically:

    1. detect setups via Plug 1
    2. for each setup, assess phase via Plug 2
    3. decide on execution via Plug 3
    4. submit + reconcile via the OrderManager

For batch engines (momentum), ``run_once`` only fires on the rebalance
cadence (e.g. first session of the month) and submits N market orders.

Compliance contract (STYLE_GUIDE.md "Engine plug compliance"):

* :func:`tpcore.calendar.is_trading_day` check FIRST — no DB / order work
  on weekends or market holidays.
* ``_cancel_stale_<engine>_orders`` helper called before submitting new
  orders — mirror ``MomentumScheduler._cancel_stale_momentum_orders``
  with this engine's ``tpcore.order_ids.ENGINE_PREFIX`` value.
* When emitting SIGNAL events via ``DBLogHandler.signal(...)``, pass
  ``extra_data={"filter_diagnostics": diag.model_dump(exclude_none=True)}``
  so the dashboard can render per-gate pass/block counters.
"""
from __future__ import annotations

from datetime import UTC, datetime
from datetime import date as date_t

import structlog

from tpcore.calendar import is_trading_day

logger = structlog.get_logger(__name__)


async def run_once(as_of: date_t | None = None, *args, **kwargs) -> dict:
    """Single-pass run of the engine. Idempotent — safe to re-invoke.

    Skeleton wires the mandatory ``is_trading_day`` early-return; the
    body below is where engine-specific orchestration lives.
    """
    as_of = as_of or datetime.now(UTC).date()
    as_of_dt = datetime.combine(as_of, datetime.min.time(), tzinfo=UTC)
    if not is_trading_day(as_of_dt):
        logger.info("ENGINE_NAME.scheduler.non_trading_day", as_of=as_of.isoformat())
        return {"as_of": as_of.isoformat(), "action": "non_trading_day"}
    raise NotImplementedError("wire run_once for this engine")


async def _cancel_stale_ENGINE_NAME_orders(broker) -> int:
    """Cancel any of this engine's still-open broker orders.

    Mirror ``MomentumScheduler._cancel_stale_momentum_orders``: list
    recent orders, filter by ``tpcore.order_ids.ENGINE_PREFIX["ENGINE_NAME"]``,
    cancel anything in ``{new, partially_filled, accepted, pending_new}``.
    Skipping this leaves positions ``held_for_orders`` and the next
    rebalance's sells get rejected.
    """
    raise NotImplementedError("wire _cancel_stale_ENGINE_NAME_orders")
