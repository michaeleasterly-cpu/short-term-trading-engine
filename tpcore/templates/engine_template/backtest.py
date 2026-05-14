"""Engine template — backtest harness.

Backtests against ``platform.prices_daily`` (survivorship-clean) using
:mod:`tpcore.backtest` primitives:

* ``tpcore.backtest.filter_diagnostics.FilterDiagnostics`` — per-gate
  pass/block counters on every SIGNAL event.
* ``tpcore.backtest.credibility.score_run`` — writes a rubric row to
  ``platform.data_quality_log`` (consumed by the capital_gate's
  ``graduation_ready`` check).
* ``tpcore.aar.AARReader`` — read-side iterator if the backtest replays
  historical AARs (e.g. for parameter search).

The output must include a DSR and a credibility score so the
graduation gate has something to read.
"""
from __future__ import annotations

import structlog

logger = structlog.get_logger(__name__)


async def run_backtest(*args, **kwargs):
    """Run the engine over a historical window. Returns a result struct."""
    raise NotImplementedError("wire run_backtest for this engine")
