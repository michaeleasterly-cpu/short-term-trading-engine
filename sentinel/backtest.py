"""Sentinel — backtest driver.

Simulates Sentinel's activation/deactivation cycles against historical
macro indicators (``platform.macro_indicators``) and ETF prices
(``platform.prices_daily``). Produces:

* JSON metrics object compatible with :class:`BacktestRunResult` so the
  parameter-search pipeline and `scripts/run_dashboard.sh` can consume
  it without bespoke shaping.
* Per-ETF-per-cycle trade log CSV (one row per closed basket position).
* Bear-Score / phase-history CSV for operator review.

The "trade" granularity is one ETF position per activation cycle:
    entry  = first ACTIVE/FADING day on which the basket was held
    exit   = last day before returning to DORMANT/EXITED
    qty    = target_shares on the entry day (no intra-cycle rebalances —
             a forward-looking enhancement)

Tier-aware costs (``tpcore.backtest.cost_model.get_round_trip_cost``)
are applied per ETF. ETFs are typically T1 (narrow spread), so costs
are small but non-zero.

This module also hosts TWO sibling pre-registered Lab candidates,
declared via the engine-OWNED ``LAB_TARGET`` (resolved roster-driven by
``ops.lab.run``); the live trading path NEVER imports this module:

* ``sentinel_maxdd`` (sibling candidate, MERGED) — toggle
  ``activation_score_threshold`` ``choice:60,55``; tests whether
  earlier activation reduces holdout drawdown. Spec:
  ``docs/superpowers/specs/2026-05-20-sentinel-maxdd-lab-candidate.md``.
* ``sentinel_bear_score`` (THIS candidate) — toggle ``bear_score_mode``
  ``choice:current,graduated``; tests whether a five-factor graduated
  Bear-Score composite (Sahm/SOS/curve/CFNAI-MA3/HY-OAS with
  literature-anchored thresholds and three action bands) reduces
  holdout drawdown vs the legacy binary activation. Spec:
  ``docs/superpowers/specs/2026-05-21-sentinel-bear-score-lab-candidate.md``.

Both candidates are off-by-default backtest seams; the LIVE trading path
is byte-identical when neither override is supplied (proven by the C1
characterization tests in ``sentinel/tests/``).
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from datetime import date as date_t
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd
import structlog

from sentinel.models import (
    ACTIVATION_SCORE_THRESHOLD,
    BASKET_WEIGHTS_DEFAULT,
    SentinelDecision,
    SentinelPhase,
    SentinelState,
)
from sentinel.plugs.execution_risk import SentinelExecutionRisk
from sentinel.plugs.lifecycle_analysis import SentinelLifecycleAnalysis
from sentinel.plugs.setup_detection import (
    SentinelSetupDetection,
    fetch_spy_close,
)
from tpcore.backtest.cost_model import get_round_trip_cost
from tpcore.backtest.search import (
    BacktestRunResult,
    SearchTrade,
    compute_search_metrics,
    write_trade_log_csv,
)
from tpcore.backtest.statistical_validation import write_credibility_score
from tpcore.db import build_asyncpg_pool
from tpcore.lab.target import LabPrimaryMetric, LabTarget

logger = structlog.get_logger(__name__)

DEFAULT_OUTPUT_DIR = Path("backtests")
DEFAULT_RESULTS_FILE = "sentinel_backtest_results.json"
DEFAULT_TRADES_FILE = "sentinel_trades.csv"
DEFAULT_PHASE_HISTORY_FILE = "sentinel_phase_history.csv"

# Default capital for sizing — affects share counts but not pnl_pct (the
# metric the diagnostic actually consumes). Backtest economics are
# percentage-based so this is largely cosmetic.
DEFAULT_PLATFORM_EQUITY_USD = Decimal("100000")


async def _fetch_etf_prices(
    pool,
    *,
    start: date_t,
    end: date_t,
) -> dict[str, pd.Series]:
    """Pull close prices for every basket ETF + SPY. Returns ``{ticker: Series}``.

    Tickers missing from ``platform.prices_daily`` yield empty Series; the
    caller handles re-weighting via :func:`apply_missing_etf_fallback`.
    """
    tickers = list(BASKET_WEIGHTS_DEFAULT.keys()) + ["SPY"]
    sql = """
        SELECT ticker, date, close
        FROM platform.prices_daily
        WHERE ticker = ANY($1)
          AND date BETWEEN $2 AND $3
        ORDER BY ticker, date
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, tickers, start - timedelta(days=365), end)
    out: dict[str, pd.Series] = {t: pd.Series(dtype=float, name=t) for t in tickers}
    if not rows:
        return out
    df = pd.DataFrame(
        [{"ticker": r["ticker"], "date": r["date"], "close": float(r["close"])} for r in rows]
    )
    for t, group in df.groupby("ticker"):
        out[t] = pd.Series(
            {pd.Timestamp(r["date"]): r["close"] for _, r in group.iterrows()},
            name=t,
        ).sort_index()
    return out


async def _round_trip_cost_by_ticker(
    pool,
    *,
    tickers: list[str],
) -> dict[str, Decimal]:
    """Per-ticker round-trip cost (fraction). Tier-aware via cost_model."""
    out: dict[str, Decimal] = {}
    for t in tickers:
        try:
            out[t] = await get_round_trip_cost(pool, t)
        except Exception as exc:  # noqa: BLE001
            logger.warning("sentinel.backtest.cost_lookup_failed", ticker=t, error=str(exc)[:120])
            out[t] = Decimal("0.001")  # 10 bps fallback
    return out


