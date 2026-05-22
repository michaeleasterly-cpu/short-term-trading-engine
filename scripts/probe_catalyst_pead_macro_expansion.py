"""Re-probe ``catalyst_pead_expansion_range`` under the
``beat_30d_only_macro_expansion`` arm (PR B 2026-05-22) — PEAD +
per-event macro-regime gate.

Context (operator brief 2026-05-22):

The 2026-05-22 ``catalyst_pead_expansion_range`` probe under
``beat_30d_only`` on T1+T2 universe FAILED with Sharpe +0.44, PF 1.11,
757 trades, MaxDD -69.7%. The hypothesis IS real but anaemic — the
profit factor barely clears 1.0 and the drawdown is catastrophic.

PR A loosened the paper-promote criteria from accidentally-LIVE-grade
to actual paper-trade-and-learn calibration (MIN_TRADE_COUNT 10→30,
MIN_MAX_DRAWDOWN -0.50→-0.75, MIN_PROFIT_FACTOR 1.0→1.05, NEW
MIN_CALMAR_RATIO 0.30). Under those criteria the prior PEAD probe
STILL fails on Calmar (0.44 × 0.20 / 0.697 = 0.126 < 0.30).

PR B added ``beat_30d_only_macro_expansion``: gate per-event firing
on macro_regime='expansion'. Hypothesis (regime-aware-trading §2.3):
PEAD's strongest edge is in expansion regimes; conditioning entries
should LIFT Sharpe AND REDUCE drawdown.

What this script does:

* Temporarily narrows
  ``catalyst.backtest.LAB_TARGET.param_ranges`` to pin
  ``event_confirmation_mode='beat_30d_only_macro_expansion'``.
  ``cluster_window_days`` is dropped from the search space (the
  PEAD branch bypasses the cluster loop). ``hold_days`` stays
  Lab-sampled over [5, 30].
* Wraps ``LAB_TARGET.load_window_context`` + ``run_for_search`` so
  every backtest call attaches a ``regime_bundle`` (SPY-close +
  macro_indicators + AAII) loaded from the same Postgres pool the
  reversion partial-axis probe uses.
* Invokes the same ``ops.lab.amain`` path the prior PEAD probe used,
  in-process, so the SP-A ledger spend + walk-forward + final-holdout
  + dossier write are all byte-equivalent to the canonical Lab
  harness.
* RESTORES ``LAB_TARGET.param_ranges`` + loader + run_for_search in
  a ``finally:`` block before exit so no probe-time narrowing leaks
  into the in-tree LAB_TARGET (the reversion + catalyst precedent).

Output: the Lab dossier path printed by ``ops.lab.amain``; the
dossier + JSON live in ``docs/lab/``. The verdict (DSR /
credibility / n_trades / PBO / improvement over incumbent) is
extracted from the dossier post-run.

Usage:
    .venv/bin/python scripts/probe_catalyst_pead_macro_expansion.py

Environment: requires ``$DATABASE_URL`` (or the .env file with
``DATABASE_URL_IPV4``). The wrapper shell at
``scripts/run_probe_catalyst_pead_macro_expansion.sh`` sets these
correctly + uses the transaction-mode Postgres pool (port 6543) to
avoid the 15-client session-mode cap on long Lab runs.
"""
from __future__ import annotations

import asyncio
import os
import sys

# ────────────────────────────────────────────────────────────────────────
# Ops-package-shadow guard (engine_readiness / tests-and-ci rule).
#
# Running `python scripts/foo.py` automatically prepends `scripts/` to
# sys.path, which makes `scripts/ops.py` shadow the `ops/` package
# (`ModuleNotFoundError: 'ops' is not a package` on `from ops.lab ...`).
# Remove the `scripts/` shadow + ensure the repo root is path[0] BEFORE
# any `import ops.*` happens (mirrors the catalyst + reversion probe
# precedents).
# ────────────────────────────────────────────────────────────────────────
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR in sys.path:
    sys.path.remove(_SCRIPTS_DIR)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


CANDIDATE_NAME = "catalyst_pead_expansion_range"
PINNED_MODE = "beat_30d_only_macro_expansion"


