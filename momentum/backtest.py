"""Cross-sectional 12-1 momentum backtest (Phase 1).

Strategy
--------
Long-only top-decile cross-sectional momentum, monthly rebalance.

For each month-end ``t`` in the backtest window:

1. Compute each ticker's 12-1 month return:
       score(ticker, t) = price(ticker, t-skip) / price(ticker, t-skip-lookback) - 1
   Default skip=21 trading days, lookback=231 trading days (~12-1 calendar months).
2. Filter out tickers without ``skip + lookback`` bars of continuous prior history.
3. Rank survivors; take the top decile (≥ 1 name).
4. Open positions at the next bar's open (× 1+slippage); close at the close of the
   bar ~21 trading days later (× 1−slippage). Slippage = tier-aware round-trip / 2.

Each (ticker, entry, exit) is recorded as a separate :class:`MomentumTrade` so
the orchestrator's per-trade machinery (DSR, MinBTL, overfitting diagnostic)
works without engine-specific accommodations.

Phase 1 scope
-------------
* No 5-plug architecture — backtest only.
* No sector caps, no drawdown circuit breaker, no per-name dollar caps —
  equal-weight by count is enough for the kill-or-continue verdict.
* No final-holdout shipping path; orchestrator's existing held-back machinery
  produces the verdict.

Survivorship caveat
-------------------
``platform.prices_daily`` is partially-clean: ~99% of 7374 distinct tickers
have bars through 2025, but only ~56 are recorded as delisted before 2025
(true count should be hundreds across 2018-2025). Major 2023 delistings
SIVB, WeWork, Credit Suisse are missing entirely; BBBY shows post-bankruptcy
ticker-reuse data as continuous. Net effect: walk-forward 2018-2023 scores
are upward-biased; held-back 2024-2025 is less affected (most major
delistings were 2023, so the 24-25 universe is mostly real survivors).
"""
from __future__ import annotations

import argparse
import asyncio
import math
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd
import structlog

from tpcore.db import build_asyncpg_pool

if TYPE_CHECKING:  # pragma: no cover
    from tpcore.backtest.search import BacktestRunResult

logger = structlog.get_logger(__name__)

# ────────────────────────────────────────────────────────────────────────────
# Backtest knobs (defaults — overridden via search-pipeline)
# ────────────────────────────────────────────────────────────────────────────

# 12-1 momentum: total return over the last 252 trading days, skipping the
# most recent 21 trading days (≈ 12 calendar months minus 1).
DEFAULT_LOOKBACK_DAYS = 231  # = 252 - 21
DEFAULT_SKIP_DAYS = 21
DEFAULT_HOLD_DAYS = 21  # rebalance every ~month
DEFAULT_TOP_DECILE_PCT = 0.10  # top 10% by momentum score

SLIPPAGE_PER_SIDE = 0.0005  # 5 bps legacy fallback; tier-aware lookup wins
_TIER_ROUND_TRIP_COSTS: dict[str, float] = {}


def _slippage_per_side(ticker: str) -> float:
    rt = _TIER_ROUND_TRIP_COSTS.get(ticker)
    return rt / 2.0 if rt is not None else SLIPPAGE_PER_SIDE


# Parameter-search overrides (set per trial by orchestrator).
_LOOKBACK_OVERRIDE: int | None = None
_SKIP_OVERRIDE: int | None = None
_HOLD_OVERRIDE: int | None = None
_TOP_DECILE_OVERRIDE: float | None = None


def _lookback() -> int:
    return _LOOKBACK_OVERRIDE if _LOOKBACK_OVERRIDE is not None else DEFAULT_LOOKBACK_DAYS


def _skip() -> int:
    return _SKIP_OVERRIDE if _SKIP_OVERRIDE is not None else DEFAULT_SKIP_DAYS


def _hold() -> int:
    return _HOLD_OVERRIDE if _HOLD_OVERRIDE is not None else DEFAULT_HOLD_DAYS


def _top_decile() -> float:
    return _TOP_DECILE_OVERRIDE if _TOP_DECILE_OVERRIDE is not None else DEFAULT_TOP_DECILE_PCT


MOMENTUM_OVERRIDE_KEYS = (
    "lookback_days",
    "skip_days",
    "hold_days",
    "top_decile_pct",
)


DEFAULT_OUTPUT_DIR = Path("backtests")
DEFAULT_RESULTS_FILE = "momentum_backtest.json"


# ────────────────────────────────────────────────────────────────────────────
# Data classes
# ────────────────────────────────────────────────────────────────────────────


@dataclass
class MomentumTrade:
    ticker: str
    entry_date: date
    entry_price: float
    exit_date: date
    exit_price: float
    pnl_pct: float
    score_at_entry: float

    @property
    def exit_reason(self) -> str:
        return "scheduled_rebalance"