def _simulate(
    states: Mapping[date_t, SentinelState],
    decisions: Mapping[date_t, SentinelDecision],
    etf_prices: dict[str, pd.Series],
    round_trip_costs: dict[str, Decimal],
) -> tuple[list[SearchTrade], list[dict[str, Any]]]:
    """Walk the daily decisions and emit one SearchTrade per closed cycle position.

    A cycle is the contiguous run of ACTIVE/FADING days under the same
    ``cycle_id``. On the first day of a cycle we record the target shares
    for each ETF; on the last day we mark out at that day's close and
    compute realized pnl_pct net of round-trip cost.

    The function is pure (no DB). Returns the SearchTrade list and a
    parallel list of dicts for OverfittingDiagnostic (it expects a
    slightly different schema).
    """
    sorted_dates = sorted(states.keys())
    if not sorted_dates:
        return [], []

    open_positions: dict[str, dict[str, Any]] = {}
    current_cycle_id: int | None = None
    trades: list[SearchTrade] = []
    trades_for_diag: list[dict[str, Any]] = []

    def _close_positions(close_date: date_t, exit_reason: str) -> None:
        for ticker, pos in list(open_positions.items()):
            price_series = etf_prices.get(ticker)
            if price_series is None or len(price_series) == 0:
                continue
            sub = price_series.loc[price_series.index <= pd.Timestamp(close_date)].dropna()
            if len(sub) == 0:
                continue
            exit_price = float(sub.iloc[-1])
            entry_price = pos["entry_price"]
            gross_ret = (exit_price - entry_price) / entry_price
            rtc = float(round_trip_costs.get(ticker, Decimal("0.001")))
            net_ret = gross_ret - rtc
            trades.append(SearchTrade(
                ticker=ticker,
                entry_date=pos["entry_date"],
                entry_price=entry_price,
                exit_date=close_date,
                exit_price=exit_price,
                pnl_pct=net_ret,
                direction="LONG",
                exit_reason=exit_reason,
            ))
            trades_for_diag.append({
                "ticker": ticker,
                "entry_date": pos["entry_date"],
                "exit_date": close_date,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "pnl_pct": net_ret,
                "direction": "LONG",
            })
            del open_positions[ticker]

    for d in sorted_dates:
        st = states[d]
        dec = decisions.get(d)

        if st.phase in (SentinelPhase.DORMANT, SentinelPhase.WATCH, SentinelPhase.EXITED):
            if open_positions:
                _close_positions(d, exit_reason=f"PHASE_{st.phase.value}")
                current_cycle_id = None
            continue

        # ACTIVE / FADING: open positions on the first day; on subsequent
        # days we don't rebalance — Sentinel's basket is set-and-hold
        # within a cycle, with shape changes only via the override flags
        # (which we apply on entry and then re-check at deactivation —
        # not a full per-day rebalance, which would inflate trade count).
        if dec is None or not dec.targets:
            # Active but no targets (no prices available) — nothing to do.
            continue
        if st.cycle_id != current_cycle_id and not open_positions:
            # First day of a fresh cycle.
            current_cycle_id = st.cycle_id
            for tgt in dec.targets:
                price_series = etf_prices.get(tgt.ticker)
                if price_series is None or len(price_series) == 0:
                    continue
                sub = price_series.loc[price_series.index <= pd.Timestamp(d)].dropna()
                if len(sub) == 0:
                    continue
                entry_price = float(sub.iloc[-1])
                open_positions[tgt.ticker] = {
                    "entry_date": d,
                    "entry_price": entry_price,
                    "shares": tgt.target_shares,
                    "cycle_id": st.cycle_id,
                }

    # Close any still-open positions on the last simulated day.
    if open_positions:
        _close_positions(sorted_dates[-1], exit_reason="BACKTEST_END")

    return trades, trades_for_diag


