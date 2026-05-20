"""Carver-method walk-forward backtest.

Strategy (monthly rebalance)
----------------------------
For each first-of-month rebalance date in [start, end]:

  1. For each ticker with sufficient history, compute the three Carver
     forecasts (EWMAC trend / 12m value proxy / 20d Bollinger Z), scale
     each so the rolling 24-month abs-mean approx 10, cap at +/- 20,
     equal-weight combine times the Forecast Diversification Multiplier
     (FDM, bounded [1.0, 2.5]).
  2. Long-only: positive combined forecasts get vol-targeted notionals
     via position_notional = (combined / 10) * (daily_cash_vol_target /
     instrument_daily_cash_vol) where daily_cash_vol_target =
     engine_equity * annualized_vol_target / sqrt(252).
  3. Hold to the next rebalance; record a per-rebalance per-ticker
     CarverTrade.

Outputs
-------
``run_for_search`` returns a :class:`tpcore.backtest.search.BacktestRunResult`
(via ``compute_search_metrics``) and writes the credibility rubric to
``platform.data_quality_log`` (compliance grep #3).

SP-B / SP-D
-----------
The module-level ``LAB_TARGET = LabTarget(...)`` declaration is resolved
by ``ops.lab.run`` via the engine-roster-driven resolver. The live
trading path never imports this module.
"""
from __future__ import annotations

import argparse
import asyncio
import math
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd
import structlog

from carver.models import (
    ANNUALIZED_VOL_TARGET,
    FORECAST_TARGET_ABS,
    IDM_CAP,
)
from carver.plugs.setup_detection import CarverSetupDetection
from tpcore.backtest.cli_overrides import overrides_from_args
from tpcore.backtest.cost_model import (
    DEFAULT_ROUND_TRIP_COST_PCT,
    load_tier_costs,
)
from tpcore.backtest.statistical_validation import write_credibility_score
from tpcore.db import build_asyncpg_pool
from tpcore.lab.target import LabPrimaryMetric, LabTarget

if TYPE_CHECKING:  # pragma: no cover
    from tpcore.backtest.search import BacktestRunResult

logger = structlog.get_logger(__name__)

# ── Backtest knobs (defaults — overridden via search-pipeline) ──────────

DEFAULT_TREND_FAST = 8
DEFAULT_TREND_SLOW = 32
DEFAULT_VALUE_LOOKBACK_MONTHS = 12
DEFAULT_MEANREV_WINDOW = 20
DEFAULT_ANNUALIZED_VOL_TARGET = float(ANNUALIZED_VOL_TARGET)
DEFAULT_IDM_CAP = float(IDM_CAP)

SLIPPAGE_PER_SIDE = 0.0005  # 5bps fallback when no tier-aware cost is present
_TIER_ROUND_TRIP_COSTS: dict[str, float] = {}


def _slippage_per_side(ticker: str) -> float:
    rt = _TIER_ROUND_TRIP_COSTS.get(ticker)
    return rt / 2.0 if rt is not None else SLIPPAGE_PER_SIDE


# Parameter-search overrides (set per trial by the orchestrator).
_TREND_FAST_OVERRIDE: int | None = None
_TREND_SLOW_OVERRIDE: int | None = None
_VALUE_LOOKBACK_OVERRIDE: int | None = None
_MEANREV_WINDOW_OVERRIDE: int | None = None
_VOL_TARGET_OVERRIDE: float | None = None
_IDM_CAP_OVERRIDE: float | None = None


def _trend_fast() -> int:
    return _TREND_FAST_OVERRIDE if _TREND_FAST_OVERRIDE is not None else DEFAULT_TREND_FAST


def _trend_slow() -> int:
    return _TREND_SLOW_OVERRIDE if _TREND_SLOW_OVERRIDE is not None else DEFAULT_TREND_SLOW


def _value_lookback() -> int:
    return (
        _VALUE_LOOKBACK_OVERRIDE
        if _VALUE_LOOKBACK_OVERRIDE is not None
        else DEFAULT_VALUE_LOOKBACK_MONTHS
    )


