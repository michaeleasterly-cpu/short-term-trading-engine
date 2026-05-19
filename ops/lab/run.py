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

SDLC SP2 T5 (H-S2-1): this module hosts the walk-forward Lab engine. It
imports the engine packages (``reversion.backtest`` etc.) inside the
``_runner_for`` / ``_context_*_for`` dispatch — LEGAL in ``ops/`` (exempt
from the ``tpcore.scripts.check_imports`` tpcore∌engine AST scan), illegal
in ``tpcore/``. ``scripts/search_parameters.py`` is now a thin re-export
shim preserving the historical CLI + every symbol the characterization
oracle pins.

Usage
-----
::

    python -m ops.lab.run \\
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
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import structlog

# tpcore.lab is the engine-FREE contract layer (H-S2-1) — safe to import
# at module top; only the engine packages stay lazy in _runner_for etc.
# MIN_TRIALS_FOR_V is a pure constant; tpcore.backtest.overfitting is
# module-top-safe (no circular import) so it lives here, not lazy in-body.
from tpcore.backtest.overfitting import MIN_TRIALS_FOR_V
from tpcore.lab.models import (
    LabCandidate,
    LabResult,
    ParamDelta,
    WalkWindowRecord,
)

logger = structlog.get_logger(__name__)

# Each engine's run_for_search is imported lazily inside _runner_for so the
# orchestrator stays importable even when an engine module is being refactored.


# ────────────────────────────────────────────────────────────────────────────
# Parameter ranges per engine — uniform sampling unless noted.
# ────────────────────────────────────────────────────────────────────────────


# ────────────────────────────────────────────────────────────────────────────
# SP-B — roster-SoT-driven dispatch resolver. Replaces the stale hardwired
# (reversion, vector, momentum) 3-tuple across all six surfaces. Engine
# add/remove is a tpcore.engine_profile._PROFILE edit + the engine
# declaring LAB_TARGET — NEVER Lab surgery (spec §1, §2.3).
# ────────────────────────────────────────────────────────────────────────────


def _lab_target_for(engine: str) -> Any:
    """Resolve the engine's declared LabTarget via the roster SoT.

    Engine import is LAZY (legal in ops/, H-S2-1 — the resolver lives in
    ops/, NOT tpcore/). Hard-rejects an engine that is not
    roster-Lab-targetable OR has not declared LAB_TARGET with a CLEAR
    ValueError — never a raw KeyError/ImportError to the operator. The
    reject fires inside sample_parameters BEFORE the SP-A
    record_trial_spend block (run.py:752-759) so no partial ledger write
    is possible (spec §4.5, §8-A4)."""
    from tpcore.engine_profile import lab_targetable_engines

    targetable = lab_targetable_engines()
    if engine not in targetable:
        raise ValueError(
            f"engine {engine!r} is not Lab-targetable; choose one of "
            f"{targetable} (roster SoT: tpcore.engine_profile)"
        )
    import importlib

    try:
        mod = importlib.import_module(f"{engine}.backtest")
    except (ImportError, SyntaxError) as exc:
        # ImportError is the superclass of ModuleNotFoundError (a missing
        # module preserves the existing not-targetable/undeclared message
        # shape) AND covers a partial/circular `from x import missing_y`;
        # SyntaxError is NOT an ImportError subclass so it must be named
        # explicitly. Post-SP-B this catch is the ONLY fence on the new
        # planner.py:693 lazy-import path (`PARAM_RANGES.get(ecr.engine,
        # {})` on the live-adjacent MODIFY-ECR validator): any
        # non-ModuleNotFoundError ImportError or a SyntaxError anywhere in
        # a declared engine's transitive `<engine>.backtest` import
        # surface MUST become the same clear fail-loud ValueError so
        # __getitem__ converts it to KeyError and `.get(...)` cleanly
        # returns {} instead of crashing that validator (spec §2.3 / EC7
        # / §2.4 / §8-A2). Deliberately NOT a bare `except Exception`.
        raise ValueError(
            f"engine {engine!r} has a {engine}.backtest module that "
            f"failed to import/parse ({type(exc).__name__}): {exc}"
        ) from exc
    target = getattr(mod, "LAB_TARGET", None)
    if target is None:
        raise ValueError(
            f"engine {engine!r} is roster-Lab-eligible but has not "
            f"declared a module-level LAB_TARGET in {engine}.backtest "
            f"(see tpcore/lab/target.py:LabTarget). This is the SP-E/SP-F "
            f"forward step: the engine must declare its Lab contract."
        )
    return target


