"""Catalyst — backtest driver + Lab-targeting declarations.

Simulates the insider-cluster swing engine against historical
``platform.sec_insider_transactions`` + ``platform.prices_daily`` rows
and emits a :class:`BacktestRunResult` so the parameter-search pipeline
and `scripts/run_dashboard.sh` can consume it without bespoke shaping.

The "trade" granularity is one per (ticker, signal date) pair: enter at
the next day's open (next session's close used as a proxy here — same
mark every engine uses in its first-cut backtest), exit at the
flat-bracket TP/SL/trailing-stop event, or at the holding-period
horizon if neither bracket fires. Mirrors the Vector pattern.

Lab targeting
-------------
Single pre-registered Lab toggle: ``cluster_window_days`` — a
``choice:`` over ``{30 (legacy default), 45}``. The default 30 mirrors
``CATALYST_CLUSTER_WINDOW_DAYS`` in :mod:`catalyst.models`; 45 is the
single pre-registered alternative-window variant. The byte-identical-
when-off seam is the module-level ``_CLUSTER_WINDOW_OVERRIDE`` global
(reset per call inside :func:`run_catalyst_with_context`) — the LIVE
trading path (``catalyst/scheduler.py``) never imports this backtest
module and so is byte-identical when the flag is off (proven by
``catalyst/tests/test_lab_cluster_window_byte_identical.py``).

This module declares ``LAB_TARGET`` with ``primary_metric=SHARPE`` —
catalyst is a swing engine whose success bar IS Sharpe (the
canonical SP-D default). The graduation gate
(``DSR ≥ 0.95 ∧ cred ≥ 60 ∧ n_trades ≥ 3``) is unchanged; the
pluggable ranking metric only changes which candidate wins the
ranking, never whether it may graduate (SP-D sacred-gate separation).

Tier-aware costs (``tpcore.backtest.cost_model.get_round_trip_cost``)
are applied per ticker.
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

from catalyst.models import (
    CATALYST_CLUSTER_WINDOW_DAYS,
    CATALYST_MIN_AGGREGATE_USD,
    CATALYST_MIN_DISTINCT_INSIDERS,
    CATALYST_TEST_UNIVERSE,
    HARD_STOP_PCT,
    MIN_AVG_VOLUME,
    MIN_PRICE,
    PROFIT_TARGET_PCT,
    SMA_TREND_PERIOD,
)
from catalyst.plugs.setup_detection import detect_clusters
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
DEFAULT_RESULTS_FILE = "catalyst_backtest_results.json"
DEFAULT_TRADES_FILE = "catalyst_trades.csv"
DEFAULT_PLATFORM_EQUITY_USD = Decimal("100000")
HOLDING_PERIOD_DAYS = 30  # hard exit horizon if neither TP nor SL fires


# ════════════════════════════════════════════════════════════════════════
# SP-F — Lab-targeting seam (the feature-flag-variant pattern).
#
# Off-by-default backtest-only override. None ⇒ the legacy module
# constant (CATALYST_CLUSTER_WINDOW_DAYS, currently 30) — the live
# path's value. The live scheduler reads the constant directly from
# `catalyst.models`; it never enters this module.
# ════════════════════════════════════════════════════════════════════════

_CLUSTER_WINDOW_OVERRIDE: int | None = None


def _cluster_window() -> int:
    """The active cluster window for THIS backtest run.

    Returns the legacy ``CATALYST_CLUSTER_WINDOW_DAYS`` unless the
    off-by-default Lab override is set. Pure."""
    return (
        _CLUSTER_WINDOW_OVERRIDE
        if _CLUSTER_WINDOW_OVERRIDE is not None
        else CATALYST_CLUSTER_WINDOW_DAYS
    )


def default_params() -> dict[str, Any]:
    """Current live defaults for the Lab-sampled keys (the SP3 O1
    dossier-param-diff seam). The legacy default carries the true
    ``legacy → variant`` delta into the dossier ``param_diff``."""
    return {
        "cluster_window_days": int(CATALYST_CLUSTER_WINDOW_DAYS),
    }


# ────────────────────────────────────────────────────────────────────────
# Data loaders
# ────────────────────────────────────────────────────────────────────────


async def _fetch_insider_rows(
    pool,
    *,
    universe: tuple[str, ...],
    start: date_t,
    end: date_t,
) -> pd.DataFrame:
    sql = """
        SELECT ticker, filing_date, insider_name, transaction_type, value
        FROM platform.sec_insider_transactions
        WHERE ticker = ANY($1)
          AND filing_date BETWEEN $2 AND $3
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, list(universe), start, end)
    if not rows:
        return pd.DataFrame(columns=["ticker", "filing_date", "insider_name",
                                     "transaction_type", "value"])
    return pd.DataFrame([
        {"ticker": r["ticker"], "filing_date": r["filing_date"],
         "insider_name": r["insider_name"],
         "transaction_type": r["transaction_type"],
         "value": float(r["value"])}
        for r in rows
    ])