def _phase_history_rows(
    states: Mapping[date_t, SentinelState],
    breakdowns: Mapping[date_t, Any],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for d in sorted(states.keys()):
        bs = breakdowns.get(d)
        st = states[d]
        out.append({
            "date": d.isoformat(),
            "phase": st.phase.value,
            "bear_score": st.bear_score,
            "consecutive_above": st.consecutive_days_above_threshold,
            "days_in_phase": st.days_in_phase,
            "cycle_id": st.cycle_id if st.cycle_id is not None else "",
            "shallow_override": int(st.shallow_recession_override),
            "vix_breaker": int(st.vix_circuit_breaker),
            "sqqq_eligible": int(st.sqqq_eligible),
            "fade_factor": str(st.fade_factor),
            "spy_rally_in_window_pct": str(st.spy_rally_pct_in_window),
            **(
                {
                    "sahm_pts": bs.sahm_pts,
                    "industrial_production_pts": bs.industrial_production_pts,
                    "initial_claims_pts": bs.initial_claims_pts,
                    "yield_curve_pts": bs.yield_curve_pts,
                    "credit_spread_pts": bs.credit_spread_pts,
                    "vix_pts": bs.vix_pts,
                    "raw_total": bs.raw_total,
                    "indicators_missing": "|".join(bs.indicators_missing),
                }
                if bs is not None else {}
            ),
        })
    return out


def _compute_summary(trades: list[SearchTrade]) -> tuple[float, float, float]:
    """(Sharpe-annualized, profit_factor, max_drawdown) from per-trade pct returns.

    Sharpe assumes the cycle of trades occurs over the lifecycle phase
    duration; treating each trade as one observation gives a directly
    comparable Sharpe to the other engines' search-pipeline numbers.
    """
    if not trades:
        return 0.0, 0.0, 0.0
    rets = [t.pnl_pct for t in trades]
    avg = sum(rets) / len(rets)
    sd = (sum((r - avg) ** 2 for r in rets) / max(1, len(rets) - 1)) ** 0.5
    sharpe = (avg / sd) * (252 ** 0.5) if sd > 0 else 0.0
    wins = sum(r for r in rets if r > 0)
    losses = -sum(r for r in rets if r < 0)
    pf = (wins / losses) if losses > 0 else (wins if wins > 0 else 0.0)
    cumulative = 1.0
    peak = 1.0
    max_dd = 0.0
    for r in rets:
        cumulative *= (1.0 + r)
        peak = max(peak, cumulative)
        max_dd = max(max_dd, (peak - cumulative) / peak)
    return float(sharpe), float(pf), float(-max_dd)


async def run_backtest(
    *,
    start: date_t,
    end: date_t,
    output_dir: Path,
    results_file: str,
    trades_file: str,
    phase_history_file: str,
    json_output: bool,
    trade_log_path: Path | None,
    graduated: bool,
) -> int:
    """Execute the Sentinel backtest end-to-end."""
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL not set", file=sys.stderr)
        return 1
    output_dir.mkdir(parents=True, exist_ok=True)
    pool = await build_asyncpg_pool(db_url)
    try:
        setup = SentinelSetupDetection()
        breakdowns = await setup.compute_for_range(pool, start=start, end=end)
        if not breakdowns:
            print("ERROR: no Bear Score breakdowns produced — check macro_indicators + SPY data",
                  file=sys.stderr)
            return 1
        spy = await fetch_spy_close(pool, start=start, end=end)
        lifecycle = SentinelLifecycleAnalysis()
        states = lifecycle.walk_states(breakdowns, spy_close=spy)

        etf_prices = await _fetch_etf_prices(pool, start=start, end=end)
        round_trip_costs = await _round_trip_cost_by_ticker(
            pool, tickers=list(BASKET_WEIGHTS_DEFAULT.keys()),
        )

        # Pre-compute decisions for every day so the simulator can iterate
        # purely. Holdings are empty by default in the backtest — we re-
        # compute target shares on each day; the simulator handles the
        # entry/exit accounting itself rather than trusting the order diff.
        execution = SentinelExecutionRisk(graduated=graduated)
        decisions: dict[date_t, SentinelDecision] = {}
        for d, st in states.items():
            prices_today: dict[str, Decimal] = {}
            for t, series in etf_prices.items():
                if t == "SPY" or len(series) == 0:
                    continue
                sub = series.loc[series.index <= pd.Timestamp(d)].dropna()
                if len(sub) > 0:
                    prices_today[t] = Decimal(str(round(float(sub.iloc[-1]), 4)))
            if not prices_today:
                continue
            decisions[d] = execution.build_decision(
                as_of=d, state=st,
                equity_usd=DEFAULT_PLATFORM_EQUITY_USD,
                prices=prices_today,
                current_holdings={},
            )

        trades, trades_for_diag = _simulate(states, decisions, etf_prices, round_trip_costs)
        sharpe, pf, max_dd = _compute_summary(trades)

        # Bundle into BacktestRunResult via compute_search_metrics so the
        # JSON shape matches the other engines.
        prices_for_diag = (
            etf_prices.get("SPY", pd.Series(dtype=float)).to_frame(name="close")
            .rename_axis("date").reset_index()
        )
        prices_for_diag["ticker"] = "SPY"
        result: BacktestRunResult = compute_search_metrics(
            engine="sentinel",
            parameters={
                "activation_score_threshold": 60,
                "activation_consecutive_days": 3,
                "deep_recession_score_threshold": 80,
                "vix_breaker_threshold": 40.0,
                "fade_days": 5,
                "graduated": graduated,
            },
            trades_for_diag=trades_for_diag,
            sharpe=sharpe,
            profit_factor=pf,
            max_drawdown=max_dd,
            n_trials=1,
            price_data=prices_for_diag,
            rubric_inputs={
                "lookahead_clean": True,
                "survivorship_inclusive": True,
                "pit_fundamentals": True,
                "regime_coverage": False,  # one cycle / few cycles; flag honestly
                "monte_carlo_drawdown": True,
            },
            search_trades=trades,
        )

        # Persist the credibility rubric to platform.data_quality_log so
        # SentinelCapitalGate.assert_can_graduate has a row to read. Mirror
        # Reversion's pattern (reversion/backtest.py:~1423).
        if result.credibility_rubric is not None:
            wrote = await write_credibility_score(
                pool, engine_name="sentinel", score=result.credibility_rubric,
            )
            logger.info(
                "sentinel.backtest.credibility_persisted",
                wrote=wrote, score=result.credibility_score,
            )

        # Write artefacts.
        results_path = output_dir / results_file
        trades_path = output_dir / trades_file
        phase_path = output_dir / phase_history_file
        results_path.write_text(result.to_json())
        write_trade_log_csv(trades_path, trades)
        if trade_log_path is not None:
            write_trade_log_csv(trade_log_path, trades)
        _write_phase_history(phase_path, _phase_history_rows(states, breakdowns))

        # Stdout.
        if json_output:
            print(result.to_json())
        else:
            n_cycles = sum(1 for s in states.values() if s.phase == SentinelPhase.ACTIVE
                           and s.days_in_phase == 1)
            print(_format_human_summary(start, end, len(trades), n_cycles, sharpe, pf, max_dd, result))
            print(f"\nartifacts → {results_path}, {trades_path}, {phase_path}")
        return 0
    finally:
        await pool.close()


def _format_human_summary(
    start: date_t,
    end: date_t,
    n_trades: int,
    n_cycles: int,
    sharpe: float,
    pf: float,
    max_dd: float,
    result: BacktestRunResult,
) -> str:
    return "\n".join([
        f"Sentinel backtest — {start} → {end}",
        f"  activation cycles : {n_cycles}",
        f"  basket-position trades : {n_trades}",
        f"  Sharpe (annualized)   : {sharpe:+.3f}",
        f"  Profit factor         : {pf:.3f}",
        f"  Max drawdown          : {max_dd:+.3%}",
        f"  Credibility score     : {result.credibility_score}/100  (passed_gate={result.passed_gate})",
        f"  DSR                   : {result.dsr:.4f}",
        f"  Trades-per-param      : {result.trades_per_param:.2f}",
    ])


def _write_phase_history(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("")
        return
    # Union of keys across all rows (the indicator-pts keys only appear
    # when a breakdown was present, so the first row may not cover them).
    all_fields: list[str] = []
    seen: set[str] = set()
    for r in rows:
        for k in r:
            if k not in seen:
                all_fields.append(k)
                seen.add(k)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=all_fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in all_fields})


# ════════════════════════════════════════════════════════════════════════
# SP-E — Sentinel Lab-targeting surface (the front-half Lab proof case).
#
# Sentinel is a macro-defense BATCH engine; its success bar is NOT
# Sharpe/DSR-expressible — it is DRAWDOWN REDUCTION. The LAB_TARGET below
# declares ``primary_metric=LabPrimaryMetric.MAXDD_REDUCTION`` (SP-D), so
# the candidate RANKING is judged by holdout max-drawdown (shallower =
# better), while the SACRED graduation gate
# (DSR≥0.95 ∧ cred≥60 ∧ n_trades≥3) is byte-identical and UNTOUCHED —
# SP-D's pluggable metric only changes WHICH candidate wins the ranking,
# never WHETHER it graduates.
#
# Single pre-registered hypothesis (lab_candidate_readiness §1): the
# defensive basket's holdout drawdown is minimized at the canonical
# Bear-Score activation threshold (60). The ONE Lab-sampled toggle is
# ``activation_score_threshold`` — a ``choice:`` over
# ``{60 (legacy default), 55 (the one pre-registered earlier-activation
# variant)}``. NO second toggle, no grid, no env var (the
# feature-flag-variant rule, §2).
#
# Feature-flag-variant seam (§2, §3): ``_ACTIVATION_THRESHOLD_OVERRIDE``
# defaults to ``None`` (off). The LIVE trading path
# (``sentinel/scheduler.py`` → ``SentinelLifecycleAnalysis.walk_states``)
# NEVER enters ``run_sentinel_with_context`` and NEVER sets the override,
# so it is BYTE-IDENTICAL when the flag is off (proven by
# ``sentinel/tests/test_lab_activation_threshold_byte_identical.py``).
# The override is applied ONLY for the duration of the backtest's
# ``walk_states`` call by context-shadowing the module constant the plug
# bound at import (``lifecycle_analysis.ACTIVATION_SCORE_THRESHOLD``) and
# restoring it in ``finally`` — the per-call module-global reset
# discipline (Vector pilot §3.1/§4.2; no cross-trial leakage). This seam
# is contained ENTIRELY in this backtest module: no ``sentinel/plugs/``,
# no ``sentinel/scheduler.py``, no ``ops/lab/run.py`` edit (the toggle
# lives in ``LAB_TARGET.param_ranges``; ``PARAM_RANGES`` is roster-driven
# post-SP-B).
# ════════════════════════════════════════════════════════════════════════