class _LazyParamRanges(Mapping):
    """``PARAM_RANGES`` kept as a NAME (oracle/planner compat) but driven
    by the roster SoT. The BINDING contract (spec §2.4, the §8 highest
    residual risk): ``__getitem__`` re-raises ``_lab_target_for``'s
    ``ValueError`` as ``KeyError`` so ``collections.abc.Mapping.get`` (which
    catches ``KeyError`` ONLY) keeps ``planner.py:694``'s
    ``.get(ecr.engine, {})`` returning ``{}`` for a non-targetable engine
    instead of crashing the live-adjacent MODIFY-ECR validator with an
    unhandled ``ValueError``."""

    def __getitem__(self, engine: str) -> dict[str, tuple]:
        try:
            return _lab_target_for(engine).param_ranges
        except ValueError as exc:
            raise KeyError(engine) from exc

    def __iter__(self):
        # Declared targets only, dispatch_order — same membership+order
        # as the old literal dict's insertion order (reversion, vector,
        # momentum). Eligible-but-undeclared (sentinel) is skipped.
        from tpcore.engine_profile import lab_targetable_engines

        for engine in lab_targetable_engines():
            try:
                _lab_target_for(engine)
            except ValueError:
                continue
            yield engine

    def __len__(self) -> int:
        return sum(1 for _ in self)