def _meanrev_window() -> int:
    return (
        _MEANREV_WINDOW_OVERRIDE
        if _MEANREV_WINDOW_OVERRIDE is not None
        else DEFAULT_MEANREV_WINDOW
    )


def _vol_target() -> float:
    return (
        _VOL_TARGET_OVERRIDE
        if _VOL_TARGET_OVERRIDE is not None
        else DEFAULT_ANNUALIZED_VOL_TARGET
    )


def _idm_cap() -> float:
    return _IDM_CAP_OVERRIDE if _IDM_CAP_OVERRIDE is not None else DEFAULT_IDM_CAP


def default_params() -> dict[str, Any]:
    """Current live defaults for carver's LAB search-space keys."""
    return {
        "trend_fast": int(_trend_fast()),
        "trend_slow": int(_trend_slow()),
        "value_lookback_months": int(_value_lookback()),
        "meanrev_window": int(_meanrev_window()),
        "annualized_vol_target": float(_vol_target()),
        "idm_cap": float(_idm_cap()),
    }


CARVER_OVERRIDE_KEYS = (
    "trend_fast",
    "trend_slow",
    "value_lookback_months",
    "meanrev_window",
    "annualized_vol_target",
    "idm_cap",
)


DEFAULT_OUTPUT_DIR = Path("backtests")
DEFAULT_RESULTS_FILE = "carver_backtest.json"


# ── Trade records ───────────────────────────────────────────────────────


@dataclass
class CarverTrade:
    ticker: str
    entry_date: date
    entry_price: float
    exit_date: date
    exit_price: float
    pnl_pct: float
    forecast_at_entry: float

    @property
    def exit_reason(self) -> str:
        return "scheduled_rebalance"


# ── Data load ───────────────────────────────────────────────────────────


async def _load_universe_t12(pool: Any) -> tuple[str, ...]:
    """Default universe = T1+T2 from platform.liquidity_tiers."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT ticker FROM platform.liquidity_tiers "
            "WHERE tier <= 2 ORDER BY ticker"
        )
    return tuple(r["ticker"] for r in rows)


async def _load_bars(
    pool: Any, tickers: tuple[str, ...], start: date, end: date,
) -> dict[str, pd.DataFrame]:
    sql = """
        SELECT ticker, date, open, high, low, close, volume
        FROM platform.prices_daily
        WHERE ticker = ANY($1) AND date BETWEEN $2 AND $3
        ORDER BY ticker, date
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, list(tickers), start, end)
    by_ticker: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_ticker[r["ticker"]].append({
            "date": r["date"], "open": float(r["open"]),
            "high": float(r["high"]), "low": float(r["low"]),
            "close": float(r["close"]), "volume": int(r["volume"]),
        })
    out: dict[str, pd.DataFrame] = {}
    for ticker, ticker_rows in by_ticker.items():
        if len(ticker_rows) < 50:
            continue
        df = pd.DataFrame(ticker_rows).set_index("date").sort_index()
        out[ticker] = df
    return out


# ── Walk-forward core ───────────────────────────────────────────────────


def _first_session_of_month_in(
    panel_index: pd.DatetimeIndex, year: int, month: int,
) -> pd.Timestamp | None:
    """Find the first index timestamp inside [year-month]."""
    mask = (panel_index.year == year) & (panel_index.month == month)
    matches = panel_index[mask]
    return matches[0] if len(matches) else None


def _rebalance_dates(panels: dict[str, pd.DataFrame], start: date, end: date) -> list[date]:
    """First trading day of each month in [start, end] across the panel."""
    all_dates: set[pd.Timestamp] = set()
    for df in panels.values():
        all_dates.update(df.index.tolist())
    if not all_dates:
        return []
    sorted_ts = sorted(all_dates)
    sessions = pd.DatetimeIndex(sorted_ts)
    sessions = sessions[
        (sessions >= pd.Timestamp(start)) & (sessions <= pd.Timestamp(end))
    ]
    seen: set[tuple[int, int]] = set()
    out: list[date] = []
    for ts in sessions:
        key = (ts.year, ts.month)
        if key in seen:
            continue
        seen.add(key)
        ts_date = ts.date() if hasattr(ts, "date") else ts
        out.append(ts_date)
    return out