def _build_argv() -> list[str]:
    """Argv mirroring the prior 2026-05-22 catalyst PEAD probe.

    Same trials / seed / window dates as the prior failed probe so
    the comparison is apples-to-apples — only the engine surface
    (the macro-expansion arm) changes.
    """
    return [
        "--candidate", CANDIDATE_NAME,
        "--target-engine", "catalyst",
        "--intent", "fold_existing",
        # The narrowed search space pins event_confirmation_mode to
        # beat_30d_only_macro_expansion + samples hold_days over
        # [5, 30] (26 unique values). 30 trials covers the space
        # well; per-window-trials=10 keeps each window's evaluation
        # reasonable. Trial budget mirrors the prior PEAD probe so
        # the SP-A ledger comparison is apples-to-apples.
        "--trials", "30",
        "--per-window-trials", "10",
        "--seed", "20260522",
        # Walk-forward windows match the prior probe.
        "--train-start", "2018-01-01",
        "--holdout-end", "2023-12-31",
        "--final-holdout-start", "2024-01-01",
        "--final-holdout-end", "2025-12-31",
        "--train-years", "3",
        "--holdout-years", "1",
        # T1 production universe — operator brief explicit
        # (--universe-tier-max=1). The prior PEAD probe was T1+T2;
        # this re-probe is T1 to test whether the macro-expansion
        # gate produces a viable engine on the smaller (cleaner)
        # liquid universe.
        "--universe-tier-max", "1",
        "--notes",
        "Engine surface enrichment re-probe (PEAD + macro-expansion): "
        "event_confirmation_mode pinned to "
        "beat_30d_only_macro_expansion via temporary "
        "LAB_TARGET.param_ranges narrowing; hold_days Lab-sampled "
        "5..30; cluster_window_days dropped from search; "
        "universe-tier-max=1. Hypothesis: PEAD's edge concentrates in "
        "expansion regimes; conditioning lifts Sharpe + reduces "
        "drawdown vs the unconditional beat_30d_only T1+T2 probe "
        "(Sharpe +0.44 / PF 1.11 / 757 trades / MaxDD -69.7%).",
    ]