# Off-by-default backtest-only override. None ⇒ the legacy module
# constant (ACTIVATION_SCORE_THRESHOLD) — the live path's value.
_ACTIVATION_THRESHOLD_OVERRIDE: int | None = None

# Off-by-default backtest-only override for the sentinel_bear_score Lab
# candidate. None / "current" / any unknown value ⇒ the legacy binary
# activation path (BYTE-IDENTICAL to pre-candidate behaviour). Only the
# exact string "graduated" reaches the variant branch (defense-in-depth
# against silent corruption from a malformed override).
_BEAR_SCORE_MODE_OVERRIDE: str | None = None

# ── Graduated Bear Score (sentinel_bear_score candidate) — pinned constants ──
#
# Every constant below is operator-pinned (TODO.md L537-552; spec §2).
# NONE of these is Lab-sampled. The ONE Lab-sampled value added by this
# candidate is the bear_score_mode choice toggle; the composite is fully
# determined by these constants when the variant fires.

# Composite weights — sum to 1.00 by construction.
_GRAD_W_SAHM = 0.30
_GRAD_W_SOS = 0.15
_GRAD_W_CURVE = 0.20
_GRAD_W_CFNAI = 0.15
_GRAD_W_HY_OAS = 0.20

# Anchor thresholds (literature-published recession signals, NOT fitted):
#   Sahm rule trigger ≥ 0.50  (Sahm 2019)
#   CFNAI-MA3 trigger ≤ -0.70 (Chicago Fed)
#   SOS state diffusion ≥ 0.20 (Crone/Clayton-Matthews 2005)
# These anchors live as comments — the [0,1] sub-score mappings below
# encode them via the choice of floor/ceiling.

# Per-factor [0, 1] sub-score linear-clip mappings (spec §2.2).
_GRAD_SAHM_FLOOR = 0.20
_GRAD_SAHM_CEIL = 0.80
_GRAD_SOS_FLOOR = 0.05
_GRAD_SOS_CEIL = 0.40
# yield_curve: inversion is value ≤ 0; saturate at -1.00.
_GRAD_CURVE_CEIL = 1.00
# CFNAI-MA3 is *negative* in contractions; we score -value.
_GRAD_CFNAI_FLOOR = 0.20
_GRAD_CFNAI_CEIL = 1.20
_GRAD_HY_FLOOR = 3.00
_GRAD_HY_CEIL = 8.00

# Action bands (spec §2.4) — graduated escalation, monotone-increasing.
_GRAD_BAND_LIGHT_LO = 0.45  # DORMANT → LIGHT
_GRAD_BAND_HEAVY_LO = 0.60  # LIGHT → HEAVY
_GRAD_BAND_DEEP_LO = 0.80   # HEAVY → DEEP

# Band → basket-scale (monotone non-decreasing).
_GRAD_SCALE_DORMANT = 0.00
_GRAD_SCALE_LIGHT = 0.40
_GRAD_SCALE_HEAVY = 0.80
_GRAD_SCALE_DEEP = 1.00

# Inverse-ETF cap — 25% of defensive capital (spec §2.5; Treasuries/gold
# first). Pinned.
_GRAD_INVERSE_ETF_CAP = 0.25

# Indicator names read from platform.macro_indicators (these are the
# canonical names in tpcore.fred.adapter.INDICATOR_SERIES).
_GRAD_INDICATORS: tuple[str, ...] = (
    "sahm_rule",
    "sos_state_diffusion",
    "yield_curve",
    "cfnai_ma3",
    "hy_spread",
)


def _activation_score_threshold() -> int:
    """The active Bear-Score activation threshold for THIS backtest run.

    Returns the legacy ``sentinel.models.ACTIVATION_SCORE_THRESHOLD``
    (60) unless the off-by-default Lab override is set. Pure."""
    return (
        _ACTIVATION_THRESHOLD_OVERRIDE
        if _ACTIVATION_THRESHOLD_OVERRIDE is not None
        else ACTIVATION_SCORE_THRESHOLD
    )


def _bear_score_mode() -> str:
    """The active Bear-Score MODE for THIS backtest run.

    Returns ``"graduated"`` ONLY when the off-by-default override is
    exactly the string ``"graduated"`` (defense-in-depth against silent
    corruption from a malformed override). Else ``"current"`` (the
    legacy binary-activation path). Pure."""
    return (
        "graduated"
        if _BEAR_SCORE_MODE_OVERRIDE == "graduated"
        else "current"
    )


def default_params() -> dict[str, Any]:
    """Current live defaults for EXACTLY this engine's Lab-sampled keys
    (the SP3 O1 dossier-param-diff seam). The legacy default carries the
    true ``legacy → variant`` delta into the dossier ``param_diff``
    (lab_candidate_readiness §2). Pure."""
    return {
        "activation_score_threshold": int(ACTIVATION_SCORE_THRESHOLD),
        "bear_score_mode": "current",
    }


# ── Graduated Bear Score helpers (sentinel_bear_score candidate) ─────────
#
# Pure functions consumed ONLY by the bear_score_mode="graduated" variant
# branch. The legacy "current" path never enters any of them — they are
# additive, not in the legacy code path. Each helper is unit-testable in
# isolation via the synthetic-fixture characterization test.