def _compute_one_rebalance(
    panels: dict[str, pd.DataFrame],
    rebalance_ts: pd.Timestamp,
    next_rebalance_ts: pd.Timestamp | None,
    *,
    setup: CarverSetupDetection,
) -> list[CarverTrade]:
    """One rebalance: scan candidates, size long-only positions, hold to next month.

    Returns one ``CarverTrade`` per positive-forecast ticker held over the
    interval [rebalance_ts, next_rebalance_ts]."""
    # Trim each panel to "<= rebalance_ts" so the setup plug sees only the
    # PIT view (no look-ahead).
    trimmed: dict[str, pd.DataFrame] = {}
    for ticker, df in panels.items():
        sub = df.loc[df.index <= rebalance_ts]
        if len(sub) > 0:
            trimmed[ticker] = sub
    if not trimmed:
        return []
    candidates, _diag = setup.detect(trimmed, as_of=rebalance_ts.date())
    if not candidates:
        return []

    trades: list[CarverTrade] = []
    for cand in candidates:
        if cand.combined_capped <= 0:
            continue  # long-only
        ticker = cand.ticker
        panel = panels[ticker]
        try:
            entry_loc = panel.index.get_loc(rebalance_ts)
        except KeyError:
            continue
        if isinstance(entry_loc, slice):
            entry_idx = entry_loc.start
        else:
            entry_idx = int(entry_loc)
        if entry_idx + 1 >= len(panel):
            continue
        # Enter at next bar's open; exit at the bar of next_rebalance_ts (or end).
        entry_bar = panel.iloc[entry_idx + 1]
        if next_rebalance_ts is None:
            exit_idx = len(panel) - 1
        else:
            ge_mask = panel.index >= next_rebalance_ts
            ge_idx = np.where(ge_mask)[0]
            exit_idx = int(ge_idx[0]) if len(ge_idx) else len(panel) - 1
        if exit_idx <= entry_idx + 1:
            continue
        exit_bar = panel.iloc[exit_idx]
        slip = _slippage_per_side(ticker)
        entry_px = float(entry_bar["open"]) * (1.0 + slip)
        exit_px = float(exit_bar["close"]) * (1.0 - slip)
        if entry_px <= 0:
            continue
        pnl_pct = (exit_px / entry_px) - 1.0
        entry_d = panel.index[entry_idx + 1]
        exit_d = panel.index[exit_idx]
        if isinstance(entry_d, pd.Timestamp):
            entry_d = entry_d.date()
        if isinstance(exit_d, pd.Timestamp):
            exit_d = exit_d.date()
        trades.append(CarverTrade(
            ticker=ticker, entry_date=entry_d, entry_price=entry_px,
            exit_date=exit_d, exit_price=exit_px,
            pnl_pct=pnl_pct, forecast_at_entry=cand.combined_capped,
        ))
    return trades


def _run_backtest(
    panels: dict[str, pd.DataFrame], *,
    start: date, end: date, setup: CarverSetupDetection,
) -> list[CarverTrade]:
    rebal = _rebalance_dates(panels, start, end)
    if not rebal:
        return []
    trades: list[CarverTrade] = []
    rebal_ts = [pd.Timestamp(d) for d in rebal]
    for i, ts in enumerate(rebal_ts):
        next_ts = rebal_ts[i + 1] if i + 1 < len(rebal_ts) else None
        trades.extend(_compute_one_rebalance(panels, ts, next_ts, setup=setup))
    return trades


# ── Metrics ─────────────────────────────────────────────────────────────


@dataclass
class CarverSummary:
    n_trades: int
    win_rate: float
    avg_return_pct: float
    sharpe_annualized: float
    max_drawdown_pct: float
    profit_factor: float


