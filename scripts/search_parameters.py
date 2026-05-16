"""Production parameter-search pipeline.

Random search + walk-forward validation + final held-back DSR verdict.
Replaces manual one-off backtesting with a systematic, statistically-rigorous
edge-discovery loop.

Architecture
------------
1. Sample ``--trials`` (default 200) parameter combinations from the engine's
   ranges using uniform / log-uniform sampling. Each combination is a dict.
2. Build walk-forward windows: 5-year train + 2-year holdout, advancing
   ``--walk-forward-step`` (default 365) days at a time. The window slate is
   bounded by ``--train-start`` / ``--holdout-end``.
3. For each window, randomly sample 50 of the 200 combinations to evaluate.
   Each candidate is run end-to-end via the engine's ``run_for_search`` and
   the resulting trade-log is sliced to the window's holdout dates. Simple
   OOS metrics (Sharpe, n_trades, drawdown) come off the slice — no extra
   backtests required.
4. Rank candidates by mean OOS credibility (Sharpe-based proxy); tiebreak on
   raw Sharpe. The winner is the candidate with the best average OOS metric
   across the windows where it was evaluated.
5. Final held-back: take the winner, run the engine over
   ``[train_start, final_holdout_end]``, slice to the final-holdout window,
   and report the DSR (deflated for the total ``--trials`` correction). The
   verdict is SURVIVED if DSR ≥ 0.95 AND credibility ≥ 60; otherwise FAILED
   and the top-5 ranked candidates are printed.

The orchestrator never imports the engine's CLI — it imports each engine's
``run_for_search`` directly so there's no subprocess overhead and no stdout
parsing.

Usage
-----
::

    python scripts/search_parameters.py \\
        --engine reversion \\
        --trials 200 \\
        --train-start 2018-01-01 --train-end 2023-12-31 \\
        --holdout-start 2022-01-01 --holdout-end 2023-12-31 \\
        --final-holdout-start 2024-01-01 --final-holdout-end 2025-12-31 \\
        --output backtests/reversion_search_results.csv
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import os
import random
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np

# Each engine's run_for_search is imported lazily inside _runner_for so the
# orchestrator stays importable even when an engine module is being refactored.


# ────────────────────────────────────────────────────────────────────────────
# Parameter ranges per engine — uniform sampling unless noted.
# ────────────────────────────────────────────────────────────────────────────


PARAM_RANGES: dict[str, dict[str, tuple]] = {
    # Sigma archived 2026-05-16 — its FINAL test (#168, failed-expansion
    # redesign) FAILED decisively (DSR 0.0000, held-back 2020-2026
    # Sharpe -1.208, 50/50 trials negative). See archive/sigma/EULOGY.md.
    "reversion": {
        "z_threshold": (2.0, 4.0, "float"),
        "volume_climax_multiplier": (1.2, 3.0, "float"),
        "max_hold_days": (3, 12, "int"),
        "stop_pct": (0.04, 0.12, "float"),
        # Earnings-quality removed from the search ranges — current
        # fundamentals_quarterly coverage on the wider universe is sparse
        # enough that any HIGH/MEDIUM filter produces near-zero trades.
        # Reversion's run_*_with_context defaults to filter_mode="none" when
        # the override is absent, so the search sweeps the technical knobs
        # against a no-EQ-gate baseline.
    },
    "vector": {
        # pb_ceiling lower-bound 1.5 (was 1.0) per the 2026-05-14
        # recalibration sweep: pb<1.5 is the known-overly-restrictive
        # zone that produced 0 candidates on the prior sweep. The point
        # of this run is to find the P/B threshold at which Vector
        # actually fires AND maintains a credible edge.
        "pb_ceiling": (1.5, 3.5, "float"),
        "de_ceiling": (1.5, 4.0, "float"),
        "catalyst_window_days": (3, 10, "int"),
        "swing_score_threshold": (55.0, 75.0, "float"),
        "stop_pct": (0.04, 0.10, "float"),
    },
    "momentum": {
        # Deliberately low-dim: 4 knobs, narrow ranges around the standard
        # 12-1 academic spec. DSR correction is much friendlier at small N.
        "lookback_days": (200, 280, "int"),
        "skip_days": (15, 30, "int"),
        "hold_days": (15, 30, "int"),
        "top_decile_pct": (0.05, 0.20, "float"),
    },
}


def _sample_value(spec: tuple, rng: random.Random) -> Any:
    low, high, kind = spec
    if kind == "float":
        return round(rng.uniform(low, high), 4)
    if kind == "int":
        return rng.randint(int(low), int(high))
    if kind.startswith("choice:"):
        choices = kind.split(":", 1)[1].split(",")
        return rng.choice(choices)
    raise ValueError(f"unknown spec kind: {kind}")


def sample_parameters(engine: str, n: int, seed: int = 0) -> list[dict]:
    ranges = PARAM_RANGES[engine]
    rng = random.Random(seed)
    return [{k: _sample_value(spec, rng) for k, spec in ranges.items()} for _ in range(n)]


# ────────────────────────────────────────────────────────────────────────────
# Walk-forward windows
# ────────────────────────────────────────────────────────────────────────────


@dataclass
class WalkWindow:
    """One walk-forward window: 5-year train, 2-year holdout, both inclusive."""

    train_start: date
    train_end: date  # = holdout_start - 1 day
    holdout_start: date
    holdout_end: date

    def label(self) -> str:
        return f"{self.train_start.year}-{self.holdout_end.year}"


def build_walk_windows(
    *, train_start: date, holdout_end: date, step_days: int = 365,
    train_years: int = 5, holdout_years: int = 2,
) -> list[WalkWindow]:
    """Produce non-overlapping-train walk-forward windows.

    Each window slides forward by ``step_days``. The last window must have its
    ``holdout_end`` ≤ ``holdout_end`` (i.e. fit within the overall span)."""
    out: list[WalkWindow] = []
    cur = train_start
    while True:
        t_start = cur
        t_end = t_start + timedelta(days=train_years * 365 - 1)
        h_start = t_end + timedelta(days=1)
        h_end = h_start + timedelta(days=holdout_years * 365 - 1)
        if h_end > holdout_end:
            break
        out.append(
            WalkWindow(
                train_start=t_start, train_end=t_end,
                holdout_start=h_start, holdout_end=h_end,
            )
        )
        cur = cur + timedelta(days=step_days)
    return out


# ────────────────────────────────────────────────────────────────────────────
# Slice metrics — Sharpe etc on a subset of trades
# ────────────────────────────────────────────────────────────────────────────


@dataclass
class SliceMetrics:
    """Metrics computed on a trade-log slice (typically the holdout window)."""

    n_trades: int
    sharpe: float
    profit_factor: float
    max_drawdown: float
    win_rate: float

    def to_dict(self) -> dict:
        return {
            "n_trades": self.n_trades,
            "sharpe": float(self.sharpe) if math.isfinite(self.sharpe) else 0.0,
            "profit_factor": float(self.profit_factor) if math.isfinite(self.profit_factor) else 0.0,
            "max_drawdown": float(self.max_drawdown),
            "win_rate": float(self.win_rate),
        }


def compute_slice_metrics_from_trades(
    trades: list[Any], span_days: int,
) -> SliceMetrics:
    """Aggregate trades into per-period portfolio returns, then compute metrics.

    Trades sharing the same ``entry_date`` are treated as one rebalance
    period — their pnl_pcts are equal-weighted into a single portfolio
    period return. For sequential single-position engines (Sigma/Reversion/
    Vector) each entry_date already has at most one trade, so the
    aggregation is a no-op. For parallel-position engines (Momentum) the
    ~130 concurrent ticker-month trades collapse into one period return.

    Equity curve uses geometric (multiplicative) compounding, which is
    correct for both modes — the previous arithmetic ``cumsum`` was wrong
    in both, but the bug was masked for single-position strategies with
    small pnl_pcts where arithmetic ≈ geometric.

    Sharpe is annualized via *periods*/year (not trades/year), which is
    the statistically meaningful denominator for portfolio strategies.
    """
    if not trades:
        return SliceMetrics(0, 0.0, 0.0, 0.0, 0.0)
    # Group by entry_date — each unique entry_date is one rebalance period.
    by_date: dict[Any, list[float]] = {}
    for t in trades:
        by_date.setdefault(t.entry_date, []).append(float(t.pnl_pct))
    period_returns_arr = np.array(
        [float(np.mean(pnls)) for _, pnls in sorted(by_date.items())],
        dtype=float,
    )
    n_periods = len(period_returns_arr)
    if n_periods == 0:
        return SliceMetrics(0, 0.0, 0.0, 0.0, 0.0)

    wins = period_returns_arr[period_returns_arr > 0]
    losses = period_returns_arr[period_returns_arr < 0]
    win_rate = float(len(wins) / n_periods)
    periods_per_year = n_periods / (span_days / 365.25) if span_days else n_periods
    if period_returns_arr.std(ddof=1) > 0 and n_periods > 1:
        sharpe = float(
            period_returns_arr.mean() / period_returns_arr.std(ddof=1)
            * math.sqrt(periods_per_year)
        )
    else:
        sharpe = 0.0

    # Geometric equity curve = ∏(1 + r_period).
    equity = np.concatenate(([1.0], np.cumprod(1.0 + period_returns_arr)))
    peak = np.maximum.accumulate(equity)
    max_dd = float(((equity - peak) / peak).min())
    gross_w = float(wins.sum()) if len(wins) else 0.0
    gross_l = float(-losses.sum()) if len(losses) else 0.0
    pf = float(gross_w / gross_l) if gross_l > 0 else float("inf")
    # n_trades reports the raw position count for transparency; metrics use n_periods.
    return SliceMetrics(
        n_trades=len(trades), sharpe=sharpe, profit_factor=pf,
        max_drawdown=max_dd, win_rate=win_rate,
    )


def period_returns_from_trades(trades: list[Any]) -> list[float]:
    """Aggregate per-trade pnl_pcts to per-rebalance-period portfolio returns.

    Same grouping as :func:`compute_slice_metrics_from_trades`. Exposed so
    the DSR computation can operate on the statistically-meaningful unit."""
    if not trades:
        return []
    by_date: dict[Any, list[float]] = {}
    for t in trades:
        by_date.setdefault(t.entry_date, []).append(float(t.pnl_pct))
    return [float(np.mean(pnls)) for _, pnls in sorted(by_date.items())]


# ────────────────────────────────────────────────────────────────────────────
# Engine dispatch
# ────────────────────────────────────────────────────────────────────────────


def _runner_for(engine: str) -> Callable[..., Awaitable[Any]]:
    """Legacy single-call entry; loads context per call. Used for the final
    held-back run where we don't reuse context across many trials."""
    if engine == "reversion":
        from reversion.backtest import run_for_search
        return run_for_search
    if engine == "vector":
        from vector.backtest import run_for_search
        return run_for_search
    if engine == "momentum":
        from momentum.backtest import run_for_search
        return run_for_search
    raise ValueError(f"unknown engine: {engine}")


