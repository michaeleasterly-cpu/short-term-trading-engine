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
"""
from __future__ import annotations

import structlog

logger = structlog.get_logger(__name__)


async def run_once(*args, **kwargs) -> None:
    """Single-pass run of the engine. Idempotent — safe to re-invoke."""
    raise NotImplementedError("wire run_once for this engine")