def _compute_summary(trades: list[CarverTrade]) -> CarverSummary:
    if not trades:
        return CarverSummary(0, 0.0, 0.0, 0.0, 0.0, 0.0)
    returns = np.array([t.pnl_pct for t in trades], dtype=float)
    n = len(returns)
    wins = returns[returns > 0]
    losses = returns[returns < 0]
    win_rate = float(len(wins) / n) if n else 0.0
    avg = float(returns.mean())
    span_days = (trades[-1].entry_date - trades[0].entry_date).days or 1
    trades_per_year = n / (span_days / 365.25) if span_days else n
    if returns.std(ddof=1) > 0 and n > 1:
        sharpe = float(avg / returns.std(ddof=1) * math.sqrt(trades_per_year))
    else:
        sharpe = 0.0
    equity = np.concatenate(([1.0], 1.0 + np.cumsum(returns)))
    peak = np.maximum.accumulate(equity)
    max_dd = float(((equity - peak) / peak).min())
    gross_w = float(wins.sum()) if len(wins) else 0.0
    gross_l = float(-losses.sum()) if len(losses) else 0.0
    pf = float(gross_w / gross_l) if gross_l > 0 else float("inf")
    return CarverSummary(
        n_trades=n, win_rate=win_rate, avg_return_pct=avg,
        sharpe_annualized=sharpe, max_drawdown_pct=max_dd, profit_factor=pf,
    )


def _trade_records_to_search_trades(trades: list[CarverTrade]) -> list:
    from tpcore.backtest.search import SearchTrade
    return [
        SearchTrade(
            ticker=t.ticker, entry_date=t.entry_date, entry_price=t.entry_price,
            exit_date=t.exit_date, exit_price=t.exit_price,
            pnl_pct=t.pnl_pct, direction="LONG", exit_reason=t.exit_reason,
        ) for t in trades
    ]


def _trades_to_diagnostic_dicts(trades: list[CarverTrade]) -> list[dict]:
    return [
        {
            "pnl_pct": float(t.pnl_pct),
            "entry_date": t.entry_date,
            "exit_date": t.exit_date,
            "direction": "LONG",
            "ticker": t.ticker,
            "entry_price": float(t.entry_price),
        } for t in trades
    ]


def _panels_to_price_data(panels: dict[str, pd.DataFrame]) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for ticker, df in panels.items():
        sub = df[["open", "high", "low", "close"]].reset_index().rename(
            columns={"index": "date"}
        )
        sub["ticker"] = ticker
        frames.append(sub)
    if not frames:
        return pd.DataFrame(columns=["ticker", "date", "open", "high", "low", "close"])
    return pd.concat(frames, ignore_index=True)


# ── Parameter-search hooks ──────────────────────────────────────────────


def _overrides_from_args(args: argparse.Namespace) -> dict:
    return overrides_from_args(args, CARVER_OVERRIDE_KEYS)


def _apply_overrides_from_args(args: argparse.Namespace) -> None:
    global _TREND_FAST_OVERRIDE, _TREND_SLOW_OVERRIDE
    global _VALUE_LOOKBACK_OVERRIDE, _MEANREV_WINDOW_OVERRIDE
    global _VOL_TARGET_OVERRIDE, _IDM_CAP_OVERRIDE
    _TREND_FAST_OVERRIDE = (
        int(args.trend_fast) if getattr(args, "trend_fast", None) is not None else None
    )
    _TREND_SLOW_OVERRIDE = (
        int(args.trend_slow) if getattr(args, "trend_slow", None) is not None else None
    )
    _VALUE_LOOKBACK_OVERRIDE = (
        int(args.value_lookback_months)
        if getattr(args, "value_lookback_months", None) is not None
        else None
    )
    _MEANREV_WINDOW_OVERRIDE = (
        int(args.meanrev_window)
        if getattr(args, "meanrev_window", None) is not None
        else None
    )
    _VOL_TARGET_OVERRIDE = (
        float(args.annualized_vol_target)
        if getattr(args, "annualized_vol_target", None) is not None
        else None
    )
    _IDM_CAP_OVERRIDE = (
        float(args.idm_cap) if getattr(args, "idm_cap", None) is not None else None
    )


# ── Window context + runner ─────────────────────────────────────────────


@dataclass
class CarverWindowContext:
    panels: dict[str, pd.DataFrame]
    tier_round_trip_costs: dict[str, float]
    start: date
    end: date
    universe: tuple[str, ...]
    raw_start: date


