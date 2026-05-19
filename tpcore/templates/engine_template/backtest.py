"""Engine template — backtest harness.

Backtests against ``platform.prices_daily`` (survivorship-clean) using
:mod:`tpcore.backtest` primitives:

* ``tpcore.backtest.filter_diagnostics.FilterDiagnostics`` — per-gate
  pass/block counters on every SIGNAL event.
* ``tpcore.backtest.search.compute_search_metrics`` — bundles
  OverfittingDiagnostic + credibility rubric + DSR into a
  :class:`BacktestRunResult`.
* ``tpcore.backtest.statistical_validation.write_credibility_score`` —
  **mandatory** — persists the rubric row to ``platform.data_quality_log``
  so the capital gate's ``graduation_ready`` check has something to
  read. Without this call the engine can never graduate regardless of
  trade performance.
* ``tpcore.aar.AARReader`` — read-side iterator if the backtest replays
  historical AARs (e.g. for parameter search).

Compliance contract (STYLE_GUIDE.md "Engine plug compliance"): every
backtest run must end with a ``write_credibility_score`` call. The
canonical pattern (see ``reversion/backtest.py`` and
``sentinel/backtest.py``) is::

    from tpcore.backtest.statistical_validation import write_credibility_score
    result = compute_search_metrics(...)
    await write_credibility_score(
        pool, engine_name="ENGINE_NAME", score=result.credibility_rubric,
    )
"""
from __future__ import annotations

import structlog

from tpcore.backtest.statistical_validation import write_credibility_score  # noqa: F401

logger = structlog.get_logger(__name__)


async def run_backtest(*args, **kwargs):
    """Run the engine over a historical window. Returns a result struct.

    Implementations must:
      1. compute trades + Sharpe + PF + max_dd over the window;
      2. call ``compute_search_metrics`` to bundle the credibility rubric;
      3. call ``write_credibility_score(pool, engine_name=..., score=result.credibility_rubric)``
         before the function returns.
    """
    raise NotImplementedError("wire run_backtest for this engine")


# ────────────────────────────────────────────────────────────────────────────
# SP-B forward dep — Lab targeting declaration. Uncomment + fill the
# param ranges once this engine has a backtest contract; the four
# callables below are the uniform Lab dispatch contract every engine
# already implements (run_for_search / load_<engine>_window_context /
# run_<engine>_with_context / default_params). Resolved lazily by
# ops.lab.run._lab_target_for via the roster SoT — being added to
# tpcore.engine_profile._PROFILE (PAPER/LAB/LIVE) + declaring this
# constant is ALL that is needed to be Lab-targetable (spec §7 T7).
# ────────────────────────────────────────────────────────────────────────────
#
# from tpcore.lab.target import LabTarget
#
# LAB_TARGET = LabTarget(
#     param_ranges={
#         # "my_param": (low, high, "float"),   # "float" | "int" | "choice:a,b"
#     },
#     run_for_search=run_for_search,
#     load_window_context=load_window_context,
#     run_with_context=run_with_context,
#     default_params=default_params,
# )