def _context_loader_for(engine: str) -> Callable[..., Awaitable[Any]]:
    """Returns the async ``load_*_window_context`` function for the engine."""
    if engine == "reversion":
        from reversion.backtest import load_reversion_window_context
        return load_reversion_window_context
    if engine == "vector":
        from vector.backtest import load_vector_window_context
        return load_vector_window_context
    if engine == "momentum":
        from momentum.backtest import load_momentum_window_context
        return load_momentum_window_context
    raise ValueError(f"unknown engine: {engine}")


def _context_runner_for(engine: str) -> Callable[..., Any]:
    """Returns the sync ``run_*_with_context`` function for the engine."""
    if engine == "reversion":
        from reversion.backtest import run_reversion_with_context
        return run_reversion_with_context
    if engine == "vector":
        from vector.backtest import run_vector_with_context
        return run_vector_with_context
    if engine == "momentum":
        from momentum.backtest import run_momentum_with_context
        return run_momentum_with_context
    raise ValueError(f"unknown engine: {engine}")


# ────────────────────────────────────────────────────────────────────────────
# Per-trial run
# ────────────────────────────────────────────────────────────────────────────


@dataclass
class TrialResult:
    """One (window, trial) outcome."""

    trial_id: int
    window_label: str
    parameters: dict
    holdout: SliceMetrics
    full_credibility_score: int  # the engine's credibility on [start, end] full window
    error: str | None = None