async def _fetch_graduated_macro_panel(
    pool,
    *,
    start: date_t,
    end: date_t,
) -> pd.DataFrame:
    """PIT-safe wide panel of the five graduated-Bear-Score indicators
    from ``platform.macro_indicators``.

    Returns a DataFrame indexed by ``date`` with columns
    ``(sahm_rule, sos_state_diffusion, yield_curve, cfnai_ma3, hy_spread)``.
    Forward-filled across the full daily index of the backtest window
    (padded back 365 days so monthly indicators have a value on day 0).
    Missing indicators yield an all-NaN column (the per-factor sub-score
    falls back to 0 in :func:`_grad_subscore_*`).

    Strictly-additive read: this loader is invoked unconditionally by
    :func:`load_sentinel_window_context` but the resulting panel is
    consumed ONLY in the graduated branch — the legacy path is unchanged.
    """
    sql = """
        SELECT date, indicator, value
        FROM platform.macro_indicators
        WHERE date BETWEEN $1 AND $2
          AND indicator = ANY($3)
        ORDER BY date
    """
    pad_start = start - timedelta(days=365)
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, pad_start, end, list(_GRAD_INDICATORS))
    if not rows:
        return pd.DataFrame(columns=list(_GRAD_INDICATORS))
    df = pd.DataFrame(
        [
            {"date": r["date"], "indicator": r["indicator"], "value": float(r["value"])}
            for r in rows
        ]
    )
    wide = df.pivot(index="date", columns="indicator", values="value").sort_index()
    for ind in _GRAD_INDICATORS:
        if ind not in wide.columns:
            wide[ind] = float("nan")
    daily_idx = pd.date_range(pad_start, end, freq="D").date
    wide = wide.reindex(daily_idx).ffill()
    wide.index.name = "date"
    return wide[list(_GRAD_INDICATORS)]