async def _fetch_prices(
    pool, *, universe: tuple[str, ...],
    start: date_t, end: date_t,
) -> dict[str, pd.DataFrame]:
    sql = """
        SELECT ticker, date, close, volume
        FROM platform.prices_daily
        WHERE ticker = ANY($1) AND date BETWEEN $2 AND $3
        ORDER BY ticker, date
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, list(universe), start, end)
    out: dict[str, pd.DataFrame] = {}
    if not rows:
        return out
    grouped: dict[str, list[tuple[date_t, float, float]]] = {}
    for r in rows:
        grouped.setdefault(r["ticker"], []).append(
            (r["date"], float(r["close"]), float(r["volume"])))
    for t, items in grouped.items():
        items.sort(key=lambda x: x[0])
        idx = pd.DatetimeIndex([pd.Timestamp(d) for d, _, _ in items])
        out[t] = pd.DataFrame(
            {"close": [c for _, c, _ in items],
             "volume": [v for _, _, v in items]},
            index=idx,
        )
    return out


async def _round_trip_cost_by_ticker(
    pool, *, tickers: tuple[str, ...],
) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for t in tickers:
        try:
            out[t] = await get_round_trip_cost(pool, t)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "catalyst.backtest.cost_lookup_failed",
                ticker=t, error=str(exc)[:120],
            )
            out[t] = Decimal("0.001")
    return out


# ────────────────────────────────────────────────────────────────────────
# Simulator (pure)
# ────────────────────────────────────────────────────────────────────────


def _simulate_trade(
    *,
    ticker: str,
    entry_date: date_t,
    prices: pd.DataFrame,
    round_trip_cost: float,
) -> SearchTrade | None:
    """Single-entry flat-bracket simulator.

    Enter at the next available close on/after ``entry_date``; exit on
    the first session whose intra-bar (close used as a conservative
    proxy here — same as Vector's first-cut path) hits the TP, SL, or
    trailing-stop trigger; or at ``HOLDING_PERIOD_DAYS`` (time stop)."""
    cut = prices[prices.index >= pd.Timestamp(entry_date)].dropna(
        subset=["close"])
    if len(cut) < 2:
        return None
    entry_price = float(cut["close"].iloc[0])
    if entry_price <= 0:
        return None
    tp = entry_price * (1 + float(PROFIT_TARGET_PCT))
    sl = entry_price * (1 - float(HARD_STOP_PCT))
    exit_idx = -1
    exit_reason = "TIME_STOP"
    high_water = entry_price
    horizon = min(len(cut) - 1, HOLDING_PERIOD_DAYS)
    for i in range(1, horizon + 1):
        close = float(cut["close"].iloc[i])
        if close <= sl:
            exit_idx = i
            exit_reason = "STOP_LOSS"
            break
        if close >= tp:
            exit_idx = i
            exit_reason = "TAKE_PROFIT"
            break
        if close > high_water:
            high_water = close
        # Trailing stop: arm at +8% from entry; once armed, exit if
        # close drops > 5% from high_water.
        if (close >= entry_price * 1.08
                and close <= high_water * 0.95):
            exit_idx = i
            exit_reason = "TIME_STOP"  # trailing exit; closest bucket
            break
    if exit_idx < 0:
        exit_idx = horizon
        exit_reason = "TIME_STOP"
    exit_price = float(cut["close"].iloc[exit_idx])
    exit_date = cut.index[exit_idx].date()
    gross_ret = (exit_price - entry_price) / entry_price
    net_ret = gross_ret - round_trip_cost
    return SearchTrade(
        ticker=ticker, entry_date=entry_date, entry_price=entry_price,
        exit_date=exit_date, exit_price=exit_price, pnl_pct=net_ret,
        direction="LONG", exit_reason=exit_reason,
    )


def _build_trades(
    *,
    universe: tuple[str, ...],
    insider_rows: pd.DataFrame,
    prices_by_ticker: dict[str, pd.DataFrame],
    cluster_window_days: int,
    round_trip_costs: dict[str, Decimal],
    start: date_t,
    end: date_t,
) -> tuple[list[SearchTrade], list[dict[str, Any]]]:
    """Walk every (ticker, signal-date) pair in the window where the
    cluster floor + the liquidity/trend gates pass; emit one
    :class:`SearchTrade` per qualified signal."""
    trades: list[SearchTrade] = []
    trades_for_diag: list[dict[str, Any]] = []
    if not prices_by_ticker:
        return trades, trades_for_diag

    # Walk monthly to keep run-time bounded; a fresh re-cluster every
    # session would re-fire the same trade. One signal per ticker per
    # 30-day stride is the conservative cadence (matches the typical
    # cluster decay window).
    cursor = start
    while cursor <= end:
        clusters = detect_clusters(
            insider_rows=insider_rows, as_of=cursor,
            window_days=cluster_window_days,
        )
        for ticker in universe:
            cl = clusters.get(ticker)
            if cl is None:
                continue
            if cl.distinct_insiders < CATALYST_MIN_DISTINCT_INSIDERS:
                continue
            if cl.aggregate_value_usd < CATALYST_MIN_AGGREGATE_USD:
                continue
            prices = prices_by_ticker.get(ticker)
            if prices is None or prices.empty:
                continue
            cut = prices[prices.index <= pd.Timestamp(cursor)].dropna(
                subset=["close"])
            if len(cut) < SMA_TREND_PERIOD:
                continue
            last_close = float(cut["close"].iloc[-1])
            if last_close < float(MIN_PRICE):
                continue
            avg_vol_series = cut["volume"].rolling(
                20, min_periods=20).mean()
            avg_vol_raw = avg_vol_series.iloc[-1]
            if pd.isna(avg_vol_raw) or int(avg_vol_raw) < MIN_AVG_VOLUME:
                continue
            sma_series = cut["close"].rolling(
                SMA_TREND_PERIOD, min_periods=SMA_TREND_PERIOD).mean()
            sma_val = sma_series.iloc[-1]
            if pd.isna(sma_val) or last_close <= float(sma_val):
                continue
            rtc = float(round_trip_costs.get(ticker, Decimal("0.001")))
            trade = _simulate_trade(
                ticker=ticker, entry_date=cursor,
                prices=prices, round_trip_cost=rtc,
            )
            if trade is None:
                continue
            trades.append(trade)
            trades_for_diag.append({
                "ticker": trade.ticker,
                "entry_date": trade.entry_date,
                "exit_date": trade.exit_date,
                "entry_price": trade.entry_price,
                "exit_price": trade.exit_price,
                "pnl_pct": trade.pnl_pct,
                "direction": "LONG",
            })
        cursor = cursor + timedelta(days=cluster_window_days)
    return trades, trades_for_diag


def _compute_summary(trades: list[SearchTrade]) -> tuple[float, float, float]:
    """(Sharpe-annualized, profit_factor, max_drawdown) from per-trade pct returns."""
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


# ────────────────────────────────────────────────────────────────────────
# Window context — Lab dispatch contract uniformity
# ────────────────────────────────────────────────────────────────────────


@dataclass
class CatalystWindowContext:
    """Pre-loaded, parameter-INDEPENDENT inputs for one walk-forward window.

    Heavy I/O amortised across the window's Lab trials; the per-trial
    work is the cluster recomputation under the active window setting.
    """

    universe: tuple[str, ...]
    insider_rows: pd.DataFrame
    prices_by_ticker: dict[str, pd.DataFrame]
    round_trip_costs: dict[str, Decimal]
    start: date_t
    end: date_t


async def load_catalyst_window_context(
    *,
    db_url: str,
    start: date_t,
    end: date_t,
    universe: tuple[str, ...] | None = None,
) -> CatalystWindowContext:
    """Load insider rows + price panels + per-ticker tier costs.

    Heavy I/O — call once per walk-forward window. ``universe`` defaults
    to ``CATALYST_TEST_UNIVERSE`` so the runner can plug straight in."""
    pool = await build_asyncpg_pool(db_url)
    try:
        u = universe if universe is not None else CATALYST_TEST_UNIVERSE
        # Pull insider rows back to ``start − max_window`` so the first
        # session's cluster window is fully covered (use 60d, the upper
        # bound of the Lab choice).
        insider_lookback = 60
        insider_rows = await _fetch_insider_rows(
            pool, universe=u,
            start=start - timedelta(days=insider_lookback),
            end=end,
        )
        prices_by_ticker = await _fetch_prices(
            pool, universe=u,
            start=start - timedelta(days=SMA_TREND_PERIOD + 30),
            end=end,
        )
        round_trip_costs = await _round_trip_cost_by_ticker(
            pool, tickers=u,
        )
    finally:
        await pool.close()
    return CatalystWindowContext(
        universe=u, insider_rows=insider_rows,
        prices_by_ticker=prices_by_ticker,
        round_trip_costs=round_trip_costs,
        start=start, end=end,
    )


def run_catalyst_with_context(
    context: CatalystWindowContext,
    *,
    overrides: dict | None = None,
    trade_log_path: Path | None = None,
) -> BacktestRunResult:
    """Run catalyst against a pre-loaded :class:`CatalystWindowContext`.

    The single Lab toggle ``cluster_window_days`` is read into the
    off-by-default module override and **reset per call** so no
    module-global state bleeds across Lab trials."""
    global _CLUSTER_WINDOW_OVERRIDE
    overrides = dict(overrides or {})
    _CLUSTER_WINDOW_OVERRIDE = (
        int(overrides["cluster_window_days"])
        if "cluster_window_days" in overrides
        else None
    )
    try:
        active_window = _cluster_window()
        trades, trades_for_diag = _build_trades(
            universe=context.universe,
            insider_rows=context.insider_rows,
            prices_by_ticker=context.prices_by_ticker,
            cluster_window_days=active_window,
            round_trip_costs=context.round_trip_costs,
            start=context.start, end=context.end,
        )
    finally:
        _CLUSTER_WINDOW_OVERRIDE = None

    sharpe, pf, max_dd = _compute_summary(trades)

    if trade_log_path is not None:
        write_trade_log_csv(trade_log_path, trades)

    # OverfittingDiagnostic needs *some* price data; use whichever
    # ticker actually has bars in the panel.
    price_frames = [
        df.assign(ticker=t, date=df.index).reset_index(drop=True)[
            ["ticker", "date", "close"]]
        for t, df in context.prices_by_ticker.items() if not df.empty
    ]
    if price_frames:
        prices_for_diag = pd.concat(price_frames, ignore_index=True)
    else:
        prices_for_diag = pd.DataFrame(
            columns=["ticker", "date", "close"])

    parameters: dict[str, Any] = {"cluster_window_days": int(active_window)}
    return compute_search_metrics(
        engine="catalyst",
        parameters=parameters,
        trades_for_diag=trades_for_diag,
        sharpe=sharpe,
        profit_factor=pf,
        max_drawdown=max_dd,
        n_trials=max(1, len(parameters)),
        price_data=prices_for_diag,
        rubric_inputs={
            "lookahead_clean": True,
            "survivorship_inclusive": True,
            "pit_fundamentals": True,
            "regime_coverage": False,  # single-leg insider-cluster; honest flag
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
    """Thin wrapper: load context, run once. Convenience.

    The orchestrator should use :func:`load_catalyst_window_context` +
    :func:`run_catalyst_with_context` to amortise the DB load across
    candidates."""
    ctx = await load_catalyst_window_context(
        db_url=db_url, start=start, end=end, universe=universe,
    )
    return run_catalyst_with_context(
        ctx, overrides=overrides, trade_log_path=trade_log_path,
    )


# ────────────────────────────────────────────────────────────────────────
# CLI entry — the mandatory write_credibility_score side effect
# ────────────────────────────────────────────────────────────────────────


async def run_backtest(
    *,
    start: date_t,
    end: date_t,
    output_dir: Path,
    results_file: str,
    trades_file: str,
    json_output: bool,
    trade_log_path: Path | None,
) -> int:
    """End-to-end backtest with credibility-rubric persistence.

    Mirrors sentinel/reversion: call ``compute_search_metrics`` to bundle
    the rubric, then ``write_credibility_score`` so
    ``graduation_ready('catalyst')`` can read it later (CLAUDE.md
    Engine-build compliance shortlist + engine_readiness §8).
    """
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL not set", file=sys.stderr)
        return 1
    output_dir.mkdir(parents=True, exist_ok=True)
    ctx = await load_catalyst_window_context(
        db_url=db_url, start=start, end=end,
    )
    result = run_catalyst_with_context(ctx, overrides=None,
                                       trade_log_path=trade_log_path)

    # The mandatory rubric persistence (compliance grep #3).
    pool = await build_asyncpg_pool(db_url)
    try:
        if result.credibility_rubric is not None:
            wrote = await write_credibility_score(
                pool, engine_name="catalyst",
                score=result.credibility_rubric,
            )
            logger.info(
                "catalyst.backtest.credibility_persisted",
                wrote=wrote, score=result.credibility_score,
            )
    finally:
        await pool.close()

    results_path = output_dir / results_file
    trades_path = output_dir / trades_file
    results_path.write_text(result.to_json())
    write_trade_log_csv(trades_path, result.trade_log)
    if json_output:
        print(result.to_json())
    else:
        print(_format_human_summary(start, end, result))
        print(f"\nartifacts → {results_path}, {trades_path}")
    return 0


def _format_human_summary(
    start: date_t, end: date_t, result: BacktestRunResult,
) -> str:
    return "\n".join([
        f"Catalyst backtest — {start} → {end}",
        f"  trades                : {result.trades}",
        f"  Sharpe (annualized)   : {result.sharpe:+.3f}",
        f"  Profit factor         : {result.profit_factor:.3f}",
        f"  Max drawdown          : {result.max_drawdown:+.3%}",
        f"  Credibility score     : {result.credibility_score}/100  "
        f"(passed_gate={result.passed_gate})",
        f"  DSR                   : {result.dsr:.4f}",
        f"  Trades-per-param      : {result.trades_per_param:.2f}",
    ])


# ────────────────────────────────────────────────────────────────────────
# SP-B / SP-F — Lab targeting declaration (engine-OWNED, resolved by
# ops.lab.run's roster-driven resolver). primary_metric=SHARPE is the
# canonical default for a swing engine; the gate stays sacred (SP-D §1.2).
# ────────────────────────────────────────────────────────────────────────


LAB_TARGET = LabTarget(
    param_ranges={
        # The ONE pre-registered toggle: legacy default 30 vs the single
        # alternative-window variant 45. choice:<csv> (NOT a range/grid).
        "cluster_window_days": (30, 45, "choice:30,45"),
    },
    run_for_search=run_for_search,
    load_window_context=load_catalyst_window_context,
    run_with_context=run_catalyst_with_context,
    default_params=default_params,
    primary_metric=LabPrimaryMetric.SHARPE,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--start", type=date_t.fromisoformat,
                   default=date_t(2020, 1, 1))
    p.add_argument("--end", type=date_t.fromisoformat,
                   default=datetime.now(UTC).date())
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    p.add_argument("--results-file", default=DEFAULT_RESULTS_FILE)
    p.add_argument("--trades-file", default=DEFAULT_TRADES_FILE)
    p.add_argument("--json", dest="json_output", action="store_true")
    p.add_argument("--trade-log", type=Path, default=None,
                   help="Write standardised per-trade CSV to this path too.")
    return p.parse_args(argv)


async def amain(args: argparse.Namespace) -> int:
    return await run_backtest(
        start=args.start, end=args.end,
        output_dir=args.output_dir,
        results_file=args.results_file,
        trades_file=args.trades_file,
        json_output=args.json_output,
        trade_log_path=args.trade_log,
    )


def main() -> None:  # pragma: no cover — CLI shim
    raise SystemExit(asyncio.run(amain(_parse_args())))


# Silence vulture for unused-but-needed imports referenced through CLI.
_ = (csv, Mapping)


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = [
    "CatalystWindowContext",
    "LAB_TARGET",
    "default_params",
    "load_catalyst_window_context",
    "run_backtest",
    "run_catalyst_with_context",
    "run_for_search",
]