def _evaluate_candidate_with_context(
    *, trial_id: int, window: WalkWindow, parameters: dict,
    context: Any, ctx_runner: Callable[..., Any],
) -> TrialResult:
    """Sync — runs the engine's CPU-only ``run_*_with_context`` against a
    pre-loaded :class:`*WindowContext`, then slices to the holdout dates."""
    try:
        result = ctx_runner(context, overrides=parameters)
    except Exception as exc:  # noqa: BLE001
        return TrialResult(
            trial_id=trial_id, window_label=window.label(), parameters=parameters,
            holdout=SliceMetrics(0, 0.0, 0.0, 0.0, 0.0), full_credibility_score=0,
            error=str(exc),
        )

    holdout_trades = [
        t for t in result.trade_log
        if window.holdout_start <= t.entry_date <= window.holdout_end
    ]
    span_days = (window.holdout_end - window.holdout_start).days or 1
    slice_metrics = compute_slice_metrics_from_trades(holdout_trades, span_days)
    return TrialResult(
        trial_id=trial_id, window_label=window.label(), parameters=parameters,
        holdout=slice_metrics, full_credibility_score=int(result.credibility_score),
    )


# ────────────────────────────────────────────────────────────────────────────
# Ranking
# ────────────────────────────────────────────────────────────────────────────