PARAM_RANGES: Mapping = _LazyParamRanges()


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
    try:
        ranges = PARAM_RANGES[engine]
    except KeyError:
        # Re-raise as the CLEAR operator-facing ValueError (defence in
        # depth for the programmatic run_lab() path + legacy shim; the
        # argparse choices gate rejects bad engines far earlier on every
        # real CLI path). spec §2.4.
        _lab_target_for(engine)  # raises the clear ValueError
        raise  # unreachable — _lab_target_for always raises here
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
    # SP-A2 / H-A2-11: the UN-annualized per-period Sharpe
    # (mean/std(ddof=1), the same quantity BEFORE the √periods_per_year
    # factor). Additive + ranking-neutral + oracle-neutral: the
    # annualized `sharpe` above is byte-IDENTICAL; this field exists
    # ONLY so compute_dsr_for_verdict's V-term is units-coherent with
    # its per-period SR̂ (annualized V would inflate SR₀ by ≈ppy).
    holdout_sharpe_per_period: float = 0.0

    def to_dict(self) -> dict:
        return {
            "n_trades": self.n_trades,
            "sharpe": float(self.sharpe) if math.isfinite(self.sharpe) else 0.0,
            "profit_factor": float(self.profit_factor) if math.isfinite(self.profit_factor) else 0.0,
            "max_drawdown": float(self.max_drawdown),
            "win_rate": float(self.win_rate),
            "holdout_sharpe_per_period": (
                float(self.holdout_sharpe_per_period)
                if math.isfinite(self.holdout_sharpe_per_period) else 0.0
            ),
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
        # SP-A2 / H-A2-11: the per-period (un-annualized) Sharpe is the
        # base quantity; the annualized `sharpe` is it × √periods_per_year
        # — the annualized expression is byte-IDENTICAL to before.
        sharpe_per_period = float(
            period_returns_arr.mean() / period_returns_arr.std(ddof=1)
        )
        sharpe = float(sharpe_per_period * math.sqrt(periods_per_year))
    else:
        sharpe_per_period = 0.0
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
        holdout_sharpe_per_period=sharpe_per_period,
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
    """Legacy single-call entry; loads context per call. SP-B: a thin
    view over the roster-SoT resolver (name + signature unchanged so the
    characterization oracle's by-name monkeypatch still binds)."""
    return _lab_target_for(engine).run_for_search


def _context_loader_for(engine: str) -> Callable[..., Awaitable[Any]]:
    """Returns the async ``load_*_window_context``. SP-B thin view."""
    return _lab_target_for(engine).load_window_context


def _context_runner_for(engine: str) -> Callable[..., Any]:
    """Returns the sync ``run_*_with_context``. SP-B thin view."""
    return _lab_target_for(engine).run_with_context


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


def compute_dsr_for_verdict(
    returns: list[float],
    n_trials: int,
    *,
    trial_sharpe_variance: float | None = None,
) -> float:
    """Deflated Sharpe Ratio corrected for the total number of search
    trials. Returns a probability ≥ 0.0; ≥ 0.95 is the "survived"
    threshold. Same formula as
    :func:`tpcore.backtest.overfitting._expected_max_sharpe_under_null`
    — the two impls MUST stay coherent (H-A2-7).

    ``trial_sharpe_variance`` — V[ŜR_n], the **cross-trial** variance
    of the per-trial *per-period* Sharpe estimates across the searched
    trials (``ddof=1``; the same per-period space as ``sr`` below — NOT
    the annualized ``SliceMetrics.sharpe`` — H-A2-11). When ``None`` (a
    count-only / non-Lab caller, e.g. the SP2 oracle's two-arg call),
    fall back to the single-estimator ``1/(n-1)`` approximation AND emit
    a structlog WARNING — documented, never silent (§1.3, H-A2-1). The
    H-A2-10 floor ``max(V, 1/(n-1))`` makes the change tightening-or-
    equal for every input. The V-source trial population and ``n_trials``
    (the SP-A cumulative selection budget) are deliberately distinct
    estimands (H-A2-4/§6) — the floor bounds the residual seam (H-A2-13).
    """
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
    # formula as tpcore/backtest/overfitting.py.
    EULER = 0.5772156649015329
    e_max_bracket = (
        (1.0 - EULER) * _norm_inv(1.0 - 1.0 / max(n_trials, 1))
        + EULER * _norm_inv(1.0 - 1.0 / (max(n_trials, 1) * math.e))
    )
    floor = 1.0 / max(n - 1, 1)  # legacy single-estimator value — now a FLOOR
    if trial_sharpe_variance is not None:
        # H-A2-10: clamp up to the floor — an honest low-dispersion sweep
        # must NOT loosen the (already-too-lenient) legacy bar.
        sr_variance = max(float(trial_sharpe_variance), floor)
    else:
        sr_variance = floor  # KNOWN APPROXIMATION — not the paper's V
        logger.warning(
            "tpcore.overfitting.dsr.null_variance_approximation",
            reason="no per-trial Sharpe vector available; using "
                   "single-estimator 1/(n_obs-1) instead of "
                   "cross-trial V[SR_n]",
            n_trials=n_trials,
            n_obs=n,
        )
    # The √V factor is now supplied solely by the V-term (the legacy
    # 1/(n-1) is REMOVED from the V role — it conflated within-strategy
    # estimation noise into the selection-bias term, the same defect
    # expressed differently). The non-normality term stays in `denom`.
    #
    # DEVIATION (plan Task-5 Step-3, real-code-aligned per SP-A2 brief
    # "PLAN'S INTENT wins but align to the REAL code + spec §3.4"):
    # the plan's literal `e_max = √(sr_variance)·bracket` was authored
    # against overfitting.py's structure, where the legacy 1/(n-1)-
    # equivalent scaled `e_max`. In THIS impl the legacy `e_max` is the
    # PURE Φ⁻¹ bracket (unscaled) and the 1/(n-1) lived entirely inside
    # `denom` (§3.4/H-A2-7 / §summary 372a: "folds 1/(n-1) into `denom`
    # rather than `e_max`"). Literally scaling `e_max` by √(1/(n-1)) on
    # the fallback would NOT be byte-identical — it double-counts 1/(n-1)
    # (also in `denom`) and inflated the fallback DSR 0.112→1.0 on a
    # representative input (catastrophic LOOSENING of a live-money gate;
    # the plan's "Note (H-A2-15)" algebraic-identity claim is provably
    # false for this impl, |diff|=0.888). The factor √(sr_variance/floor)
    # is the symmetric correction that satisfies BOTH binding
    # requirements: on the fallback (sr_variance == floor) it is exactly
    # 1.0 ⇒ `e_max == bracket` ⇒ None/default path BYTE-IDENTICAL to
    # pre-SP-A2 (spec §5/§8 T-VERDICT-FALLBACK-WARNS; HARD CONSTRAINT);
    # with a supplied V the H-A2-10 clamp makes sr_variance ≥ floor ⇒
    # factor ≥ 1 ⇒ `e_max` ≥ bracket ⇒ DSR ≤ fallback (tightening-or-
    # equal — the H-A2-10 floor semantics, correction-symmetric with
    # tpcore/backtest/overfitting.py::_expected_max_sharpe_under_null).
    # Plan intent + every T5 assertion preserved byte-identical.
    e_max = math.sqrt(sr_variance / floor) * e_max_bracket
    # `denom` is the non-normality ESTIMATION term — NOT the V/selection-
    # bias role. Kept in its EXACT legacy single-sqrt arithmetic form
    # (`sqrt(nonnorm / (n-1))`) rather than the plan's split
    # `sqrt(nonnorm)/sqrt(n-1)`: the split is algebraically equal but not
    # universally bit-identical (≤1 ULP on some inputs) and the §5/§8
    # HARD CONSTRAINT is bit-for-bit byte-identity on the None/default
    # path. The plan's intent (move the V role OFF `denom` onto the V-
    # term) is fully preserved — the V role is the `√(sr_variance/floor)`
    # factor on `e_max`; this line's `1/(n-1)` is the non-normality
    # estimator, untouched, deliberately kept legacy-exact.
    denom = math.sqrt(
        max(1.0 - skew * sr + (kurt - 1.0) / 4.0 * (sr ** 2), 1e-12)
        / max(n - 1, 1)
    )
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


def _lab_credibility_engine_name(target_engine: str, candidate: str) -> str:
    """Lab credibility is namespaced ``lab.<candidate>`` so
    ``graduation_ready(pool, <target_engine>)`` can NEVER read an
    experimental score (live-safety, H-S2-3). ``target_engine`` is
    accepted for signature symmetry/future use; intentionally unused —
    a ``fold_existing`` Lab run targeting ``target_engine="reversion"``
    must leave ``backtest_credibility.reversion`` byte-identical."""
    return f"lab.{candidate}"


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
    from tpcore.db import build_asyncpg_pool

    # Pooler-safety (statement_cache_size=0 + jit:off + URL normalization)
    # lives in the ONE canonical builder — Supabase txn-pooler note in
    # tpcore.db. SELECT-only ⇒ read_only=True (explicit read/write intent
    # is mandatory on the SP2/SP3 isolation boundary — H-S3-8).
    pool = await build_asyncpg_pool(
        db_url, min_size=1, max_size=1, read_only=True)
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


@dataclass
class _LabCore:
    """The structured outcome of one walk-forward Lab run — the shared
    spine of ``amain`` (prints + int rc, oracle-pinned) and ``run_lab``
    (returns a frozen ``LabResult`` for the dossier). Carrying the exact
    locals ``amain`` already computes means the walk-forward is run
    EXACTLY ONCE: ``run_lab`` does not re-execute it (T10 seam)."""

    winner_params: dict
    winner_score: float
    held_metrics: SliceMetrics
    dsr: float
    full_credibility_score: int
    credibility_rubric: Any | None
    ranked: list[tuple[dict, float, int]]
    windows: list[WalkWindow]
    survived: bool
    effective_n_trials: int


async def _run_lab_core(
    args: argparse.Namespace, candidate: str | None = None,
) -> _LabCore | int:
    """Walk-forward search + final held-back verdict — the orchestration
    body shared by ``amain`` and ``run_lab``.

    Returns an ``int`` rc for the three non-result outcomes (no DSN → 2,
    no walk-forward windows fit → 2, no rankable trial → 1) — the exact
    legacy stderr/stdout message is already printed inline, so
    ``amain``'s observable contract (rc + the ``write_credibility_score``
    call) is byte-identical pre/post extraction (T1 characterization
    oracle). A successful run returns the :class:`_LabCore` spine.

    ``candidate`` is the Lab seam (H-S2-3): when set (a Lab run), the
    final credibility is persisted under the Lab-namespaced source
    ``backtest_credibility.lab.<candidate>`` so it can NEVER poison
    ``graduation_ready(pool, <live_engine>)``. When ``None`` (the legacy
    ``python scripts/search_parameters.py`` manual operator search — NOT
    a Lab run), the historical contract is preserved byte-identical:
    persist under ``backtest_credibility.<args.engine>``.
    """
    db_url = args.database_url or os.getenv("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL not set — pass --database-url or export it.", file=sys.stderr)
        return 2

    print(f"\n═══ Parameter Search — engine={args.engine}  trials={args.trials} ═══\n")

    candidates = sample_parameters(args.engine, args.trials, seed=args.seed)
    print(f"  → sampled {len(candidates)} parameter combinations  (seed={args.seed})")

    # SP-A H-LL-1 (the §3.2 spine): record this run's trial SPEND as its
    # own UNCONDITIONAL append-only fact, RIGHT HERE — before the DSR
    # code and before EVERY non-result rc return below (no DSN already
    # returned at the early :2 path above; no-windows/no-rankable returns
    # are still ahead). An abort-after-fishing therefore still counts
    # (T-ABORT). Lab seam (H-S2-3): only a Lab run (candidate is not
    # None) with the active LabContext RW handle
    # (active_credibility_pool(), the ONE allowlisted RW pool — H-LL-3
    # reuse, no second ad-hoc pool). The legacy non-Lab path (candidate
    # is None / no LabContext) emits nothing and stays byte-identical
    # (T9). spend_ts is the strict ``<`` boundary the cumulative read
    # uses below.
    from tpcore.lab.context import active_credibility_pool
    from tpcore.lab.ledger import cumulative_n_trials, record_trial_spend

    _ledger_pool = (
        active_credibility_pool() if candidate is not None else None
    )
    spend_ts = None
    if _ledger_pool is not None:
        spend_ts = await record_trial_spend(
            _ledger_pool,
            target=args.engine,
            candidate=candidate,
            trials=args.trials,
            seed=args.seed,
        )

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
        return 1  # noqa: RET504 — oracle-pinned non-result rc

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
    # SP-A §2.3: the multiple-testing penalty is CUMULATIVE — every
    # configuration ever scored against this target, summed, plus this
    # run's own args.trials (read strictly BEFORE this run's spend row
    # so the current run is counted exactly once via the explicit
    # + args.trials). Legacy non-Lab path (spend_ts is None) keeps the
    # per-run penalty byte-identical (T6/T9: SP-A reduces to today's
    # behaviour when cumulative == 0). The gate expression + thresholds
    # below are UNCHANGED — only this n_trials input grows.
    if spend_ts is not None:
        cumulative = await cumulative_n_trials(
            _ledger_pool, args.engine, spend_ts)
        effective_n_trials = cumulative + args.trials
    else:
        effective_n_trials = args.trials
    # SP-A2 / H-A2-9 + H-A2-11: V[ŜR_n] is the cross-trial dispersion of
    # the per-trial *per-period* (NON-annualized) holdout Sharpes across
    # this run's searched trials — the same per-period space as
    # compute_dsr_for_verdict's internal SR̂. NOT t.holdout.sharpe (that
    # is ANNUALIZED — feeding it would inflate SR₀ by ≈periods_per_year).
    # Guarded by MIN_TRIALS_FOR_V (H-A2-10): too few non-errored trials ⇒
    # None ⇒ the documented 1/(n-1) fallback + WARNING inside the call.
    # H-A2-4: V-source trial count and the SP-A cumulative n_trials are
    # deliberately distinct estimands — logged side-by-side, not
    # silently reconciled (the floor bounds the residual seam, H-A2-13).
    pp_sharpes = [
        t.holdout.holdout_sharpe_per_period
        for t in trials
        if not t.error
    ]
    if len(pp_sharpes) >= MIN_TRIALS_FOR_V:
        trial_sharpe_var: float | None = float(
            np.var(np.asarray(pp_sharpes, dtype=float), ddof=1)
        )
        logger.info(
            "ops.lab.dsr.v_n_trial_population",
            v_trial_count=len(pp_sharpes),
            n_trials=effective_n_trials,
        )
    else:
        trial_sharpe_var = None
    dsr = compute_dsr_for_verdict(
        held_period_returns,
        n_trials=effective_n_trials,
        trial_sharpe_variance=trial_sharpe_var,
    )

    # Persist the winner's CredibilityScore to platform.data_quality_log so
    # downstream tools (tip sheet, capital gate) can read it. Without this,
    # the rubric breakdown is recomputed on every search but never stored.
    if final_result.credibility_rubric is not None:
        import asyncpg

        from tpcore.backtest.statistical_validation import write_credibility_score

        # H-S2-3 live-safety: the Lab path (candidate set) persists under
        # the Lab-namespaced engine_name so graduation_ready(pool,
        # <live_engine>) can never read an experimental score. The legacy
        # search-CLI path (candidate None) stays byte-identical.
        cred_engine_name = (
            _lab_credibility_engine_name(args.engine, candidate)
            if candidate is not None
            else args.engine
        )
        # H-S3-8 / spec §7.2: under an active LabContext (a Lab run —
        # candidate is not None) the credibility write goes through the
        # context's ONE allowlisted RW handle, NOT a second ad-hoc RW
        # asyncpg.create_pool inside the SP2 isolation boundary. The
        # legacy search-CLI path (candidate is None — no active
        # LabContext) stays byte-identical: it keeps opening its own
        # pool. The write_credibility_score(engine_name=…, score=…) call
        # args are unchanged in both paths.
        from tpcore.lab.context import active_credibility_pool

        ctx_pool = active_credibility_pool() if candidate is not None else None
        if ctx_pool is not None:
            wrote = await write_credibility_score(
                ctx_pool,
                engine_name=cred_engine_name,
                score=final_result.credibility_rubric,
            )
            print(
                f"  → persisted credibility rubric to platform.data_quality_log "
                f"(source=backtest_credibility.{cred_engine_name}, wrote={wrote})\n"
            )
        else:
            # H-S3-8 byte-identical-legacy invariant: the legacy non-Lab
            # path (candidate is None / no active LabContext) MUST open its
            # OWN ad-hoc raw asyncpg.create_pool — deliberately NOT routed
            # through build_asyncpg_pool / any context handle (the SP-A SP2
            # isolation guarantee, enforced by
            # test_lab_credibility_pool_threaded.py). So the canonical
            # pooler-safety kwargs are mirrored inline here instead.
            # keep in sync with tpcore.db.build_asyncpg_pool (Supabase
            # txn-pooler: statement_cache_size=0 + server_settings jit:off).
            persist_pool = await asyncpg.create_pool(
                db_url, min_size=1, max_size=1,
                statement_cache_size=0,
                server_settings={"jit": "off"},
            )
            try:
                wrote = await write_credibility_score(
                    persist_pool,
                    engine_name=cred_engine_name,
                    score=final_result.credibility_rubric,
                )
                print(
                    f"  → persisted credibility rubric to platform.data_quality_log "
                    f"(source=backtest_credibility.{cred_engine_name}, wrote={wrote})\n"
                )
            finally:
                await persist_pool.close()

    # The 6-line held-back metrics block is rendered EXACTLY ONCE by the
    # caller that owns presentation: the legacy operator path prints it in
    # ``amain``; the CLI path (``run_lab`` → ``_build_lab_result``) carries
    # it in the written dossier instead. ``_run_lab_core`` (the shared
    # spine) intentionally prints nothing here — printing it here too
    # would double-print on the legacy path (T10 review #1).
    survived = (
        dsr >= args.dsr_threshold
        and final_result.credibility_score >= args.credibility_threshold
        and held_metrics.n_trades >= 3
    )
    return _LabCore(
        winner_params=winner_params,
        winner_score=winner_score,
        held_metrics=held_metrics,
        dsr=dsr,
        full_credibility_score=int(final_result.credibility_score),
        credibility_rubric=final_result.credibility_rubric,
        ranked=ranked,
        windows=windows,
        survived=survived,
        effective_n_trials=effective_n_trials,
    )


async def amain(args: argparse.Namespace, candidate: str | None = None) -> int:
    """Walk-forward search + final held-back verdict — prints the human
    report and returns the int rc (0 SURVIVED / 1 FAILED / 2 setup
    error). Behaviour matches the pre-T10 ``amain``: the rc and the
    ``write_credibility_score`` call args are preserved exactly (T1
    oracle pins ``amain(args, candidate) -> int`` + the credibility call
    args). The structured spine is computed once by :func:`_run_lab_core`
    (no duplicated walk-forward); the 6-line held-back metrics block is
    printed once HERE (the pre-T10 single print is preserved — the spine
    no longer prints it, so the legacy operator path is stdout-faithful
    to pre-T10, not double-printed). This function then renders the
    verdict block and maps it to the historical exit code. ``candidate``
    is the H-S2-3 Lab-namespacing seam.
    """
    core = await _run_lab_core(args, candidate)
    if isinstance(core, int):
        return core  # non-result outcome — message already printed inline.

    print(f"  Trade count        : {core.held_metrics.n_trades}")
    print(f"  Sharpe (held-back) : {core.held_metrics.sharpe:+.3f}")
    print(f"  Profit factor      : {core.held_metrics.profit_factor:+.3f}")
    print(f"  Max drawdown       : {core.held_metrics.max_drawdown*100:+.2f}%")
    print(f"  Credibility (full) : {core.full_credibility_score}/100")
    print(f"  DSR (n_trials={core.effective_n_trials:>4}): {core.dsr:.4f}")
    print()

    if core.survived:
        print(f"  VERDICT: SURVIVED — DSR ≥ {args.dsr_threshold} AND credibility ≥ {args.credibility_threshold}")
        print(f"  → recommend promoting these parameters: {json.dumps(core.winner_params, sort_keys=True)}")
        return 0

    print(f"  VERDICT: FAILED — DSR < {args.dsr_threshold} or credibility < {args.credibility_threshold}")
    print("\n  Top 5 alternatives (for the next iteration):")
    for i, (params, score, nw) in enumerate(core.ranked[:5], 1):
        print(f"    {i}. score={score:+.3f}  windows={nw}  params={json.dumps(params, sort_keys=True)}")
    return 1


def _build_lab_result(
    *, candidate: LabCandidate, core: _LabCore, args: argparse.Namespace,
) -> LabResult:
    """Assemble the frozen SP2→SP3 contract (:class:`LabResult`) from the
    already-computed :class:`_LabCore` — pure, no DB, no re-run. The
    recommendation is a deterministic function of the numbers (D-SP2-8:
    SP2 recommends, never applies): FAILED → ``"none"``; SURVIVED →
    the candidate's declared intent. The O1 ``default_params()`` seam
    (SP3 T1) supplies the live default for each swept param, so
    ``param_diff`` carries the real ``current → winning`` diff
    (SP3 §7.1).
    """
    # LabResult.credibility_rubric is non-optional (spec §7). _LabCore
    # carries it as Any | None; if a run produced none, raise the clean
    # RuntimeError run_lab already maps to an explicit CLI rc1 rather
    # than letting pydantic raise an unhandled ValidationError here.
    if core.credibility_rubric is None:
        raise RuntimeError(
            "Lab run produced no credibility rubric — cannot build a LabResult"
        )
    verdict = "SURVIVED" if core.survived else "FAILED"
    recommended_exit = candidate.intent if core.survived else "none"
    from ops.engine_sdlc.default_params import default_params
    _live_defaults = default_params(args.engine)
    param_diff = [
        ParamDelta(name=k, current=_live_defaults.get(k), winning=v)
        for k, v in sorted(core.winner_params.items())
    ]
    walk_windows = [
        WalkWindowRecord(
            train_start=w.train_start, train_end=w.train_end,
            holdout_start=w.holdout_start, holdout_end=w.holdout_end,
        )
        for w in core.windows
    ]
    return LabResult(
        candidate=candidate.name,
        target_engine=candidate.target_engine,
        intent=candidate.intent,
        verdict=verdict,
        dsr=core.dsr,
        credibility_score=core.full_credibility_score,
        credibility_rubric=core.credibility_rubric,
        held_metrics=core.held_metrics.to_dict(),
        winning_params=core.winner_params,
        param_diff=param_diff,
        recommended_exit=recommended_exit,
        ranked_alternatives=[p for p, _s, _n in core.ranked[:5]],
        walk_windows=walk_windows,
        n_trials=core.effective_n_trials,
        seed=args.seed,
        generated_at=datetime.now(UTC),
    )


async def run_lab(
    args: argparse.Namespace, *, candidate: LabCandidate,
) -> LabResult:
    """The T10 CLI seam: run the walk-forward Lab EXACTLY ONCE (via the
    same :func:`_run_lab_core` ``amain`` uses — no duplicated search) and
    return the frozen :class:`LabResult` the dossier renders. Raises
    ``RuntimeError`` for the non-result outcomes (no DSN / no windows /
    no rankable trial) — the CLI maps that to an explicit non-zero rc,
    never a silent 0. ``amain``'s behaviour is unchanged (the oracle is
    unaffected: ``run_lab`` is additive and ``_run_lab_core`` preserves
    every print + the ``write_credibility_score`` call site).
    """
    core = await _run_lab_core(args, candidate.name)
    if isinstance(core, int):
        raise RuntimeError(
            f"Lab produced no result (rc={core}): no DSN, no walk-forward "
            f"window fit the span, or no trial was rankable. See the "
            f"message above."
        )
    return _build_lab_result(candidate=candidate, core=core, args=args)


def main() -> None:  # pragma: no cover - CLI shim
    raise SystemExit(asyncio.run(amain(_parse_args())))


if __name__ == "__main__":  # pragma: no cover - dev alias of `python -m ops.lab`
    main()