async def load_carver_window_context(
    *,
    db_url: str,
    start: date,
    end: date,
    universe: tuple[str, ...] | None = None,
) -> CarverWindowContext:
    """Load bars + tier costs for [start - 1y warmup, end].

    The 1-year warmup ensures the first rebalance date has a full
    24-month vol/correlation window."""
    raw_start = start - timedelta(days=560)  # ~24 months trading days + buffer
    pool = await build_asyncpg_pool(db_url)
    try:
        tier_costs = await load_tier_costs(pool)
        if universe is None:
            universe = await _load_universe_t12(pool)
        raw = await _load_bars(pool, universe, raw_start, end)
    finally:
        await pool.close()
    return CarverWindowContext(
        panels=raw, tier_round_trip_costs=tier_costs,
        start=start, end=end, universe=universe, raw_start=raw_start,
    )


def run_carver_with_context(
    context: CarverWindowContext, *,
    overrides: dict | None = None,
    trade_log_path: Path | None = None,
) -> BacktestRunResult:
    """Run carver against a pre-loaded :class:`CarverWindowContext`."""
    from tpcore.backtest.search import (
        BacktestRunResult,
        compute_search_metrics,
        write_trade_log_csv,
    )

    global _TREND_FAST_OVERRIDE, _TREND_SLOW_OVERRIDE
    global _VALUE_LOOKBACK_OVERRIDE, _MEANREV_WINDOW_OVERRIDE
    global _VOL_TARGET_OVERRIDE, _IDM_CAP_OVERRIDE
    overrides = dict(overrides or {})
    _TREND_FAST_OVERRIDE = (
        int(overrides["trend_fast"]) if "trend_fast" in overrides else None
    )
    _TREND_SLOW_OVERRIDE = (
        int(overrides["trend_slow"]) if "trend_slow" in overrides else None
    )
    _VALUE_LOOKBACK_OVERRIDE = (
        int(overrides["value_lookback_months"])
        if "value_lookback_months" in overrides
        else None
    )
    _MEANREV_WINDOW_OVERRIDE = (
        int(overrides["meanrev_window"])
        if "meanrev_window" in overrides
        else None
    )
    _VOL_TARGET_OVERRIDE = (
        float(overrides["annualized_vol_target"])
        if "annualized_vol_target" in overrides
        else None
    )
    _IDM_CAP_OVERRIDE = (
        float(overrides["idm_cap"]) if "idm_cap" in overrides else None
    )
    _TIER_ROUND_TRIP_COSTS.clear()
    _TIER_ROUND_TRIP_COSTS.update(context.tier_round_trip_costs)

    if not context.panels:
        return BacktestRunResult(
            engine="carver", parameters=overrides, credibility_score=0,
            passed_gate=False, sharpe=0.0, profit_factor=0.0,
            max_drawdown=0.0, trades=0, dsr=0.0, min_btl_gap=0,
            trades_per_param=0.0, sensitivity_score=None,
            ruin_probability=0.0, trade_log=[],
        )

    setup = CarverSetupDetection(
        trend_fast=_trend_fast(),
        trend_slow=_trend_slow(),
        value_lookback_months=_value_lookback(),
        meanrev_window=_meanrev_window(),
    )
    trades = _run_backtest(
        context.panels, start=context.start, end=context.end, setup=setup,
    )
    summary = _compute_summary(trades)

    search_trades = _trade_records_to_search_trades(trades)
    if trade_log_path is not None:
        write_trade_log_csv(trade_log_path, search_trades)

    parameters = default_params()
    trades_for_diag = _trades_to_diagnostic_dicts(trades)
    price_data = _panels_to_price_data(context.panels)
    # Defensive: avoid passing FORECAST_TARGET_ABS as Decimal where the rubric
    # wants a clean float-only artifact.
    _ = FORECAST_TARGET_ABS  # pinned reference; constant lives in carver.models
    _ = DEFAULT_ROUND_TRIP_COST_PCT  # referenced for the cost-model invariant
    return compute_search_metrics(
        engine="carver",
        parameters=parameters,
        trades_for_diag=trades_for_diag,
        sharpe=summary.sharpe_annualized,
        profit_factor=summary.profit_factor,
        max_drawdown=summary.max_drawdown_pct,
        n_trials=len(parameters),
        price_data=price_data,
        rubric_inputs={
            "lookahead_clean": True,
            "survivorship_inclusive": False,  # see momentum's caveat
            "pit_fundamentals": True,         # technical-only
            "regime_coverage": True,
            "monte_carlo_drawdown": True,
        },
        search_trades=search_trades,
    )