def _score_for_ranking(metrics: SliceMetrics) -> float:
    """OOS score used to rank candidates.

    Combines OOS Sharpe + a soft penalty for trade-count thinness. Higher is
    better. We avoid using credibility-from-full-window for ranking because
    that conflates train and holdout."""
    if metrics.n_trades < 3:
        return -1.0  # trade count too low to be statistically meaningful
    base = float(metrics.sharpe)
    # Mild bonus for higher trade counts (statistical power).
    return base + 0.05 * math.log10(max(metrics.n_trades, 1))


def rank_candidates(trials: list[TrialResult]) -> list[tuple[dict, float, int]]:
    """Aggregate trials by parameters (deterministic key), return ranked list of
    (parameters, mean_score, n_windows_evaluated)."""
    by_param: dict[str, list[TrialResult]] = {}
    for t in trials:
        if t.error:
            continue
        key = json.dumps(t.parameters, sort_keys=True)
        by_param.setdefault(key, []).append(t)
    ranked: list[tuple[dict, float, int]] = []
    for key, group in by_param.items():
        scores = [_score_for_ranking(t.holdout) for t in group]
        if not scores:
            continue
        ranked.append((json.loads(key), float(np.mean(scores)), len(group)))
    ranked.sort(key=lambda x: x[1], reverse=True)
    return ranked


# ────────────────────────────────────────────────────────────────────────────
# DSR for final verdict
# ────────────────────────────────────────────────────────────────────────────


def compute_dsr_for_verdict(returns: list[float], n_trials: int) -> float:
    """Deflated Sharpe Ratio corrected for the total number of search trials.

    Returns a probability ≥ 0.0; ≥ 0.95 is the "survived" threshold.
    Computed via the same formula in :mod:`tpcore.backtest.overfitting`."""
    if len(returns) < 2:
        return 0.0
    arr = np.asarray(returns, dtype=float)
    sr = float(arr.mean() / arr.std(ddof=1)) if arr.std(ddof=1) > 0 else 0.0
    n = len(arr)
    skew = float(((arr - arr.mean()) ** 3).mean() / (arr.std() ** 3)) if arr.std() > 0 else 0.0
    kurt = (
        float(((arr - arr.mean()) ** 4).mean() / (arr.std() ** 4))
        if arr.std() > 0 else 3.0
    )
    # Threshold from López de Prado (Deflated Sharpe Ratio, eqn 8/9). Same
    # formula as in tpcore/backtest/overfitting.py.
    EULER = 0.5772156649015329
    e_max = ((1.0 - EULER) * _norm_inv(1.0 - 1.0 / max(n_trials, 1))
             + EULER * _norm_inv(1.0 - 1.0 / (max(n_trials, 1) * math.e)))
    denom = math.sqrt(max(1.0 - skew * sr + (kurt - 1.0) / 4.0 * (sr ** 2), 1e-12) / max(n - 1, 1))
    if denom <= 0:
        return 0.0
    z = (sr - e_max) / denom
    return float(0.5 * (1.0 + math.erf(z / math.sqrt(2.0))))