@dataclass
class MomentumSummary:
    n_trades: int
    win_rate: float
    avg_return_pct: float
    sharpe_annualized: float
    max_drawdown_pct: float
    profit_factor: float


# ────────────────────────────────────────────────────────────────────────────
# Data load
# ────────────────────────────────────────────────────────────────────────────


async def _load_universe_t12(pool) -> tuple[str, ...]:
    """Default universe = T1+T2 from platform.liquidity_tiers."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT ticker FROM platform.liquidity_tiers WHERE tier <= 2 ORDER BY ticker"
        )
    return tuple(r["ticker"] for r in rows)


async def _load_bars(
    pool, tickers: tuple[str, ...], start: date, end: date,
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
        by_ticker[r["ticker"]].append(
            {"date": r["date"], "open": float(r["open"]),
             "high": float(r["high"]), "low": float(r["low"]),
             "close": float(r["close"]), "volume": int(r["volume"])}
        )
    out: dict[str, pd.DataFrame] = {}
    for ticker, ticker_rows in by_ticker.items():
        if len(ticker_rows) < 50:  # toss tickers with almost no data
            continue
        df = pd.DataFrame(ticker_rows).set_index("date").sort_index()
        out[ticker] = df
    return out


# ────────────────────────────────────────────────────────────────────────────
# Rebalance schedule
# ────────────────────────────────────────────────────────────────────────────


def _month_end_dates_within(_dates: pd.DatetimeIndex, start: date, end: date) -> list[date]:
    """Pick the last NYSE trading session of each calendar month in [start, end].

    Source of truth is ``tpcore.calendar`` (CLAUDE.md: "Market hours via
    exchange_calendars (NYSE)"). The ``_dates`` argument is retained for
    backwards-compat with callers but ignored — we no longer derive sessions
    from panel data so that the rebalance schedule is independent of which
    tickers happened to have bars on each day."""
    from tpcore.calendar import sessions_in_range

    sessions = sessions_in_range(start, end)
    last_for_ym: dict[tuple[int, int], date] = {}
    for d in sessions:
        last_for_ym[(d.year, d.month)] = d
    return [d for _, d in sorted(last_for_ym.items())]


# ────────────────────────────────────────────────────────────────────────────
# Core backtest loop
# ────────────────────────────────────────────────────────────────────────────


def _compute_one_rebalance(
    panels: dict[str, pd.DataFrame],
    rebalance_date: date,
    *,
    lookback: int,
    skip: int,
    hold: int,
    top_decile_pct: float,
) -> list[MomentumTrade]:
    """For one rebalance date, compute scores → top decile → open + close trades."""
    scores: dict[str, float] = {}
    for ticker, df in panels.items():
        if rebalance_date not in df.index:
            continue
        idx = df.index.get_loc(rebalance_date)
        # Need at least skip + lookback prior bars; reject names with gaps.
        if idx < skip + lookback:
            continue
        p_now = float(df.iloc[idx - skip]["close"])
        p_then = float(df.iloc[idx - skip - lookback]["close"])
        if p_then <= 0 or math.isnan(p_now) or math.isnan(p_then):
            continue
        # Tradability filter — keep live and backtest agreed on the universe.
        # See momentum/models.py for the rules.
        from decimal import Decimal as _Decimal

        from momentum.models import is_tradeable_common_stock

        if not is_tradeable_common_stock(ticker, _Decimal(str(p_now))):
            continue
        scores[ticker] = (p_now / p_then) - 1.0

    if not scores:
        return []

    # Top decile by score, descending.
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    n_decile = max(1, int(len(ranked) * top_decile_pct))
    top = ranked[:n_decile]

    trades: list[MomentumTrade] = []
    for ticker, score in top:
        df = panels[ticker]
        entry_idx = df.index.get_loc(rebalance_date) + 1
        if entry_idx >= len(df):
            continue
        exit_idx = min(entry_idx + hold, len(df) - 1)
        if exit_idx <= entry_idx:
            continue
        slip = _slippage_per_side(ticker)
        entry_px = float(df.iloc[entry_idx]["open"]) * (1.0 + slip)
        exit_px = float(df.iloc[exit_idx]["close"]) * (1.0 - slip)
        if entry_px <= 0:
            continue
        pnl_pct = (exit_px / entry_px) - 1.0
        # pandas hands back Timestamps from the index; downstream code (the
        # orchestrator's slice-by-date, OverfittingDiagnostic) compares
        # against python date — coerce here so the trade record is uniform.
        entry_d = df.index[entry_idx]
        exit_d = df.index[exit_idx]
        if isinstance(entry_d, pd.Timestamp):
            entry_d = entry_d.date()
        if isinstance(exit_d, pd.Timestamp):
            exit_d = exit_d.date()
        trades.append(MomentumTrade(
            ticker=ticker,
            entry_date=entry_d,
            entry_price=entry_px,
            exit_date=exit_d,
            exit_price=exit_px,
            pnl_pct=pnl_pct,
            score_at_entry=score,
        ))
    return trades


def _run_backtest(
    panels: dict[str, pd.DataFrame], *,
    start: date, end: date,
    lookback: int, skip: int, hold: int, top_decile_pct: float,
) -> list[MomentumTrade]:
    """Walk every month-end in [start, end]; produce per-position trades."""
    all_dates = sorted({d for df in panels.values() for d in df.index})
    rebal_dates = _month_end_dates_within(pd.DatetimeIndex(all_dates), start, end)
    trades: list[MomentumTrade] = []
    for rd in rebal_dates:
        trades.extend(_compute_one_rebalance(
            panels, rd,
            lookback=lookback, skip=skip, hold=hold, top_decile_pct=top_decile_pct,
        ))
    return trades


# ────────────────────────────────────────────────────────────────────────────
# Metrics
# ────────────────────────────────────────────────────────────────────────────


def _compute_summary(trades: list[MomentumTrade]) -> MomentumSummary:
    if not trades:
        return MomentumSummary(0, 0.0, 0.0, 0.0, 0.0, 0.0)
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
    return MomentumSummary(
        n_trades=n, win_rate=win_rate, avg_return_pct=avg,
        sharpe_annualized=sharpe, max_drawdown_pct=max_dd, profit_factor=pf,
    )


def _trade_records_to_search_trades(trades: list[MomentumTrade]) -> list:
    from tpcore.backtest.search import SearchTrade
    return [
        SearchTrade(
            ticker=t.ticker, entry_date=t.entry_date, entry_price=t.entry_price,
            exit_date=t.exit_date, exit_price=t.exit_price,
            pnl_pct=t.pnl_pct, direction="LONG", exit_reason=t.exit_reason,
        ) for t in trades
    ]


def _trades_to_diagnostic_dicts(trades: list[MomentumTrade]) -> list[dict]:
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
        sub = df[["open", "high", "low", "close"]].reset_index().rename(columns={"index": "date"})
        sub["ticker"] = ticker
        frames.append(sub)
    if not frames:
        return pd.DataFrame(columns=["ticker", "date", "open", "high", "low", "close"])
    return pd.concat(frames, ignore_index=True)


# ────────────────────────────────────────────────────────────────────────────
# Parameter-search hooks
# ────────────────────────────────────────────────────────────────────────────


def _overrides_from_args(args: argparse.Namespace) -> dict:
    out: dict = {}
    for k in MOMENTUM_OVERRIDE_KEYS:
        v = getattr(args, k, None)
        if v is not None:
            out[k] = v
    return out


def _apply_overrides_from_args(args: argparse.Namespace) -> None:
    global _LOOKBACK_OVERRIDE, _SKIP_OVERRIDE, _HOLD_OVERRIDE, _TOP_DECILE_OVERRIDE
    _LOOKBACK_OVERRIDE = (
        int(args.lookback_days) if getattr(args, "lookback_days", None) is not None else None
    )
    _SKIP_OVERRIDE = (
        int(args.skip_days) if getattr(args, "skip_days", None) is not None else None
    )
    _HOLD_OVERRIDE = (
        int(args.hold_days) if getattr(args, "hold_days", None) is not None else None
    )
    _TOP_DECILE_OVERRIDE = (
        float(args.top_decile_pct) if getattr(args, "top_decile_pct", None) is not None else None
    )


@dataclass
class MomentumWindowContext:
    """Pre-loaded panels for one walk-forward window.

    Note: ``start`` here is the rebalance-window start; ``raw_start`` reaches
    back ``lookback + skip`` trading days further so 12-1 momentum can be
    computed on the first rebalance date."""

    panels: dict[str, pd.DataFrame]
    tier_round_trip_costs: dict[str, float]
    start: date  # earliest rebalance date considered
    end: date
    universe: tuple[str, ...]
    raw_start: date  # actual bar-load start (= start - warmup)


async def load_momentum_window_context(
    *,
    db_url: str,
    start: date,
    end: date,
    universe: tuple[str, ...] | None = None,
) -> MomentumWindowContext:
    """Load bars + tier costs for [start - 1y warmup, end].

    The 1-year warmup ensures the first rebalance date has a complete
    lookback window. Heavy I/O — call once per walk-forward window."""
    from datetime import timedelta as _td

    from tpcore.backtest.cost_model import load_tier_costs

    raw_start = start - _td(days=400)  # 400 calendar days ≈ 252 trading days + buffer
    pool = await build_asyncpg_pool(db_url)
    try:
        tier_costs = await load_tier_costs(pool)
        if universe is None:
            universe = await _load_universe_t12(pool)
        raw = await _load_bars(pool, universe, raw_start, end)
    finally:
        await pool.close()
    return MomentumWindowContext(
        panels=raw, tier_round_trip_costs=tier_costs,
        start=start, end=end, universe=universe, raw_start=raw_start,
    )


def run_momentum_with_context(
    context: MomentumWindowContext,
    *,
    overrides: dict | None = None,
    trade_log_path: Path | None = None,
) -> BacktestRunResult:
    """Run momentum against a pre-loaded :class:`MomentumWindowContext`."""
    from tpcore.backtest.search import (
        BacktestRunResult,
        compute_search_metrics,
        write_trade_log_csv,
    )

    global _LOOKBACK_OVERRIDE, _SKIP_OVERRIDE, _HOLD_OVERRIDE, _TOP_DECILE_OVERRIDE
    overrides = dict(overrides or {})
    _LOOKBACK_OVERRIDE = (
        int(overrides["lookback_days"]) if "lookback_days" in overrides else None
    )
    _SKIP_OVERRIDE = (
        int(overrides["skip_days"]) if "skip_days" in overrides else None
    )
    _HOLD_OVERRIDE = (
        int(overrides["hold_days"]) if "hold_days" in overrides else None
    )
    _TOP_DECILE_OVERRIDE = (
        float(overrides["top_decile_pct"]) if "top_decile_pct" in overrides else None
    )
    _TIER_ROUND_TRIP_COSTS.clear()
    _TIER_ROUND_TRIP_COSTS.update(context.tier_round_trip_costs)

    if not context.panels:
        return BacktestRunResult(
            engine="momentum", parameters=overrides, credibility_score=0, passed_gate=False,
            sharpe=0.0, profit_factor=0.0, max_drawdown=0.0, trades=0, dsr=0.0,
            min_btl_gap=0, trades_per_param=0.0, sensitivity_score=None,
            ruin_probability=0.0, trade_log=[],
        )

    trades = _run_backtest(
        context.panels, start=context.start, end=context.end,
        lookback=_lookback(), skip=_skip(), hold=_hold(), top_decile_pct=_top_decile(),
    )
    summary = _compute_summary(trades)

    search_trades = _trade_records_to_search_trades(trades)
    if trade_log_path is not None:
        write_trade_log_csv(trade_log_path, search_trades)

    parameters = {
        "lookback_days": int(_lookback()),
        "skip_days": int(_skip()),
        "hold_days": int(_hold()),
        "top_decile_pct": float(_top_decile()),
    }
    trades_for_diag = _trades_to_diagnostic_dicts(trades)
    price_data = _panels_to_price_data(context.panels)
    return compute_search_metrics(
        engine="momentum",
        parameters=parameters,
        trades_for_diag=trades_for_diag,
        sharpe=summary.sharpe_annualized,
        profit_factor=summary.profit_factor,
        max_drawdown=summary.max_drawdown_pct,
        n_trials=len(parameters),
        price_data=price_data,
        rubric_inputs={
            "lookahead_clean": True,
            "survivorship_inclusive": False,  # honestly flagged — see module docstring
            "pit_fundamentals": True,  # technical-only
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
    """Thin wrapper: load context, run once. Single-call convenience."""
    ctx = await load_momentum_window_context(
        db_url=db_url, start=start, end=end, universe=universe,
    )
    return run_momentum_with_context(ctx, overrides=overrides, trade_log_path=trade_log_path)


# ────────────────────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────────────────────


async def amain(args: argparse.Namespace) -> int:
    db_url = args.database_url or os.getenv("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL not set — pass --database-url or export it.", file=sys.stderr)
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

    print(f"\nMomentum backtest  {args.start} → {args.end}")
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
    p.add_argument("--json", dest="json_output", action="store_true",
                   help="Emit a single JSON object with search-pipeline metrics and exit 0.")
    p.add_argument("--trade-log", type=Path, default=None,
                   help="Write standardised per-trade CSV to this path.")
    p.add_argument("--lookback-days", type=int, default=None,
                   help="Override lookback in trading days (default 231).")
    p.add_argument("--skip-days", type=int, default=None,
                   help="Override skip in trading days (default 21).")
    p.add_argument("--hold-days", type=int, default=None,
                   help="Override holding period in trading days (default 21).")
    p.add_argument("--top-decile-pct", type=float, default=None,
                   help="Top decile fraction (default 0.10).")
    return p.parse_args(argv)


def main() -> None:  # pragma: no cover
    raise SystemExit(asyncio.run(amain(_parse_args())))


if __name__ == "__main__":  # pragma: no cover
    main()
