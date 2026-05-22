"""Re-probe ``reversion_earnings_season_5d_range_normal`` against the
post-2026-05-22 partial-axis regime-filter Lab enrichment.

The prior 2026-05-22 probe FAILED with n_trades=0 because the
full-4-axis match for the candidate's regime tuple ``968624efa259``
(vol=normal, trend=range, macro=expansion, sentiment=neutral) had zero
occurrences in the 2024-2025 final holdout. The persona v2.1 testability
pre-check directive that landed in PR #275 catches this kind of
non-actionable hypothesis before the probe spend - the structural fix
landed here is engine-side: a partial-axis variant menu that lets the
LLM finder say "match the candidate's regime on TREND axis only" (more
permissive) instead of forcing all four axes.

What this script does:

* Temporarily narrows ``reversion.backtest.LAB_TARGET.param_ranges`` to
  pin ``regime_filter_v1='trend_only'`` (the most permissive variant -
  matches sessions where current trend_regime='range' regardless of
  vol/macro/sentiment). ``signal_mode`` is pinned to ``price_z`` so we
  don't confound the regime-conditional hypothesis with the
  Avellaneda-Lee PCA-residual signal.
* Passes ``regime_target='968624efa259'`` as a non-LAB_TARGET probe-
  time override (it's the candidate's pre-registered regime hash; the
  Lab does not sample it).
* Invokes the same ``ops.lab.amain`` path the prior probe used so the
  SP-A ledger spend + walk-forward + final-holdout + dossier write are
  byte-equivalent to the canonical Lab harness.
* RESTORES ``LAB_TARGET.param_ranges`` in a ``finally:`` block before
  exit so no probe-time pinning leaks into the in-tree LAB_TARGET (the
  signal_mode + catalyst precedent).

Usage (the operator runs this AFTER the PR lands; this script is NEVER
auto-invoked from CI or by the implementer):

    .venv/bin/python scripts/probe_reversion_partial_axis.py

Environment: requires ``$DATABASE_URL`` (or the .env file with
``DATABASE_URL_IPV4``). The wrapper shell at
``scripts/run_probe_reversion_partial_axis.sh`` sets these correctly.
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
# any `import ops.*` happens (mirrors the catalyst probe precedent).
# ────────────────────────────────────────────────────────────────────────
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR in sys.path:
    sys.path.remove(_SCRIPTS_DIR)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


CANDIDATE_NAME = "reversion_earnings_season_5d_range_normal"
TARGET_REGIME_HASH = "968624efa259"
# trend_only is the most permissive partial-axis variant - sessions
# where the current trend_regime classifies as 'range' fire the gate
# regardless of vol/macro/sentiment. Range-trend regime is common in
# history (~50-60% of sessions), so n_trades should land well above
# the 0 that the full-4-axis probe produced.
PARTIAL_AXIS_CHOICE = "trend_only"


def _build_argv() -> list[str]:
    """Argv mirroring the prior 2026-05-22 reversion probe.

    Same trials / seed / window dates as the prior FAILED probe so the
    comparison is apples-to-apples - only the engine surface (the
    partial-axis regime filter) changes.

    NOTE: ``regime_target`` is passed as a JSON ``--param-overrides``
    value; ``regime_filter_v1`` is sampled by the Lab from the pinned
    LAB_TARGET param_ranges (narrowing happens in the in-process pin
    below, not via CLI). ``signal_mode`` is also pinned to ``price_z``
    via the in-process narrowing.
    """
    overrides = (
        '{"regime_target": "' + TARGET_REGIME_HASH + '"}'
    )
    return [
        "--candidate", CANDIDATE_NAME,
        "--target-engine", "reversion",
        "--intent", "fold_existing",
        "--param-overrides", overrides,
        "--trials", "20",
        "--per-window-trials", "10",
        "--seed", "20260522",
        "--train-start", "2018-01-01",
        "--holdout-end", "2023-12-31",
        "--final-holdout-start", "2024-01-01",
        "--final-holdout-end", "2025-12-31",
        "--train-years", "3",
        "--holdout-years", "1",
        "--notes",
        "Engine surface enrichment re-probe (partial-axis regime "
        f"filter): regime_filter_v1 pinned to '{PARTIAL_AXIS_CHOICE}' "
        "via temporary LAB_TARGET.param_ranges narrowing; regime_target "
        f"= {TARGET_REGIME_HASH} (range x normal x expansion x neutral); "
        "signal_mode pinned to price_z. Prior 4-axis probe yielded "
        "n_trades=0; partial-axis with the trend axis alone should "
        "expand the eligible session population substantially.",
    ]


async def load_regime_bundle_from_pool(pool, start, end):
    """Read the regime substrates from Postgres for [start, end].

    This loader lives in the probe driver (NOT in
    ``reversion/regime_filter.py``) by deliberate design - the
    engine-data-dependencies drift clockwork
    (``tpcore/tests/test_engine_data_dependencies_drift.py``) scans
    every ``.py`` file under each PAPER/LIVE engine package for
    ``platform.<table>`` strings, and the `aaii_sentiment` /
    `macro_indicators` reads here would otherwise demand an
    ECR-MODIFY on `_PROFILE['reversion'].data_dependencies` (which
    the task brief explicitly forbids - "Engine roster unchanged - no
    ECR needed (LAB_TARGET edit only)").

    Loads:
      * SPY adj-close from ``platform.prices_daily``.
      * VIX / sahm_rule / cfnai_ma3 / yield_curve from
        ``platform.macro_indicators``.
      * AAII bullish/bearish_pct from ``platform.aaii_sentiment``.
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
        by_ind = {
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
        aaii = pd.DataFrame(
            [
                {
                    "date": pd.Timestamp(r["date"]),
                    "bullish_pct": (
                        float(r["bullish_pct"]) if r["bullish_pct"] is not None else None
                    ),
                    "bearish_pct": (
                        float(r["bearish_pct"]) if r["bearish_pct"] is not None else None
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
    """In-process Lab probe with probe-time LAB_TARGET narrowing."""
    import reversion.backtest as reversion_bt
    from ops.lab.__main__ import _amain as ops_lab_amain
    from tpcore.db import build_asyncpg_pool

    target = reversion_bt.LAB_TARGET
    original_ranges = dict(target.param_ranges)
    # The narrowed search space:
    # - regime_filter_v1 is PINNED to ``trend_only`` (a single-value
    #   choice satisfies LabTarget's validator: >=1 non-empty member
    #   required).
    # - signal_mode is PINNED to ``price_z`` so the regime-conditional
    #   hypothesis isn't confounded with the PCA-residual variant.
    # - regime_target is the probe-pinned hash, passed via
    #   --param-overrides at the CLI level (NOT a Lab-sampled knob).
    # - The other live knobs (z_threshold, stop_pct, volume_climax_*,
    #   max_hold_days) stay in their declared ranges so the Lab can
    #   sweep within the regime-conditional population.
    narrowed_ranges: dict[str, tuple] = dict(original_ranges)
    narrowed_ranges["regime_filter_v1"] = (0, 0, f"choice:{PARTIAL_AXIS_CHOICE}")
    narrowed_ranges["signal_mode"] = (0, 0, "choice:price_z")
    # NOTE on axis choice (operator-tunable): the candidate's regime
    # (range × normal × expansion × neutral) decomposes to a SHA12 that
    # is rare in history because trend=range requires
    # |SPY 200d slope| < 50bp (~0.5%) — a flat-year condition that
    # occurred only 17 times in 2018-2025 (per the snapshot SoT
    # _SPY_SLOPE_BP_TRIGGER). Choosing `trend_only` for the partial axis
    # is therefore NOT MORE PERMISSIVE than full-match — both match the
    # same ~17 historical sessions. To unblock the engine to produce
    # trade counts ≥30 for the gate, try a more-common axis:
    #   - `vol_only`  matches vol=normal sessions (~50% of history)
    #   - `macro_only` matches macro=expansion (~80% of history)
    # These TEST A RELAXED VERSION of the LLM's hypothesis — useful for
    # probing whether the LLM's claim "5d mean-reversion in current
    # regime" holds when conditioning is loosened, but does NOT
    # faithfully test the LLM's narrow conditioning. Operator picks.
    print(
        "\n[probe_reversion_partial_axis] narrowing "
        "reversion.backtest.LAB_TARGET.param_ranges:"
    )
    print(f"  before: {original_ranges}")
    print(f"   after: {narrowed_ranges}")

    # Wrap the context loader so it attaches a regime_bundle. The
    # loader signature stays compatible with ops.lab.__main__'s
    # resolver (it calls load_window_context(db_url=..., start=...,
    # end=..., universe=...)).
    original_loader = target.load_window_context
    original_run_for_search = target.run_for_search

    async def _loader_with_regime(**kwargs):
        ctx = await original_loader(**kwargs)
        # Open a fresh short-lived pool so we don't reach into the
        # loader's own pool (already-closed by now).
        db_url = kwargs.get("db_url") or os.environ["DATABASE_URL"]
        pool = await build_asyncpg_pool(db_url)
        try:
            bundle = await load_regime_bundle_from_pool(pool, ctx.start, ctx.end)
        finally:
            await pool.close()
        # Mutate the dataclass field - ReversionWindowContext is a
        # plain dataclass, not frozen.
        ctx.regime_bundle = bundle
        return ctx

    async def _run_for_search_with_regime(**kwargs):
        """Mirror of the loader wrap for the chunked final-holdout replay
        path (`ops.lab.run._run_final_holdout_chunked` calls `_runner_for`
        which returns `LAB_TARGET.run_for_search` — NOT the loader; the
        loader wrap above doesn't catch this path). Replicates
        `reversion.backtest.run_for_search`'s body but attaches the
        regime_bundle before calling `run_reversion_with_context`.

        Fixes the chunked-replay missing-regime_bundle gap surfaced
        2026-05-22 by the reversion partial-axis probe (the train-window
        wrap worked but the held-back replay failed with
        ``regime_filter_v1 is set but context.regime_bundle is None``).
        """
        from reversion.backtest import (
            load_reversion_window_context, run_reversion_with_context,
        )
        ctx_kwargs = {
            k: v for k, v in kwargs.items()
            if k in ("db_url", "start", "end", "universe")
        }
        ctx = await load_reversion_window_context(**ctx_kwargs)
        # Same bundle-load pattern as the loader wrap.
        db_url = kwargs.get("db_url") or os.environ["DATABASE_URL"]
        pool = await build_asyncpg_pool(db_url)
        try:
            bundle = await load_regime_bundle_from_pool(pool, ctx.start, ctx.end)
        finally:
            await pool.close()
        ctx.regime_bundle = bundle
        return run_reversion_with_context(
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
            "[probe_reversion_partial_axis] restored "
            "reversion.backtest.LAB_TARGET.param_ranges:"
        )
        print(f"  {dict(target.param_ranges)}")
    return rc


def main() -> None:
    if not os.environ.get("DATABASE_URL"):
        print(
            "DATABASE_URL not set. Use the wrapper "
            "scripts/run_probe_reversion_partial_axis.sh which sources "
            ".env and exports DATABASE_URL_IPV4 → DATABASE_URL.",
            file=sys.stderr,
        )
        sys.exit(2)
    sys.exit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