def _norm_inv(p: float) -> float:
    """Inverse normal CDF via Beasley-Springer-Moro approximation (good enough
    for this use case — no scipy dependency required)."""
    if p <= 0.0 or p >= 1.0:
        return 0.0 if p <= 0.0 else 10.0
    # Acklam's approximation, well-known and self-contained.
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow = 0.02425
    phigh = 1.0 - plow
    if p < plow:
        q = math.sqrt(-2.0 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1.0)
    if p <= phigh:
        q = p - 0.5
        r = q * q
        return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
               (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1.0)
    q = math.sqrt(-2.0 * math.log(1.0 - p))
    return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
            ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1.0)


# ────────────────────────────────────────────────────────────────────────────
# Output / persistence
# ────────────────────────────────────────────────────────────────────────────


def write_results_csv(path: Path, trials: list[TrialResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow([
            "trial_id", "window", "parameters_json",
            "holdout_n_trades", "holdout_sharpe", "holdout_pf", "holdout_dd", "holdout_win_rate",
            "full_credibility_score", "error",
        ])
        for t in trials:
            w.writerow([
                t.trial_id, t.window_label, json.dumps(t.parameters, sort_keys=True),
                t.holdout.n_trades, f"{t.holdout.sharpe:.4f}",
                f"{t.holdout.profit_factor:.4f}" if math.isfinite(t.holdout.profit_factor) else "inf",
                f"{t.holdout.max_drawdown:.4f}", f"{t.holdout.win_rate:.4f}",
                t.full_credibility_score, t.error or "",
            ])


# ────────────────────────────────────────────────────────────────────────────
# Entry point
# ────────────────────────────────────────────────────────────────────────────


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--engine", choices=("reversion", "vector", "momentum"), required=True)
    p.add_argument("--trials", type=int, default=200,
                   help="Total parameter combinations to pre-sample (default 200).")
    p.add_argument("--per-window-trials", type=int, default=50,
                   help="Random subsample of trials evaluated per walk-forward window.")
    p.add_argument("--train-start", type=date.fromisoformat, default=date(2018, 1, 1))
    p.add_argument("--holdout-end", type=date.fromisoformat, default=date(2023, 12, 31),
                   help="End of the search walk-forward span (last holdout window's end).")
    p.add_argument("--final-holdout-start", type=date.fromisoformat, default=date(2024, 1, 1))
    p.add_argument("--final-holdout-end", type=date.fromisoformat, default=date(2025, 12, 31))
    p.add_argument("--walk-forward-step", type=int, default=365,
                   help="Days to advance the train-window between walk-forward iterations.")
    # The spec calls for 5y train / 2y holdout, but our continuous-coverage
    # window is 2018→2023 (final-holdout reserves 2024-2025), so a 5/2 split
    # only fits one window. Default to 3/1 so the walk-forward loop produces
    # multiple windows; override at the CLI when more history is available.
    p.add_argument("--train-years", type=int, default=3,
                   help="Walk-forward train window in years (default 3; spec target 5).")
    p.add_argument("--holdout-years", type=int, default=1,
                   help="Walk-forward holdout window in years (default 1; spec target 2).")
    p.add_argument("--seed", type=int, default=0,
                   help="Seed for the parameter sampler (reproducibility).")
    p.add_argument("--output", type=Path, default=None,
                   help="CSV destination for per-trial results.")
    p.add_argument("--database-url", default=None,
                   help="Postgres URL; defaults to $DATABASE_URL.")
    p.add_argument("--dsr-threshold", type=float, default=0.95,
                   help="DSR floor for the SURVIVED verdict (default 0.95).")
    p.add_argument("--credibility-threshold", type=int, default=60,
                   help="Credibility floor for SURVIVED verdict (default 60).")
    p.add_argument("--universe-tier-max", type=int, default=None,
                   help=(
                       "If set, pull the universe from platform.liquidity_tiers where "
                       "tier <= this value (1=tightest spread, 5=widest). Typical: 2 for "
                       "T1+T2 (~1,300 names), 3 for T1+T2+T3 (~2,700). When omitted, each "
                       "engine uses its built-in default universe."
                   ))
    return p.parse_args(argv)


async def _load_universe_by_tier(db_url: str, max_tier: int) -> tuple[str, ...]:
    """Query platform.liquidity_tiers for tickers with tier ≤ max_tier."""
    import asyncpg

    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=1)
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT ticker FROM platform.liquidity_tiers "
                "WHERE tier <= $1 ORDER BY ticker",
                max_tier,
            )
    finally:
        await pool.close()
    return tuple(r["ticker"] for r in rows)