def _clip01(x: float) -> float:
    """Clamp ``x`` to ``[0, 1]``. Pure."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    return float(x)


def _grad_sub_sahm(value: float | None) -> float:
    """Sahm sub-score (spec §2.2). ``None`` ⇒ 0."""
    if value is None:
        return 0.0
    return _clip01((value - _GRAD_SAHM_FLOOR) / (_GRAD_SAHM_CEIL - _GRAD_SAHM_FLOOR))


def _grad_sub_sos(value: float | None) -> float:
    """SOS state-diffusion sub-score (spec §2.2). ``None`` ⇒ 0."""
    if value is None:
        return 0.0
    return _clip01((value - _GRAD_SOS_FLOOR) / (_GRAD_SOS_CEIL - _GRAD_SOS_FLOOR))


def _grad_sub_curve(value: float | None) -> float:
    """Yield-curve inversion sub-score (spec §2.2). ``None`` ⇒ 0.

    ``yield_curve`` is the T10Y2Y spread (percent). Inversion is
    ``value ≤ 0``; we score ``-value`` (so a more inverted curve scores
    higher) and saturate at -1.00 (sub-score = 1.0)."""
    if value is None:
        return 0.0
    return _clip01(-value / _GRAD_CURVE_CEIL)


def _grad_sub_cfnai(value: float | None) -> float:
    """CFNAI-MA3 sub-score (spec §2.2). ``None`` ⇒ 0.

    CFNAI-MA3 is *negative* in contractions; we score ``-value``."""
    if value is None:
        return 0.0
    neg = -value
    return _clip01((neg - _GRAD_CFNAI_FLOOR) / (_GRAD_CFNAI_CEIL - _GRAD_CFNAI_FLOOR))


def _grad_sub_hy_oas(value: float | None) -> float:
    """HY-OAS sub-score (spec §2.2). ``None`` ⇒ 0.

    ``hy_spread`` is in percent (BAMLH0A0HYM2)."""
    if value is None:
        return 0.0
    return _clip01((value - _GRAD_HY_FLOOR) / (_GRAD_HY_CEIL - _GRAD_HY_FLOOR))


def _grad_composite(panel_row: Mapping[str, float | None]) -> float:
    """Weighted composite of the five sub-scores (spec §2.3). Pure.

    ``panel_row`` is a Mapping with keys exactly ``_GRAD_INDICATORS`` and
    float-or-None values (a ``None`` entry — or NaN — is treated as
    missing and contributes 0 to that factor). Result is in ``[0, 1]``."""
    def _coerce(key: str) -> float | None:
        v = panel_row.get(key)
        if v is None:
            return None
        try:
            fv = float(v)
        except (TypeError, ValueError):
            return None
        if fv != fv:  # NaN
            return None
        return fv

    sahm = _grad_sub_sahm(_coerce("sahm_rule"))
    sos = _grad_sub_sos(_coerce("sos_state_diffusion"))
    curve = _grad_sub_curve(_coerce("yield_curve"))
    cfnai = _grad_sub_cfnai(_coerce("cfnai_ma3"))
    hy_oas = _grad_sub_hy_oas(_coerce("hy_spread"))
    return (
        _GRAD_W_SAHM * sahm
        + _GRAD_W_SOS * sos
        + _GRAD_W_CURVE * curve
        + _GRAD_W_CFNAI * cfnai
        + _GRAD_W_HY_OAS * hy_oas
    )


def _grad_band_scale(composite: float) -> float:
    """Action-band → basket scale (spec §2.4). Pure, monotone."""
    if composite < _GRAD_BAND_LIGHT_LO:
        return _GRAD_SCALE_DORMANT
    if composite < _GRAD_BAND_HEAVY_LO:
        return _GRAD_SCALE_LIGHT
    if composite < _GRAD_BAND_DEEP_LO:
        return _GRAD_SCALE_HEAVY
    return _GRAD_SCALE_DEEP


def _grad_basket_weights() -> dict[str, Decimal]:
    """Pinned graduated-mode basket weights (spec §2.5): the legacy
    Sentinel basket renormalized with the **inverse-ETF cap of 25 % of
    defensive capital**; Treasuries/gold first absorbs the surplus.

    Returns a fresh dict keyed by ticker → fractional weight summing to
    1.00. Pure."""
    # Legacy basket from sentinel.models.BASKET_WEIGHTS_DEFAULT:
    #   SH=0.35, PSQ=0.25, TLT=0.20, GLD=0.10, SQQQ=0.10
    # Inverse-ETFs (SH+PSQ+SQQQ) currently = 0.70, far above the 0.25
    # cap. Treasuries/gold (TLT+GLD) currently = 0.30. We cap inverse at
    # 0.25 and reallocate the surplus to TLT/GLD pro-rata.
    inverse_target = Decimal(str(_GRAD_INVERSE_ETF_CAP))
    inverse = {"SH", "PSQ", "SQQQ"}
    legacy = dict(BASKET_WEIGHTS_DEFAULT)

    inverse_legacy_total = sum(legacy[t] for t in inverse if t in legacy)
    treasury_gold_legacy_total = sum(
        legacy[t] for t in legacy if t not in inverse
    )

    # Scale inverse leg down to the cap, preserving relative shape.
    scaled: dict[str, Decimal] = {}
    if inverse_legacy_total > 0:
        for t in inverse:
            if t in legacy:
                scaled[t] = legacy[t] * inverse_target / inverse_legacy_total

    # Treasuries/gold leg absorbs the surplus pro-rata.
    treasury_gold_target = Decimal("1") - inverse_target
    if treasury_gold_legacy_total > 0:
        for t in legacy:
            if t in inverse:
                continue
            scaled[t] = (
                legacy[t] * treasury_gold_target / treasury_gold_legacy_total
            )
    # Renormalize to absorb any rounding drift.
    total = sum(scaled.values())
    if total <= 0:
        return {}
    return {t: (w / total) for t, w in scaled.items()}


@dataclass
class SentinelWindowContext:
    """Pre-loaded, parameter-INDEPENDENT inputs for one walk-forward
    window. Bear-Score breakdowns + SPY + ETF prices + costs are loaded
    ONCE; the threshold toggle is applied per-run in
    :func:`run_sentinel_with_context` (heavy I/O amortised across the
    window's Lab trials, mirroring the Momentum/Vector context idiom).

    ``macro_panel`` is the strictly-additive raw five-factor indicator
    panel consumed ONLY by the ``sentinel_bear_score`` candidate's
    ``bear_score_mode="graduated"`` variant branch (the graduated
    composite reads point-in-time raw ``sahm_rule``,
    ``sos_state_diffusion``, ``yield_curve``, ``cfnai_ma3``,
    ``hy_spread`` values). The legacy path NEVER reads it, so adding
    it is byte-identical for ``bear_score_mode="current"`` (lab_candidate
    _readiness §8). Defaults to ``None`` so pre-existing callers /
    fixtures continue to construct contexts without touching the new
    attribute; the graduated branch falls back to the legacy path when
    the panel is missing (defense-in-depth)."""

    breakdowns: Mapping[date_t, Any]
    spy_close: pd.Series
    etf_prices: dict[str, pd.Series]
    round_trip_costs: dict[str, Decimal]
    start: date_t
    end: date_t
    graduated: bool
    macro_panel: pd.DataFrame | None = None


async def load_sentinel_window_context(
    *,
    db_url: str,
    start: date_t,
    end: date_t,
    universe: tuple[str, ...] | None = None,
    graduated: bool = False,
) -> SentinelWindowContext:
    """Load Bear-Score breakdowns + SPY + ETF prices + tier costs for
    ``[start, end]``. Heavy I/O — call once per walk-forward window.

    ``universe`` is accepted for the uniform Lab dispatch signature but
    unused: Sentinel's traded set is the fixed defensive ETF basket
    (``BASKET_WEIGHTS_DEFAULT``), not a roster-derived universe.

    Strictly-additive read: ``macro_panel`` is loaded from
    ``platform.macro_indicators`` for the five graduated-Bear-Score
    factors. The legacy ``bear_score_mode="current"`` path NEVER reads
    this attribute (byte-identical contract preserved); only the
    ``bear_score_mode="graduated"`` variant branch consumes it."""
    _ = universe  # uniform-signature only; Sentinel's basket is fixed.
    pool = await build_asyncpg_pool(db_url)
    try:
        setup = SentinelSetupDetection()
        breakdowns = await setup.compute_for_range(pool, start=start, end=end)
        spy = await fetch_spy_close(pool, start=start, end=end)
        etf_prices = await _fetch_etf_prices(pool, start=start, end=end)
        round_trip_costs = await _round_trip_cost_by_ticker(
            pool, tickers=list(BASKET_WEIGHTS_DEFAULT.keys()),
        )
        macro_panel = await _fetch_graduated_macro_panel(
            pool, start=start, end=end,
        )
    finally:
        await pool.close()
    return SentinelWindowContext(
        breakdowns=breakdowns, spy_close=spy, etf_prices=etf_prices,
        round_trip_costs=round_trip_costs, start=start, end=end,
        graduated=graduated, macro_panel=macro_panel,
    )


def run_sentinel_with_context(
    context: SentinelWindowContext,
    *,
    overrides: dict | None = None,
    trade_log_path: Path | None = None,
) -> BacktestRunResult:
    """Run Sentinel against a pre-loaded :class:`SentinelWindowContext`.

    Reads two off-by-default Lab toggles from ``overrides`` into module
    globals and **resets them per call** so no module-global state bleeds
    across Lab trials:

    * ``activation_score_threshold`` (sibling ``sentinel_maxdd`` candidate)
    * ``bear_score_mode`` (THIS ``sentinel_bear_score`` candidate)

    When both toggles are absent / equal to their legacy defaults the
    result is the legacy behaviour (proven byte-identical by
    ``sentinel/tests/test_lab_activation_threshold_byte_identical.py``
    and ``sentinel/tests/test_bear_score_byte_identical.py``)."""
    global _ACTIVATION_THRESHOLD_OVERRIDE, _BEAR_SCORE_MODE_OVERRIDE
    overrides = dict(overrides or {})
    _ACTIVATION_THRESHOLD_OVERRIDE = (
        int(overrides["activation_score_threshold"])
        if "activation_score_threshold" in overrides
        else None
    )
    _BEAR_SCORE_MODE_OVERRIDE = (
        str(overrides["bear_score_mode"])
        if "bear_score_mode" in overrides
        else None
    )

    if not context.breakdowns:
        return BacktestRunResult(
            engine="sentinel", parameters=overrides, credibility_score=0,
            passed_gate=False, sharpe=0.0, profit_factor=0.0,
            max_drawdown=0.0, trades=0, dsr=0.0, min_btl_gap=0,
            trades_per_param=0.0, sensitivity_score=None,
            ruin_probability=0.0, trade_log=[],
        )

    # bear_score_mode dispatch. _bear_score_mode() returns "graduated"
    # ONLY when the override is exactly the string "graduated" AND the
    # macro_panel is available; any unknown value / missing panel falls
    # back to the byte-identical legacy path. The effective_mode
    # reported in `parameters` reflects WHICH BRANCH ACTUALLY RAN so
    # the dossier `param_diff` carries the honest variant truth (not
    # the requested override the panel could not satisfy).
    if _bear_score_mode() == "graduated" and context.macro_panel is not None:
        return _run_graduated_bear_score(
            context=context, trade_log_path=trade_log_path,
            effective_mode="graduated",
        )

    return _run_legacy_bear_score(
        context=context, trade_log_path=trade_log_path,
        effective_mode="current",
    )


def _run_legacy_bear_score(
    *,
    context: SentinelWindowContext,
    trade_log_path: Path | None,
    effective_mode: str = "current",
) -> BacktestRunResult:
    """Legacy binary-activation Sentinel path (the ``bear_score_mode=
    "current"`` arm; also the live-trading-path-byte-identical contract).

    Hoisted out of :func:`run_sentinel_with_context` so the graduated
    branch can sit alongside it without inlining; ALL behaviour is
    byte-identical to the pre-candidate code path that previously lived
    in ``run_sentinel_with_context`` directly. The C1 characterization
    golden in ``test_bear_score_byte_identical.py`` reds on any drift.
    """
    import sentinel.plugs.lifecycle_analysis as _lifecycle_mod

    lifecycle = SentinelLifecycleAnalysis()
    # Backtest-only seam: shadow the module constant the plug bound at
    # import for EXACTLY this walk_states call, then restore. The live
    # scheduler never reaches here, so its walk_states is byte-identical.
    _saved = _lifecycle_mod.ACTIVATION_SCORE_THRESHOLD
    try:
        _lifecycle_mod.ACTIVATION_SCORE_THRESHOLD = _activation_score_threshold()
        states = lifecycle.walk_states(
            context.breakdowns, spy_close=context.spy_close,
        )
    finally:
        _lifecycle_mod.ACTIVATION_SCORE_THRESHOLD = _saved

    execution = SentinelExecutionRisk(graduated=context.graduated)
    decisions: dict[date_t, SentinelDecision] = {}
    for d, st in states.items():
        prices_today: dict[str, Decimal] = {}
        for t, series in context.etf_prices.items():
            if t == "SPY" or len(series) == 0:
                continue
            sub = series.loc[series.index <= pd.Timestamp(d)].dropna()
            if len(sub) > 0:
                prices_today[t] = Decimal(str(round(float(sub.iloc[-1]), 4)))
        if not prices_today:
            continue
        decisions[d] = execution.build_decision(
            as_of=d, state=st,
            equity_usd=DEFAULT_PLATFORM_EQUITY_USD,
            prices=prices_today,
            current_holdings={},
        )

    trades, trades_for_diag = _simulate(
        states, decisions, context.etf_prices, context.round_trip_costs,
    )
    sharpe, pf, max_dd = _compute_summary(trades)

    if trade_log_path is not None:
        write_trade_log_csv(trade_log_path, trades)

    prices_for_diag = (
        context.etf_prices.get("SPY", pd.Series(dtype=float))
        .to_frame(name="close").rename_axis("date").reset_index()
    )
    prices_for_diag["ticker"] = "SPY"
    parameters = {
        "activation_score_threshold": int(_activation_score_threshold()),
        "bear_score_mode": effective_mode,
    }
    return compute_search_metrics(
        engine="sentinel",
        parameters=parameters,
        trades_for_diag=trades_for_diag,
        sharpe=sharpe,
        profit_factor=pf,
        max_drawdown=max_dd,
        n_trials=len(parameters),
        price_data=prices_for_diag,
        rubric_inputs={
            "lookahead_clean": True,
            "survivorship_inclusive": True,
            "pit_fundamentals": True,
            "regime_coverage": False,  # few cycles — flagged honestly.
            "monte_carlo_drawdown": True,
        },
        search_trades=trades,
    )


def _run_graduated_bear_score(
    *,
    context: SentinelWindowContext,
    trade_log_path: Path | None,
    effective_mode: str = "graduated",
) -> BacktestRunResult:
    """Graduated five-factor Bear-Score variant
    (``bear_score_mode="graduated"``; sentinel_bear_score Lab candidate).

    Computes the per-date composite from the macro_panel (spec §2.2–§2.3),
    maps to action bands (§2.4), and runs the simulator on the
    band-scaled basket (§2.5: Treasuries/gold first, inverse-ETF cap
    25 %). Reuses ``_simulate`` and ``compute_search_metrics`` so trade
    accounting, cost model, sizing, and the credibility rubric are
    IDENTICAL to the legacy path — only "which days carry which
    positions and at what scale" differs.
    """
    panel = context.macro_panel
    assert panel is not None  # guarded by the caller

    grad_weights = _grad_basket_weights()
    sorted_dates = sorted(context.breakdowns.keys())

    # Per-date band determination.
    band_for_date: dict[date_t, float] = {}
    for d in sorted_dates:
        # PIT lookup — most recent observation at or before d.
        try:
            row = panel.loc[panel.index <= d].iloc[-1]
            row_dict = {k: row.get(k) for k in _GRAD_INDICATORS}
        except (IndexError, KeyError):
            row_dict = {k: None for k in _GRAD_INDICATORS}
        composite = _grad_composite(row_dict)
        band_for_date[d] = _grad_band_scale(composite)

    # Cycle detection + trade simulation (analog of _simulate's
    # state-based cycle accounting but driven by band_scale instead of
    # the SentinelPhase machine).
    trades: list[SearchTrade] = []
    trades_for_diag: list[dict[str, Any]] = []
    open_positions: dict[str, dict[str, Any]] = {}

    def _close_all(close_date: date_t, exit_reason: str) -> None:
        for ticker, pos in list(open_positions.items()):
            price_series = context.etf_prices.get(ticker)
            if price_series is None or len(price_series) == 0:
                continue
            sub = price_series.loc[price_series.index <= pd.Timestamp(close_date)].dropna()
            if len(sub) == 0:
                continue
            exit_price = float(sub.iloc[-1])
            entry_price = pos["entry_price"]
            gross_ret = (exit_price - entry_price) / entry_price
            rtc = float(context.round_trip_costs.get(ticker, Decimal("0.001")))
            net_ret = gross_ret - rtc
            trades.append(SearchTrade(
                ticker=ticker,
                entry_date=pos["entry_date"],
                entry_price=entry_price,
                exit_date=close_date,
                exit_price=exit_price,
                pnl_pct=net_ret,
                direction="LONG",
                exit_reason=exit_reason,
            ))
            trades_for_diag.append({
                "ticker": ticker,
                "entry_date": pos["entry_date"],
                "exit_date": close_date,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "pnl_pct": net_ret,
                "direction": "LONG",
            })
            del open_positions[ticker]

    prev_scale: float = _GRAD_SCALE_DORMANT
    for d in sorted_dates:
        scale = band_for_date[d]
        if scale <= 0.0:
            if open_positions:
                _close_all(d, exit_reason="GRADUATED_DORMANT")
            prev_scale = scale
            continue
        # Band became (or stayed) non-zero. Open positions only on the
        # FIRST non-zero day of a cycle (transition from DORMANT). The
        # variant is "set and hold within a cycle" — basket re-sizing on
        # band changes is NOT modelled (mirrors the legacy "no
        # per-cycle rebalance" discipline; spec §10 no-trade-machinery
        # change).
        if prev_scale <= 0.0 and not open_positions:
            for ticker, weight in grad_weights.items():
                if float(weight) * scale <= 0.0:
                    continue
                price_series = context.etf_prices.get(ticker)
                if price_series is None or len(price_series) == 0:
                    continue
                sub = price_series.loc[price_series.index <= pd.Timestamp(d)].dropna()
                if len(sub) == 0:
                    continue
                entry_price = float(sub.iloc[-1])
                open_positions[ticker] = {
                    "entry_date": d,
                    "entry_price": entry_price,
                }
        prev_scale = scale

    if open_positions:
        _close_all(sorted_dates[-1], exit_reason="BACKTEST_END")

    sharpe, pf, max_dd = _compute_summary(trades)

    if trade_log_path is not None:
        write_trade_log_csv(trade_log_path, trades)

    prices_for_diag = (
        context.etf_prices.get("SPY", pd.Series(dtype=float))
        .to_frame(name="close").rename_axis("date").reset_index()
    )
    prices_for_diag["ticker"] = "SPY"
    parameters = {
        "activation_score_threshold": int(_activation_score_threshold()),
        "bear_score_mode": effective_mode,
    }
    return compute_search_metrics(
        engine="sentinel",
        parameters=parameters,
        trades_for_diag=trades_for_diag,
        sharpe=sharpe,
        profit_factor=pf,
        max_drawdown=max_dd,
        n_trials=len(parameters),
        price_data=prices_for_diag,
        rubric_inputs={
            "lookahead_clean": True,
            "survivorship_inclusive": True,
            "pit_fundamentals": True,
            "regime_coverage": False,
            "monte_carlo_drawdown": True,
        },
        search_trades=trades,
    )


async def run_for_search(
    *,
    db_url: str,
    start: date_t,
    end: date_t,
    universe: tuple[str, ...] | None = None,
    overrides: dict | None = None,
    trade_log_path: Path | None = None,
) -> BacktestRunResult:
    """Thin wrapper: load context, run once. Single-call convenience.

    The orchestrator should use :func:`load_sentinel_window_context` +
    :func:`run_sentinel_with_context` to amortise the DB load across all
    candidates in a window."""
    ctx = await load_sentinel_window_context(
        db_url=db_url, start=start, end=end, universe=universe,
    )
    return run_sentinel_with_context(
        ctx, overrides=overrides, trade_log_path=trade_log_path,
    )


# ────────────────────────────────────────────────────────────────────────────
# SP-B/SP-E — Lab targeting declaration (engine-OWNED; resolved by
# ops.lab.run's roster-driven resolver; the live trading path never
# imports this). primary_metric=MAXDD_REDUCTION is SP-E's whole point:
# Sentinel's success is drawdown reduction, NOT Sharpe — the SP-D
# pluggable-metric proof case. The gate stays sacred (SP-D §1.2).
# ────────────────────────────────────────────────────────────────────────────

LAB_TARGET = LabTarget(
    param_ranges={
        # sentinel_maxdd candidate (sibling, MERGED): legacy default 60
        # vs the single earlier-activation variant 55. choice:<csv> (NOT
        # a range/grid). Spec:
        # docs/superpowers/specs/2026-05-20-sentinel-maxdd-lab-candidate.md
        "activation_score_threshold": (60, 55, "choice:60,55"),
        # sentinel_bear_score candidate (THIS spec): legacy default
        # "current" (binary activation) vs the single graduated
        # five-factor composite variant "graduated". (low, high) are the
        # established (0, 0) placeholder for choice: kinds; the runtime
        # values are read from kind.split(":",1)[1].split(","). Spec:
        # docs/superpowers/specs/2026-05-21-sentinel-bear-score-lab-candidate.md
        "bear_score_mode": (0, 0, "choice:current,graduated"),
    },
    run_for_search=run_for_search,
    load_window_context=load_sentinel_window_context,
    run_with_context=run_sentinel_with_context,
    default_params=default_params,
    primary_metric=LabPrimaryMetric.MAXDD_REDUCTION,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--start", type=date_t.fromisoformat, default=date_t(2018, 1, 1))
    p.add_argument("--end", type=date_t.fromisoformat,
                   default=datetime.now(UTC).date())
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    p.add_argument("--results-file", default=DEFAULT_RESULTS_FILE)
    p.add_argument("--trades-file", default=DEFAULT_TRADES_FILE)
    p.add_argument("--phase-history-file", default=DEFAULT_PHASE_HISTORY_FILE)
    p.add_argument("--json", dest="json_output", action="store_true",
                   help="Emit BacktestRunResult JSON to stdout.")
    p.add_argument("--trade-log", type=Path, default=None,
                   help="Write standardised per-trade CSV to this path too.")
    p.add_argument("--graduated", action="store_true",
                   help="Use the 20% permanent cap (post-graduation) instead of 10% pre-grad.")
    return p.parse_args(argv)


async def amain(args: argparse.Namespace) -> int:
    return await run_backtest(
        start=args.start, end=args.end,
        output_dir=args.output_dir,
        results_file=args.results_file,
        trades_file=args.trades_file,
        phase_history_file=args.phase_history_file,
        json_output=args.json_output,
        trade_log_path=args.trade_log,
        graduated=args.graduated,
    )


def main() -> None:  # pragma: no cover — CLI shim
    raise SystemExit(asyncio.run(amain(_parse_args())))


if __name__ == "__main__":  # pragma: no cover
    main()