async def load_regime_bundle_from_pool(pool, start, end):
    """Read the regime substrates from Postgres for [start, end].

    Mirrors ``scripts/probe_reversion_partial_axis.load_regime_bundle_
    from_pool`` exactly — the macro-regime classifier in
    ``reversion.regime_filter`` consumes the same SPY/VIX/Sahm/CFNAI/
    yield-curve/AAII substrates. Lives in this probe driver (NOT in
    ``catalyst/`` or ``reversion/``) by design — the engine-data-
    dependencies drift clockwork scans every .py file under each
    PAPER/LIVE engine package for ``platform.<table>`` strings, and
    the macro_indicators / aaii_sentiment reads here would otherwise
    demand an ECR-MODIFY on catalyst's data_dependencies.
    """
    import pandas as pd

    from reversion.regime_filter import RegimeBundle

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT date, close
              FROM platform.prices_daily
             WHERE ticker = 'SPY' AND date BETWEEN $1 AND $2
             ORDER BY date
            """,
            start, end,
        )
        spy = pd.Series(
            {pd.Timestamp(r["date"]): float(r["close"]) for r in rows}
        )

        macro_rows = await conn.fetch(
            """
            SELECT indicator, date, value
              FROM platform.macro_indicators
             WHERE indicator IN ('vix', 'sahm_rule', 'cfnai_ma3', 'yield_curve')
               AND date BETWEEN $1 AND $2
             ORDER BY indicator, date
            """,
            start, end,
        )
        by_ind: dict[str, dict] = {
            "vix": {}, "sahm_rule": {}, "cfnai_ma3": {}, "yield_curve": {},
        }
        for r in macro_rows:
            by_ind[r["indicator"]][pd.Timestamp(r["date"])] = float(r["value"])
        vix = pd.Series(by_ind["vix"])
        sahm = pd.Series(by_ind["sahm_rule"])
        cfnai = pd.Series(by_ind["cfnai_ma3"])
        yc = pd.Series(by_ind["yield_curve"])

        aaii_rows = await conn.fetch(
            """
            SELECT date, bullish_pct, bearish_pct
              FROM platform.aaii_sentiment
             WHERE date BETWEEN $1 AND $2
             ORDER BY date
            """,
            start, end,
        )
        import pandas as pd
        aaii = pd.DataFrame(
            [
                {
                    "date": pd.Timestamp(r["date"]),
                    "bullish_pct": (
                        float(r["bullish_pct"])
                        if r["bullish_pct"] is not None else None
                    ),
                    "bearish_pct": (
                        float(r["bearish_pct"])
                        if r["bearish_pct"] is not None else None
                    ),
                }
                for r in aaii_rows
            ]
        )
        if not aaii.empty:
            aaii = aaii.set_index("date").sort_index()

    return RegimeBundle(
        spy_close=spy.sort_index(),
        vix=vix.sort_index(),
        sahm=sahm.sort_index(),
        cfnai_ma3=cfnai.sort_index(),
        yield_curve=yc.sort_index(),
        aaii=aaii,
    )


async def _amain() -> int:
    """In-process Lab probe with probe-time LAB_TARGET narrowing +
    regime_bundle attach.
    """
    import catalyst.backtest as catalyst_bt
    from ops.lab.__main__ import _amain as ops_lab_amain
    from tpcore.db import build_asyncpg_pool

    target = catalyst_bt.LAB_TARGET
    original_ranges = dict(target.param_ranges)
    original_loader = target.load_window_context
    original_run_for_search = target.run_for_search

    # The narrowed search space:
    # - event_confirmation_mode is PINNED to beat_30d_only_macro_expansion
    #   (a single-value choice satisfies LabTarget's validator).
    # - cluster_window_days is DROPPED — the PEAD branch bypasses the
    #   cluster loop entirely (pure waste of trials).
    # - hold_days stays Lab-sampled int over [5, 30] — the hypothesis
    #   recommends 20 but the sampler tells us the per-window OOS
    #   optimum.
    narrowed_ranges: dict[str, tuple] = {
        "event_confirmation_mode": (
            0, 0, f"choice:{PINNED_MODE}",
        ),
        "hold_days": (5, 30, "int"),
    }
    print(
        "\n[probe_catalyst_pead_macro_expansion] narrowing "
        "catalyst.backtest.LAB_TARGET.param_ranges:"
    )
    print(f"  before: {original_ranges}")
    print(f"   after: {narrowed_ranges}")

    async def _loader_with_regime(**kwargs):
        """Wrap the loader: load the standard catalyst context AND
        attach a regime_bundle for [ctx.start, ctx.end]."""
        ctx = await original_loader(**kwargs)
        db_url = kwargs.get("db_url") or os.environ["DATABASE_URL"]
        pool = await build_asyncpg_pool(db_url)
        try:
            bundle = await load_regime_bundle_from_pool(pool, ctx.start, ctx.end)
        finally:
            await pool.close()
        # Mutate the dataclass field — CatalystWindowContext is a plain
        # dataclass, not frozen (matches the reversion precedent).
        ctx.regime_bundle = bundle
        return ctx

    async def _run_for_search_with_regime(**kwargs):
        """Mirror of the loader wrap for the chunked final-holdout
        replay path (``ops.lab.run._run_final_holdout_chunked`` calls
        ``_runner_for`` which returns ``LAB_TARGET.run_for_search`` —
        NOT the loader; the loader wrap above doesn't catch this path).
        Replicates ``catalyst.backtest.run_for_search``'s body but
        attaches the regime_bundle before calling
        ``run_catalyst_with_context``.

        Mirrors the reversion partial-axis probe's pattern (the prior
        precedent for the same chunked-replay gap).
        """
        from catalyst.backtest import (
            load_catalyst_window_context,
            run_catalyst_with_context,
        )
        ctx_kwargs = {
            k: v for k, v in kwargs.items()
            if k in ("db_url", "start", "end", "universe")
        }
        ctx = await load_catalyst_window_context(**ctx_kwargs)
        db_url = kwargs.get("db_url") or os.environ["DATABASE_URL"]
        pool = await build_asyncpg_pool(db_url)
        try:
            bundle = await load_regime_bundle_from_pool(pool, ctx.start, ctx.end)
        finally:
            await pool.close()
        ctx.regime_bundle = bundle
        return run_catalyst_with_context(
            ctx,
            overrides=kwargs.get("overrides"),
            trade_log_path=kwargs.get("trade_log_path"),
        )

    object.__setattr__(target, "param_ranges", narrowed_ranges)
    object.__setattr__(target, "load_window_context", _loader_with_regime)
    object.__setattr__(target, "run_for_search", _run_for_search_with_regime)
    try:
        rc = await ops_lab_amain(_build_argv())
    finally:
        # Restore the in-source LAB_TARGET so no probe-time narrowing
        # leaks into the in-tree state.
        object.__setattr__(target, "param_ranges", original_ranges)
        object.__setattr__(target, "load_window_context", original_loader)
        object.__setattr__(target, "run_for_search", original_run_for_search)
        print(
            "[probe_catalyst_pead_macro_expansion] restored "
            "catalyst.backtest.LAB_TARGET.param_ranges:"
        )
        print(f"  {dict(target.param_ranges)}")
    return rc


def main() -> None:
    if not os.environ.get("DATABASE_URL"):
        print(
            "DATABASE_URL not set. Use the wrapper "
            "scripts/run_probe_catalyst_pead_macro_expansion.sh which "
            "sources .env and exports DATABASE_URL_IPV4 → DATABASE_URL.",
            file=sys.stderr,
        )
        sys.exit(2)
    sys.exit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