async def amain(args: argparse.Namespace) -> int:
    db_url = args.database_url or os.getenv("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL not set — pass --database-url or export it.", file=sys.stderr)
        return 2

    print(f"\n═══ Parameter Search — engine={args.engine}  trials={args.trials} ═══\n")

    candidates = sample_parameters(args.engine, args.trials, seed=args.seed)
    print(f"  → sampled {len(candidates)} parameter combinations  (seed={args.seed})")

    windows = build_walk_windows(
        train_start=args.train_start, holdout_end=args.holdout_end,
        step_days=args.walk_forward_step,
        train_years=args.train_years, holdout_years=args.holdout_years,
    )
    if not windows:
        print(
            f"ERROR: no walk-forward windows fit between {args.train_start} and "
            f"{args.holdout_end}. Widen the span or shorten train/holdout duration.",
            file=sys.stderr,
        )
        return 2
    print(f"  → built {len(windows)} walk-forward window(s):")
    for w in windows:
        print(f"      {w.label():>9}  train {w.train_start}→{w.train_end}  holdout {w.holdout_start}→{w.holdout_end}")
    print()

    runner = _runner_for(args.engine)
    ctx_loader = _context_loader_for(args.engine)
    ctx_runner = _context_runner_for(args.engine)
    rng = random.Random(args.seed + 1)

    universe: tuple[str, ...] | None = None
    if args.universe_tier_max is not None:
        universe = await _load_universe_by_tier(db_url, args.universe_tier_max)
        print(
            f"  → universe: {len(universe)} tickers from liquidity_tiers "
            f"(tier ≤ {args.universe_tier_max})"
        )
    else:
        print("  → universe: engine default (typically ~50 mega-caps)")

    trials: list[TrialResult] = []
    trial_id_seq = 0
    for w in windows:
        # Sample subset of candidates for THIS window. Same RNG seed → reproducible.
        idxs = rng.sample(range(len(candidates)), min(args.per_window_trials, len(candidates)))
        print(f"── window {w.label()}: loading panels + indicators (one-time per window) ──")
        load_start = time.time()
        context = await ctx_loader(
            db_url=db_url, start=w.train_start, end=w.holdout_end,
            universe=universe,
        )
        load_secs = time.time() - load_start
        print(f"   panels loaded in {load_secs:.1f}s — evaluating {len(idxs)} candidates against shared context\n")
        for k, idx in enumerate(idxs, 1):
            tid = trial_id_seq
            trial_id_seq += 1
            params = candidates[idx]
            trial_start = time.time()
            result = _evaluate_candidate_with_context(
                trial_id=tid, window=w, parameters=params,
                context=context, ctx_runner=ctx_runner,
            )
            trial_secs = time.time() - trial_start
            trials.append(result)
            status = (
                f"err={result.error[:40]}" if result.error
                else f"n={result.holdout.n_trades}  sharpe={result.holdout.sharpe:+.2f}  ({trial_secs:.1f}s)"
            )
            print(f"  [{k:3d}/{len(idxs)}] trial {tid:>4}  param-idx {idx:>3}  {status}")
        print()

    out_path = args.output or Path(f"backtests/{args.engine}_search_results.csv")
    write_results_csv(out_path, trials)
    print(f"per-trial results → {out_path}\n")

    ranked = rank_candidates(trials)
    if not ranked:
        print("FAILED: no trial produced any rankable result (all errored or had < 3 trades).")
        return 1

    print("═══ Top 5 candidates by mean OOS score ═══")
    for i, (params, score, nw) in enumerate(ranked[:5], 1):
        print(f"  {i}. score={score:+.3f}  windows={nw}  params={json.dumps(params, sort_keys=True)}")
    print()

    # Final held-back validation on the WINNER.
    winner_params, winner_score, _ = ranked[0]
    print(f"═══ Final held-back: replaying winner on {args.final_holdout_start} → {args.final_holdout_end} ═══\n")
    final_result = await runner(
        db_url=db_url,
        start=args.train_start,
        end=args.final_holdout_end,
        overrides=winner_params,
        universe=universe,
    )
    # Slice to held-back only. DSR is computed on period-aggregated returns
    # (one observation per rebalance), which is the statistically-meaningful
    # unit; computing it on per-position trades would double-count concurrent
    # positions and produce nonsense.
    held_trades = [
        t for t in final_result.trade_log
        if args.final_holdout_start <= t.entry_date <= args.final_holdout_end
    ]
    span_days = (args.final_holdout_end - args.final_holdout_start).days or 1
    held_metrics = compute_slice_metrics_from_trades(held_trades, span_days)
    held_period_returns = period_returns_from_trades(held_trades)
    dsr = compute_dsr_for_verdict(held_period_returns, n_trials=args.trials)

    # Persist the winner's CredibilityScore to platform.data_quality_log so
    # downstream tools (tip sheet, capital gate) can read it. Without this,
    # the rubric breakdown is recomputed on every search but never stored.
    if final_result.credibility_rubric is not None:
        import asyncpg

        from tpcore.backtest.statistical_validation import write_credibility_score

        persist_pool = await asyncpg.create_pool(db_url, min_size=1, max_size=1)
        try:
            wrote = await write_credibility_score(
                persist_pool,
                engine_name=args.engine,
                score=final_result.credibility_rubric,
            )
            print(
                f"  → persisted credibility rubric to platform.data_quality_log "
                f"(source=backtest_credibility.{args.engine}, wrote={wrote})\n"
            )
        finally:
            await persist_pool.close()

    print(f"  Trade count        : {held_metrics.n_trades}")
    print(f"  Sharpe (held-back) : {held_metrics.sharpe:+.3f}")
    print(f"  Profit factor      : {held_metrics.profit_factor:+.3f}")
    print(f"  Max drawdown       : {held_metrics.max_drawdown*100:+.2f}%")
    print(f"  Credibility (full) : {final_result.credibility_score}/100")
    print(f"  DSR (n_trials={args.trials:>3}): {dsr:.4f}")
    print()

    survived = (
        dsr >= args.dsr_threshold
        and final_result.credibility_score >= args.credibility_threshold
        and held_metrics.n_trades >= 3
    )
    if survived:
        print(f"  VERDICT: SURVIVED — DSR ≥ {args.dsr_threshold} AND credibility ≥ {args.credibility_threshold}")
        print(f"  → recommend promoting these parameters: {json.dumps(winner_params, sort_keys=True)}")
        return 0

    print(f"  VERDICT: FAILED — DSR < {args.dsr_threshold} or credibility < {args.credibility_threshold}")
    print("\n  Top 5 alternatives (for the next iteration):")
    for i, (params, score, nw) in enumerate(ranked[:5], 1):
        print(f"    {i}. score={score:+.3f}  windows={nw}  params={json.dumps(params, sort_keys=True)}")
    return 1


def main() -> None:  # pragma: no cover - CLI shim
    raise SystemExit(asyncio.run(amain(_parse_args())))


if __name__ == "__main__":  # pragma: no cover
    main()