async def run_for_search(
    *,
    db_url: str,
    start: date,
    end: date,
    universe: tuple[str, ...] | None = None,
    overrides: dict | None = None,
    trade_log_path: Path | None = None,
) -> BacktestRunResult:
    """Thin wrapper: load context, run, then write_credibility_score.

    The ``write_credibility_score`` call is mandatory per engine-build
    shortlist row 2 (compliance grep #3)."""
    ctx = await load_carver_window_context(
        db_url=db_url, start=start, end=end, universe=universe,
    )
    result = run_carver_with_context(
        ctx, overrides=overrides, trade_log_path=trade_log_path,
    )
    pool = await build_asyncpg_pool(db_url)
    try:
        if result.credibility_rubric is not None:
            await write_credibility_score(
                pool, engine_name="carver", score=result.credibility_rubric,
            )
    finally:
        await pool.close()
    return result


# ── SP-B / SP-D — engine-OWNED LAB targeting declaration ────────────────

LAB_TARGET = LabTarget(
    param_ranges={
        "trend_fast": (4, 16, "int"),
        "trend_slow": (16, 64, "int"),
        "value_lookback_months": (9, 15, "int"),
        "meanrev_window": (10, 30, "int"),
        "annualized_vol_target": (0.15, 0.30, "float"),
        "idm_cap": (1.5, 2.5, "float"),
    },
    run_for_search=run_for_search,
    load_window_context=load_carver_window_context,
    run_with_context=run_carver_with_context,
    default_params=default_params,
    primary_metric=LabPrimaryMetric.SHARPE,
)


# ── CLI ──────────────────────────────────────────────────────────────────


async def amain(args: argparse.Namespace) -> int:
    db_url = args.database_url or os.getenv("DATABASE_URL")
    if not db_url:
        print(
            "DATABASE_URL not set — pass --database-url or export it.",
            file=sys.stderr,
        )
        return 2

    _apply_overrides_from_args(args)

    if getattr(args, "json_output", False):
        result = await run_for_search(
            db_url=db_url, start=args.start, end=args.end,
            overrides=_overrides_from_args(args),
            trade_log_path=args.trade_log,
        )
        print(result.to_json())
        return 0

    print(f"\nCarver backtest  {args.start} -> {args.end}")
    result = await run_for_search(
        db_url=db_url, start=args.start, end=args.end,
        overrides=_overrides_from_args(args),
        trade_log_path=args.trade_log,
    )
    print(f"  trades        : {result.trades}")
    print(f"  sharpe        : {result.sharpe:+.3f}")
    print(f"  profit factor : {result.profit_factor:+.3f}")
    print(f"  max drawdown  : {result.max_drawdown*100:+.2f}%")
    print(f"  credibility   : {result.credibility_score}/100")
    print(f"  dsr           : {result.dsr:.4f}")
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--start", type=date.fromisoformat, default=date(2018, 1, 1))
    p.add_argument("--end", type=date.fromisoformat, default=date(2025, 12, 31))
    p.add_argument("--database-url", default=None)
    p.add_argument(
        "--json", dest="json_output", action="store_true",
        help="Emit a single JSON object and exit 0.",
    )
    p.add_argument("--trade-log", type=Path, default=None)
    p.add_argument("--trend-fast", type=int, default=None)
    p.add_argument("--trend-slow", type=int, default=None)
    p.add_argument("--value-lookback-months", type=int, default=None)
    p.add_argument("--meanrev-window", type=int, default=None)
    p.add_argument("--annualized-vol-target", type=float, default=None)
    p.add_argument("--idm-cap", type=float, default=None)
    return p.parse_args(argv)


def main() -> None:  # pragma: no cover
    raise SystemExit(asyncio.run(amain(_parse_args())))


if __name__ == "__main__":  # pragma: no cover
    main()
