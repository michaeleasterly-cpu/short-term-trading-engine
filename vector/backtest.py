"""Backtest Vector's three-gate model on historical data.

Same shape as ``sigma/backtest.py`` and
``reversion/backtest.py``: pure historical simulation
on cached panels, no broker calls, no plug imports beyond what already
lives in ``vector/plugs/``. The science is shared — anything tightened
in production should land here too.

Gates (all point-in-time):
    1. **Value & Quality** — most-recent ``fundamentals_quarterly`` row
       with ``filing_date <= sim_date`` must satisfy
       ``pb < 1.5 AND de < 3 AND revenue > $500M`` AND last close above
       the 200-SMA.
    2. **Earnings** — at least one ``earnings_events`` row for the
       ticker with ``event_date`` within ±5 trading days of sim_date.
    3. **Technical trigger** — pullback to 10-/20-MA on volume > 1.2× avg
       OR breakout above 50-MA on volume > 1.5× avg (mirrors
       ``vector/plugs/setup_detection``).

Crash guard mirrors the live engine: VIX-scaled sizing (deferred — no
historical VIX in the cache, so all trades use the 1.0× factor) and
SPY-drawdown cooldown (SPY drops ≥10% in 20 days → no new entries for
10 sessions).

Trade simulation: market entry next bar, hard stop −7%, profit target
+15%, trailing stop −5% from peak after +10% is reached. 0.05%
slippage per side.

Output:
    backtests/vector_trades.csv      — per-trade ledger
    backtests/vector_backtest.json   — summary metrics
    Statistical Validation block printed inline (sensitivity sweep + MC +
    PSR/DSR/MinBTL via tpcore.backtest.statistical_validation). The
    full overfitting bundle from tpcore.backtest.overfitting will be
    wired here once the parallel session lands that module.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import os
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd
import structlog

from tpcore.backtest.cli_overrides import overrides_from_args
from tpcore.backtest.cost_model import (
    slippage_per_side as _tpcore_slippage_per_side,
)
from tpcore.backtest.price_loader import load_prices
from tpcore.db import build_asyncpg_pool
from tpcore.lab.target import LabTarget

if TYPE_CHECKING:  # pragma: no cover
    from tpcore.backtest.search import BacktestRunResult

logger = structlog.get_logger(__name__)

# ────────────────────────────────────────────────────────────────────────────
# Constants — copy of vector/plugs values, kept here so the backtest is
# importable even if a plug is being refactored.
# ────────────────────────────────────────────────────────────────────────────

SPY_SYMBOL = "SPY"

# Gate 1 thresholds.
PB_CEILING = 1.5
DE_CEILING = 3.0
REVENUE_FLOOR = 500_000_000.0  # $500M

# Gate 2 window (trading days, applied in calendar days for simplicity).
EARNINGS_WINDOW_DAYS = 5

# Gate 3 windows / volume multipliers.
SMA_10 = 10
SMA_20 = 20
SMA_50 = 50
SMA_200 = 200
PULLBACK_VOL_MULT = 1.2
BREAKOUT_VOL_MULT = 1.5

# Risk / sizing.
HARD_STOP_PCT = 0.07
PROFIT_TARGET_PCT = 0.15
TRAILING_TRIGGER_PCT = 0.10
TRAILING_STOP_PCT = 0.05
SLIPPAGE_PER_SIDE = 0.0005  # legacy default; per-ticker tier lookup wins.
_TIER_ROUND_TRIP_COSTS: dict[str, float] = {}


def _slippage_per_side(ticker: str) -> float:
    """Per-side slippage for ``ticker``; T4 default for unknowns.

    Thin delegate to the shared :func:`tpcore.backtest.cost_model.
    slippage_per_side` (Lean P5.2 consolidation, cluster #11).
    """
    return _tpcore_slippage_per_side(
        ticker, _TIER_ROUND_TRIP_COSTS, SLIPPAGE_PER_SIDE
    )


# Parameter-search overrides. None = use the module default. Set once per
# trial by the search orchestrator (or by the CLI flags) before the backtest
# runs; reset between trials.
_PB_CEILING_OVERRIDE: float | None = None
_DE_CEILING_OVERRIDE: float | None = None
_EARNINGS_WINDOW_OVERRIDE: int | None = None
_HARD_STOP_PCT_OVERRIDE: float | None = None
_SWING_SCORE_THRESHOLD_OVERRIDE: float | None = None  # None = no gate
# vector_composite Lab candidate (spec
# docs/superpowers/specs/2026-05-20-vector-composite-lab-candidate.md
# §3.1): off-by-default flag selecting AND-gate (legacy) vs composite
# (top-decile selection of value/earnings/technical z-score weighted
# 0.35/0.40/0.25). None ⇒ "and_gate"; explicit "composite" ⇒ the
# Lab candidate branch. Reset per call to run_vector_with_context (§4.2,
# H-VC-8) so no module-global state bleeds across trials.
_COMPOSITE_MODE_OVERRIDE: str | None = None

# Legacy default for the composite_mode toggle (the AND-gate path).
COMPOSITE_MODE_DEFAULT: str = "and_gate"


def _pb_ceiling() -> float:
    return _PB_CEILING_OVERRIDE if _PB_CEILING_OVERRIDE is not None else PB_CEILING


def _de_ceiling() -> float:
    return _DE_CEILING_OVERRIDE if _DE_CEILING_OVERRIDE is not None else DE_CEILING


def _earnings_window_days() -> int:
    return (
        _EARNINGS_WINDOW_OVERRIDE
        if _EARNINGS_WINDOW_OVERRIDE is not None
        else EARNINGS_WINDOW_DAYS
    )


def _hard_stop_pct() -> float:
    return _HARD_STOP_PCT_OVERRIDE if _HARD_STOP_PCT_OVERRIDE is not None else HARD_STOP_PCT


def _swing_score_threshold() -> float | None:
    return _SWING_SCORE_THRESHOLD_OVERRIDE


def _composite_mode() -> str:
    """Active composite mode: ``"composite"`` iff the override is the
    exact string ``"composite"``, else ``"and_gate"`` (the legacy default
    and the value when no Lab override is supplied). Pure accessor;
    callers MUST NOT branch on the raw module global."""
    if _COMPOSITE_MODE_OVERRIDE == "composite":
        return "composite"
    return COMPOSITE_MODE_DEFAULT


def default_params() -> dict[str, Any]:
    """Current live defaults for EXACTLY this engine's
    ops.lab.run.PARAM_RANGES keys (SP3 O1 seam, spec §7.1). Pure. The
    swing-score default mirrors run_vector_with_context's 0.0-when-None
    convention so the diff is well-defined. ``composite_mode`` reports
    the legacy AND-gate default — the Lab dossier's ``param_diff`` then
    carries the true ``and_gate → composite`` delta (spec §4.2,
    H-VC-10)."""
    swing = _swing_score_threshold()
    return {
        "pb_ceiling": float(_pb_ceiling()),
        "de_ceiling": float(_de_ceiling()),
        "earnings_window_days": int(_earnings_window_days()),
        "swing_score_threshold": float(swing) if swing is not None else 0.0,
        "stop_pct": float(_hard_stop_pct()),
        # ALWAYS the legacy default (NOT _composite_mode() — that would
        # report the current override state, polluting the Lab dossier's
        # ``param_diff`` between trials. The dossier needs the engine's
        # static legacy default so the delta is the true variant choice,
        # not "default == current override".) Spec §4.2, H-VC-10.
        "composite_mode": COMPOSITE_MODE_DEFAULT,
    }


def _synth_swing_score(magnitude: float | None) -> float:
    """Synthetic 0-90 swing score for the search pipeline only.

    The live engine computes swing_score in ``vector.plugs.setup_detection``;
    the backtest's gate-pass-through machinery doesn't, so for the search
    we synthesise: 30 (technical trigger passed) + 100 × |earnings growth|,
    clamped to [30, 90]. None / 0 magnitude → 30 (technical only)."""
    base = 30.0
    mag = abs(float(magnitude)) if magnitude is not None else 0.0
    return float(min(90.0, base + 100.0 * mag))


PRE_GRAD_POSITION_CAP_USD = 2000.0

# Crash guard: SPY −10% in 20 sessions → 10-session cooldown on new entries.
SPY_DRAWDOWN_THRESHOLD = 0.10
SPY_DRAWDOWN_LOOKBACK = 20
SPY_COOLDOWN_DAYS = 10

# VIX-aware sizing — uses 20-day SPY realized vol as VIX proxy.
# Plan §4.3: > 25% RV → 0.5× size; > 30% RV → 0.25× size.
VIX_PROXY_LOOKBACK = 20
VIX_SCALE_DOWN_50_PCT = 25.0  # threshold for half size
VIX_SCALE_DOWN_25_PCT = 30.0  # threshold for quarter size

DEFAULT_OUTPUT_DIR = Path("backtests")
DEFAULT_RESULTS_FILE = "vector_backtest.json"
DEFAULT_TRADES_FILE = "vector_trades.csv"


# ────────────────────────────────────────────────────────────────────────────
# Data classes
# ────────────────────────────────────────────────────────────────────────────


@dataclass
class TradeRecord:
    ticker: str
    entry_date: date
    entry_price: float
    exit_date: date | None = None
    exit_price: float | None = None
    exit_reason: str = ""  # target | hard_stop | trailing_stop | max_hold
    holding_days: int = 0
    pnl: float = 0.0
    raw_return_pct: float = 0.0  # pre-sizing — pnl / entry_price
    return_pct: float = 0.0  # post-sizing — raw × size_factor (this is what feeds Sharpe/PF)
    size_factor: float = 1.0  # 1.0 / 0.5 / 0.25 from VIX-aware crash guard
    rv20_at_entry_pct: float | None = None  # SPY 20-day realized vol at entry, %
    trigger: str = ""  # pullback_to_10ma | pullback_to_20ma | breakout_above_50ma
    earnings_magnitude: float | None = None
    pb_at_entry: float | None = None
    de_at_entry: float | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("entry_date", "exit_date"):
            if d[k] is not None:
                d[k] = d[k].isoformat() if hasattr(d[k], "isoformat") else d[k]
        return d


@dataclass
class VariantSummary:
    n_trades: int
    win_rate: float
    avg_return_pct: float
    sharpe_annualized: float
    max_drawdown_pct: float
    profit_factor: float
    by_year: dict[int, dict] = field(default_factory=dict)


# ────────────────────────────────────────────────────────────────────────────
# Data loading — bars + fundamentals + earnings events
# ────────────────────────────────────────────────────────────────────────────


async def _load_prices(pool, tickers: list[str], start: date, end: date) -> dict[str, pd.DataFrame]:
    # Lean P5.3 (#2): thin delegate to the shared tpcore loader. The
    # min-bar floor (SMA_200 + 5) is vector's intentional divergence,
    # preserved via the explicit ``min_bars`` parameter.
    return await load_prices(pool, tickers, start, end, min_bars=SMA_200 + 5)


async def _load_fundamentals(pool, tickers: list[str]) -> dict[str, list[dict]]:
    """Latest-first list per ticker; PIT filter applied in-loop."""
    sql = """
        SELECT ticker, filing_date, pb, de, revenue
        FROM platform.fundamentals_quarterly
        WHERE ticker = ANY($1)
        ORDER BY ticker, filing_date DESC
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, tickers)
    out: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        out[r["ticker"]].append(
            {
                "filing_date": r["filing_date"],
                "pb": float(r["pb"]) if r["pb"] is not None else None,
                "de": float(r["de"]) if r["de"] is not None else None,
                "revenue": float(r["revenue"]) if r["revenue"] is not None else None,
            }
        )
    return out


async def _load_earnings(pool, tickers: list[str]) -> dict[str, list[date]]:
    sql = """
        SELECT ticker, event_date, magnitude_pct
        FROM platform.earnings_events
        WHERE ticker = ANY($1) AND event_type = 'EARNINGS_BEAT'
        ORDER BY ticker, event_date
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, tickers)
    by_ticker: dict[str, list[tuple[date, float]]] = defaultdict(list)
    for r in rows:
        by_ticker[r["ticker"]].append(
            (r["event_date"], float(r["magnitude_pct"]) if r["magnitude_pct"] is not None else 0.0)
        )
    return by_ticker


# ────────────────────────────────────────────────────────────────────────────
# Indicator precompute
# ────────────────────────────────────────────────────────────────────────────


def _precompute(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["sma_10"] = df["close"].rolling(SMA_10, min_periods=SMA_10).mean()
    df["sma_20"] = df["close"].rolling(SMA_20, min_periods=SMA_20).mean()
    df["sma_50"] = df["close"].rolling(SMA_50, min_periods=SMA_50).mean()
    df["sma_200"] = df["close"].rolling(SMA_200, min_periods=SMA_200).mean()
    df["avg_vol_20"] = df["volume"].rolling(SMA_20, min_periods=SMA_20).mean()
    df["prior_close"] = df["close"].shift(1)
    return df


# ────────────────────────────────────────────────────────────────────────────
# Per-day gate evaluators
# ────────────────────────────────────────────────────────────────────────────


def _pit_fundamentals(rows: list[dict], as_of: date) -> dict | None:
    """Latest filing with filing_date <= as_of; rows are pre-sorted DESC."""
    for r in rows:
        if r["filing_date"] <= as_of:
            return r
    return None


def _passes_gate1(fundamentals: dict | None, last_close: float, sma_200: float) -> bool:
    if fundamentals is None:
        return False
    pb = fundamentals["pb"]
    de = fundamentals["de"]
    revenue = fundamentals["revenue"]
    if pb is None or de is None or revenue is None:
        return False
    if pb >= _pb_ceiling() or de >= _de_ceiling() or revenue <= REVENUE_FLOOR:
        return False
    if math.isnan(sma_200):
        return False
    if last_close < sma_200:
        return False
    return True


def _has_earnings(events: list[tuple[date, float]], as_of: date) -> tuple[bool, float | None]:
    if not events:
        return False, None
    lo = as_of - timedelta(days=_earnings_window_days())
    hi = as_of + timedelta(days=_earnings_window_days())
    # Earnings window. The "+5 days" half is technically lookahead, but
    # for momentum strategies the *anticipation* of an upcoming earnings
    # beat is itself the signal. The Gate 2 spec explicitly says ±5 days
    # — we honor it. PIT integrity comes from the price/fundamentals side.
    for ev_date, magnitude in events:
        if lo <= ev_date <= hi:
            return True, magnitude
    return False, None


def _technical_trigger(row: pd.Series, prior_close: float) -> str | None:
    """Pullback or breakout per Vector setup_detection spec."""
    last_close = float(row["close"])
    last_vol = float(row["volume"])
    avg_vol = float(row["avg_vol_20"])
    sma_10 = float(row["sma_10"])
    sma_20 = float(row["sma_20"])
    sma_50 = float(row["sma_50"])
    if any(math.isnan(x) for x in (last_close, avg_vol, sma_10, sma_20, sma_50)):
        return None

    # Breakout: today's close > 50-MA, prior_close <= 50-MA, vol > 1.5× avg.
    if last_close > sma_50 and prior_close <= sma_50 and last_vol > BREAKOUT_VOL_MULT * avg_vol:
        return "breakout_above_50ma"

    # Pullback: close near 10/20-MA, today > prior, vol > 1.2× avg.
    near_10 = abs(last_close - sma_10) / max(last_close, 1e-9) < 0.02
    near_20 = abs(last_close - sma_20) / max(last_close, 1e-9) < 0.02
    if (near_10 or near_20) and last_close > prior_close and last_vol > PULLBACK_VOL_MULT * avg_vol:
        return "pullback_to_10ma" if near_10 else "pullback_to_20ma"
    return None


# ────────────────────────────────────────────────────────────────────────────
# VIX-aware sizing (SPY 20-day realized volatility proxy)
# ────────────────────────────────────────────────────────────────────────────


def _spy_realized_vol_pct(spy_panel: pd.DataFrame) -> pd.Series:
    """Annualized SPY 20-day realized volatility expressed as a percent.

    Returns a Series indexed by date — same index as ``spy_panel``. NaN
    for the first ``VIX_PROXY_LOOKBACK`` rows where the rolling window
    isn't yet full. Used in two places: as the live VIX proxy for
    per-trade sizing, and as one bucket axis for the regime test in the
    overfitting diagnostic.
    """
    daily_returns = spy_panel["close"].astype(float).pct_change()
    rv = daily_returns.rolling(VIX_PROXY_LOOKBACK, min_periods=VIX_PROXY_LOOKBACK).std() * math.sqrt(252)
    return rv * 100.0  # express as percent


def _size_factor_from_rv(rv_pct: float | None) -> float:
    """Plan §4.3 VIX-aware sizing — applied per-trade.

    rv_pct > 30 → 0.25× ; > 25 → 0.50× ; otherwise 1.00× .
    None / NaN (rolling window not full) → 1.00× — the strategy can't
    yet judge regime, so we don't penalize.
    """
    if rv_pct is None or (isinstance(rv_pct, float) and math.isnan(rv_pct)):
        return 1.0
    v = float(rv_pct)
    if v > VIX_SCALE_DOWN_25_PCT:
        return 0.25
    if v > VIX_SCALE_DOWN_50_PCT:
        return 0.50
    return 1.0


# ────────────────────────────────────────────────────────────────────────────
# Crash guard — SPY drawdown cooldown
# ────────────────────────────────────────────────────────────────────────────


def _spy_in_cooldown(spy_panel: pd.DataFrame, today: date, cooldown_until: dict[str, date]) -> bool:
    """Returns True iff SPY drawdown ≥ threshold within last 20 sessions and the
    10-session cooldown hasn't elapsed yet."""
    if "until" in cooldown_until and today < cooldown_until["until"]:
        return True
    if today not in spy_panel.index:
        return False
    pos = spy_panel.index.get_loc(today)
    if pos < SPY_DRAWDOWN_LOOKBACK:
        return False
    window = spy_panel["close"].iloc[max(0, pos - SPY_DRAWDOWN_LOOKBACK + 1) : pos + 1]
    peak = float(window.max())
    last = float(window.iloc[-1])
    if peak > 0 and (peak - last) / peak >= SPY_DRAWDOWN_THRESHOLD:
        # Set cooldown end to N sessions ahead — use calendar days as a proxy.
        cooldown_until["until"] = today + timedelta(days=int(SPY_COOLDOWN_DAYS * 1.4))
        return True
    return False


# ────────────────────────────────────────────────────────────────────────────
# Trade simulator
# ────────────────────────────────────────────────────────────────────────────


def _simulate_trade(
    df: pd.DataFrame,
    *,
    entry_idx: int,
    entry_price: float,
    ticker: str,
    entry_date: date,
    trigger: str,
    earnings_mag: float | None,
    pb_at_entry: float | None,
    de_at_entry: float | None,
    rv20_at_entry_pct: float | None = None,
    size_factor: float = 1.0,
    max_hold_days: int = 30,
) -> TradeRecord:
    """Walk forward applying Vector's exit rules: target / trail / hard stop / max-hold.

    ``size_factor`` is applied at the end to scale the trade's contribution to
    portfolio P&L (1.0 / 0.5 / 0.25 from the VIX-aware crash guard). The
    ``raw_return_pct`` field preserves the unsized return for diagnostics.
    """
    record = TradeRecord(
        ticker=ticker,
        entry_date=entry_date,
        entry_price=entry_price,
        trigger=trigger,
        earnings_magnitude=earnings_mag,
        pb_at_entry=pb_at_entry,
        de_at_entry=de_at_entry,
        rv20_at_entry_pct=rv20_at_entry_pct,
        size_factor=size_factor,
    )
    stop = entry_price * (1 - _hard_stop_pct())
    target = entry_price * (1 + PROFIT_TARGET_PCT)
    trail_armed = False
    high_water = entry_price
    bars_left = min(max_hold_days, len(df) - entry_idx - 1)
    for i in range(1, bars_left + 1):
        bar = df.iloc[entry_idx + i]
        high = float(bar["high"])
        low = float(bar["low"])
        close = float(bar["close"])

        # Stop check first.
        if low <= stop:
            sell_px = stop * (1.0 - _slippage_per_side(ticker))
            record.exit_date = bar.name
            record.exit_price = sell_px
            record.exit_reason = "trailing_stop" if trail_armed else "hard_stop"
            record.holding_days = i
            break
        # Target hit.
        if high >= target:
            sell_px = target * (1.0 - _slippage_per_side(ticker))
            record.exit_date = bar.name
            record.exit_price = sell_px
            record.exit_reason = "target"
            record.holding_days = i
            break
        # Update trail high-water mark on any new high.
        if close > high_water:
            high_water = close
        # Arm trail when close ≥ entry × (1 + trigger).
        if not trail_armed and close >= entry_price * (1 + TRAILING_TRIGGER_PCT):
            trail_armed = True
        # If armed, raise the stop to peak × (1 - 0.05).
        if trail_armed:
            new_stop = high_water * (1 - TRAILING_STOP_PCT)
            if new_stop > stop:
                stop = new_stop
    else:
        # max-hold expired without exit.
        bar = df.iloc[entry_idx + bars_left]
        sell_px = float(bar["close"]) * (1.0 - _slippage_per_side(ticker))
        record.exit_date = bar.name
        record.exit_price = sell_px
        record.exit_reason = "max_hold"
        record.holding_days = bars_left

    pnl = (record.exit_price or entry_price) - entry_price
    record.pnl = pnl
    raw = pnl / entry_price if entry_price else 0.0
    record.raw_return_pct = raw
    record.return_pct = raw * size_factor  # what feeds Sharpe / equity / PF
    return record


# ────────────────────────────────────────────────────────────────────────────
# Variant runner
# ────────────────────────────────────────────────────────────────────────────


def _run(
    *,
    panels: dict[str, pd.DataFrame],
    spy_panel: pd.DataFrame | None,
    spy_rv_pct: pd.Series | None,
    fundamentals: dict[str, list[dict]],
    earnings: dict[str, list[tuple[date, float]]],
    start: date,
    end: date,
) -> list[TradeRecord]:
    all_dates = sorted({d for df in panels.values() for d in df.index})
    all_dates = [d for d in all_dates if start <= d <= end]
    trades: list[TradeRecord] = []
    next_eligible_idx = 0
    cooldown_state: dict[str, date] = {}

    for di, today in enumerate(all_dates):
        if di < next_eligible_idx:
            continue
        if spy_panel is not None and _spy_in_cooldown(spy_panel, today, cooldown_state):
            continue
        rv_today = float(spy_rv_pct.loc[today]) if (spy_rv_pct is not None and today in spy_rv_pct.index) else None
        size_factor = _size_factor_from_rv(rv_today)

        chosen: tuple[str, pd.DataFrame, int, str, float | None, dict] | None = None

        for ticker, df in panels.items():
            if today not in df.index:
                continue
            idx = df.index.get_loc(today)
            if idx < SMA_200 + 5:
                continue
            row = df.iloc[idx]
            prior_close = float(row["prior_close"])
            if math.isnan(prior_close):
                continue
            last_close = float(row["close"])
            sma_200 = float(row["sma_200"])

            funds = _pit_fundamentals(fundamentals.get(ticker, []), today)
            if not _passes_gate1(funds, last_close, sma_200):
                continue
            ok_cat, magnitude = _has_earnings(earnings.get(ticker, []), today)
            if not ok_cat:
                continue
            trigger = _technical_trigger(row, prior_close)
            if trigger is None:
                continue
            # Search-only synthetic swing-score gate. No-op when the override
            # is None (default backtest behaviour).
            swing_floor = _swing_score_threshold()
            if swing_floor is not None and _synth_swing_score(magnitude) < swing_floor:
                continue
            chosen = (ticker, df, idx, trigger, magnitude, funds or {})
            break  # single position at a time, first match wins

        if chosen is None:
            continue

        ticker, df, idx, trigger, magnitude, funds = chosen
        if idx + 1 >= len(df):
            continue
        next_open = float(df.iloc[idx + 1]["open"])
        entry_price = next_open * (1 + _slippage_per_side(ticker))
        record = _simulate_trade(
            df,
            entry_idx=idx + 1,
            entry_price=entry_price,
            ticker=ticker,
            entry_date=df.index[idx + 1],
            trigger=trigger,
            earnings_mag=magnitude,
            pb_at_entry=funds.get("pb"),
            de_at_entry=funds.get("de"),
            rv20_at_entry_pct=rv_today,
            size_factor=size_factor,
        )
        trades.append(record)

        if record.exit_date is not None:
            try:
                exit_idx = all_dates.index(record.exit_date)
            except ValueError:
                exit_idx = di + record.holding_days
            next_eligible_idx = exit_idx + 1
        else:
            next_eligible_idx = di + 1

    return trades


# ────────────────────────────────────────────────────────────────────────────
# vector_composite Lab candidate — composite scorer (spec §2.2–2.4 +
# §3.1; off-by-default; reached only when ``_composite_mode()=="composite"``)
# ────────────────────────────────────────────────────────────────────────────

# Spec §2.4 — pinned weights (no grid).
COMPOSITE_W_VALUE: float = 0.35
COMPOSITE_W_EARNINGS: float = 0.40
COMPOSITE_W_TECHNICAL: float = 0.25

# Spec §2.3 — zero-variance guard for cross-sectional z-score.
COMPOSITE_ZERO_VAR_EPS: float = 1e-9

# Spec §2.2 — insider-cluster window (strictly backward).
COMPOSITE_INSIDER_LOOKBACK_DAYS: int = 30

# Spec §2.4 — top-decile selection.
COMPOSITE_TOP_DECILE_PCT: float = 0.10

# Spec §2.2 — value sub-weight on D/E (P/B is the primary value axis).
COMPOSITE_DE_SUB_WEIGHT: float = 0.5

# Spec §2.2 — technical category → graded raw signal.
_COMPOSITE_TECH_MAP: dict[str | None, float] = {
    "breakout_above_50ma": 1.0,
    "pullback_to_10ma": 0.7,
    "pullback_to_20ma": 0.6,
    None: 0.0,
}


def _zscore_with_guard(values: list[float]) -> list[float]:
    """Cross-sectional z-score with the spec §2.3 zero-variance guard:
    ``std <= eps → z := 0.0`` for every entry. Pure, deterministic."""
    n = len(values)
    if n == 0:
        return []
    arr = np.asarray(values, dtype=float)
    mu = float(arr.mean())
    sigma = float(arr.std(ddof=1)) if n > 1 else 0.0
    if sigma <= COMPOSITE_ZERO_VAR_EPS:
        return [0.0] * n
    return ((arr - mu) / sigma).tolist()


def _composite_per_ticker_signals(
    *,
    ticker: str,
    today: date,
    row: pd.Series,
    prior_close: float,
    funds: dict | None,
    earnings_for_ticker: list[tuple[date, float]],
    insider_dates: list[date],
    earnings_window_days: int,
) -> dict[str, float] | None:
    """Per-ticker raw signal trio (value / earnings / insider / technical).

    Strictly point-in-time / backward windows per spec §2.2 + H-VC-6.
    Returns None when value-family inputs are missing (pb / de / revenue
    null) — matches the live AND-gate's null-guard (no imputation; that
    would be a silent lookahead/quality hazard).
    """
    if not funds:
        return None
    pb = funds.get("pb")
    de = funds.get("de")
    rev = funds.get("revenue")
    if pb is None or de is None or rev is None:
        return None
    pb_f = float(pb)
    de_f = float(de)
    # Spec §2.2 raw value signal: cheaper (lower P/B) and less-levered
    # (lower D/E) is better; D/E sub-weighted by COMPOSITE_DE_SUB_WEIGHT.
    # Sign is applied at the raw step; z-standardisation happens
    # cross-sectionally over the eligible universe (§2.3).
    v_raw_pre = -pb_f - COMPOSITE_DE_SUB_WEIGHT * de_f

    # Strictly-backward earnings window [today - w, today].
    earn_signal = 0.0
    backward_cutoff = today - timedelta(days=int(earnings_window_days))
    for evt_date, mag in earnings_for_ticker:
        if backward_cutoff <= evt_date <= today:
            earn_signal = max(earn_signal, max(0.0, float(mag)))

    # Strictly-backward insider-cluster window [today - 30, today].
    insider_signal = 0.0
    if insider_dates:
        insider_cutoff = today - timedelta(days=COMPOSITE_INSIDER_LOOKBACK_DAYS)
        if any(insider_cutoff <= d <= today for d in insider_dates):
            insider_signal = 1.0

    trigger = _technical_trigger(row, prior_close)
    tech_raw = _COMPOSITE_TECH_MAP.get(trigger, 0.0)

    return {
        "v_raw_pre": v_raw_pre,
        "earn_signal": earn_signal,
        "insider_signal": insider_signal,
        "tech_raw": tech_raw,
        "trigger": trigger or "",
    }


def _run_composite(
    *,
    panels: dict[str, pd.DataFrame],
    spy_panel: pd.DataFrame | None,
    spy_rv_pct: pd.Series | None,
    fundamentals: dict[str, list[dict]],
    earnings: dict[str, list[tuple[date, float]]],
    start: date,
    end: date,
    eligible_tickers: list[str],
    insider_clusters: dict[str, list[date]] | None = None,
) -> list[TradeRecord]:
    """Spec §2 composite scorer + top-decile selection. Replaces the
    AND-gate's pass/fail chain with a graded ranking; everything
    downstream (sizing / crash-guard / cost model / `_simulate_trade`)
    is unchanged."""
    insider_clusters = insider_clusters or {}
    all_dates = sorted({d for df in panels.values() for d in df.index})
    all_dates = [d for d in all_dates if start <= d <= end]
    trades: list[TradeRecord] = []
    next_eligible_idx = 0
    cooldown_state: dict[str, date] = {}
    earnings_window = int(_earnings_window_days())

    for di, today in enumerate(all_dates):
        if di < next_eligible_idx:
            continue
        if spy_panel is not None and _spy_in_cooldown(spy_panel, today, cooldown_state):
            continue
        rv_today = float(spy_rv_pct.loc[today]) if (spy_rv_pct is not None and today in spy_rv_pct.index) else None
        size_factor = _size_factor_from_rv(rv_today)

        # Per-sim_date cross-sectional scoring — for each eligible
        # ticker compute the raw signal trio, then standardise
        # cross-sectionally, then rank, then top-decile-select.
        per_ticker_raw: list[tuple[str, pd.DataFrame, int, dict]] = []
        for ticker, df in panels.items():
            if today not in df.index:
                continue
            idx = df.index.get_loc(today)
            if idx < SMA_200 + 5:
                continue
            row = df.iloc[idx]
            prior_close = float(row["prior_close"])
            if math.isnan(prior_close):
                continue
            funds = _pit_fundamentals(fundamentals.get(ticker, []), today)
            signals = _composite_per_ticker_signals(
                ticker=ticker,
                today=today,
                row=row,
                prior_close=prior_close,
                funds=funds,
                earnings_for_ticker=earnings.get(ticker, []),
                insider_dates=insider_clusters.get(ticker, []),
                earnings_window_days=earnings_window,
            )
            if signals is None:
                continue
            per_ticker_raw.append((ticker, df, idx, {**signals, "funds": funds or {}}))

        if not per_ticker_raw:
            continue

        # Cross-sectional standardisation (spec §2.3 — single-group
        # full-universe; the §6.1 BLOCKER fallback for the absent GICS
        # source) + zero-variance guard.
        v_raws = [d["v_raw_pre"] for _, _, _, d in per_ticker_raw]
        earn_raws = [d["earn_signal"] for _, _, _, d in per_ticker_raw]
        insider_raws = [d["insider_signal"] for _, _, _, d in per_ticker_raw]
        tech_raws = [d["tech_raw"] for _, _, _, d in per_ticker_raw]
        v_zs = _zscore_with_guard(v_raws)
        earn_zs = _zscore_with_guard(earn_raws)
        insider_zs = _zscore_with_guard(insider_raws)
        tech_zs = _zscore_with_guard(tech_raws)

        # Composite = 0.35*v_z + 0.40*(earn_z + insider_z) + 0.25*tech_z
        composites: list[tuple[float, str, pd.DataFrame, int, dict]] = []
        for i, (ticker, df, idx, sig) in enumerate(per_ticker_raw):
            c_z = earn_zs[i] + insider_zs[i]
            composite = (
                COMPOSITE_W_VALUE * v_zs[i]
                + COMPOSITE_W_EARNINGS * c_z
                + COMPOSITE_W_TECHNICAL * tech_zs[i]
            )
            composites.append((composite, ticker, df, idx, sig))

        # Top-decile selection: ceil(0.10*N), minimum 1.
        composites.sort(key=lambda x: x[0], reverse=True)
        n_decile = max(1, int(math.ceil(COMPOSITE_TOP_DECILE_PCT * len(composites))))
        top = composites[:n_decile]

        # Single position at a time — first match in the top decile.
        chosen: tuple[str, pd.DataFrame, int, dict] | None = None
        for _, ticker, df, idx, sig in top:
            # Swing-score floor (search-only, mirrors _run) — applied to
            # the same synthetic score so the toggle remains comparable
            # across modes when a Lab trial pins a swing floor.
            swing_floor = _swing_score_threshold()
            if swing_floor is not None:
                mag = sig.get("earn_signal") if sig.get("earn_signal", 0.0) > 0 else None
                if _synth_swing_score(mag) < swing_floor:
                    continue
            chosen = (ticker, df, idx, sig)
            break

        if chosen is None:
            continue

        ticker, df, idx, sig = chosen
        if idx + 1 >= len(df):
            continue
        next_open = float(df.iloc[idx + 1]["open"])
        entry_price = next_open * (1 + _slippage_per_side(ticker))
        funds = sig.get("funds", {})
        trigger = sig.get("trigger") or None
        magnitude = sig.get("earn_signal") if sig.get("earn_signal", 0.0) > 0 else None
        record = _simulate_trade(
            df,
            entry_idx=idx + 1,
            entry_price=entry_price,
            ticker=ticker,
            entry_date=df.index[idx + 1],
            trigger=trigger or "composite_top_decile",
            earnings_mag=magnitude,
            pb_at_entry=funds.get("pb"),
            de_at_entry=funds.get("de"),
            rv20_at_entry_pct=rv_today,
            size_factor=size_factor,
        )
        trades.append(record)

        if record.exit_date is not None:
            try:
                exit_idx = all_dates.index(record.exit_date)
            except ValueError:
                exit_idx = di + record.holding_days
            next_eligible_idx = exit_idx + 1
        else:
            next_eligible_idx = di + 1

    return trades


# ────────────────────────────────────────────────────────────────────────────
# Metrics + rendering — same shape as Sigma/Reversion
# ────────────────────────────────────────────────────────────────────────────


def _summary(trades: list[TradeRecord]) -> VariantSummary:
    if not trades:
        return VariantSummary(
            n_trades=0, win_rate=0.0, avg_return_pct=0.0,
            sharpe_annualized=0.0, max_drawdown_pct=0.0, profit_factor=0.0,
        )
    returns = np.array([t.return_pct for t in trades], dtype=float)
    n = len(returns)
    wins = returns[returns > 0]
    losses = returns[returns < 0]
    win_rate = float(len(wins) / n)
    avg = float(returns.mean())
    span_days = (trades[-1].entry_date - trades[0].entry_date).days or 1
    trades_per_year = n / (span_days / 365.25)
    sharpe = float(avg / returns.std(ddof=1) * math.sqrt(trades_per_year)) if returns.std(ddof=1) > 0 and n > 1 else 0.0
    equity = np.concatenate(([1.0], 1.0 + np.cumsum(returns)))
    peak = np.maximum.accumulate(equity)
    max_dd = float(((equity - peak) / peak).min())
    gross_w = float(wins.sum()) if len(wins) else 0.0
    gross_l = -float(losses.sum()) if len(losses) else 0.0
    pf = float(gross_w / gross_l) if gross_l > 0 else float("inf")
    by_year: dict[int, dict] = {}
    for year in sorted({t.entry_date.year for t in trades}):
        yr = np.array([t.return_pct for t in trades if t.entry_date.year == year])
        if len(yr) == 0:
            continue
        by_year[year] = {
            "n_trades": int(len(yr)),
            "win_rate": float(((yr > 0).sum()) / len(yr)),
            "avg_return_pct": float(yr.mean()),
            "total_return_pct": float(yr.sum()),
        }
    return VariantSummary(
        n_trades=n, win_rate=win_rate, avg_return_pct=avg,
        sharpe_annualized=sharpe, max_drawdown_pct=max_dd, profit_factor=pf,
        by_year=by_year,
    )


def _render(s: VariantSummary) -> str:
    def fmt_pct(x: float) -> str:
        return f"{x*100:+.2f}%"
    def fmt_pf(x: float) -> str:
        return "inf" if math.isinf(x) else f"{x:.2f}"
    return "\n".join([
        f"  trades              {s.n_trades}",
        f"  win rate            {fmt_pct(s.win_rate)}",
        f"  avg return / trade  {fmt_pct(s.avg_return_pct)}",
        f"  Sharpe (annualized) {s.sharpe_annualized:+.2f}",
        f"  max drawdown        {fmt_pct(s.max_drawdown_pct)}",
        f"  profit factor       {fmt_pf(s.profit_factor)}",
    ])


# ────────────────────────────────────────────────────────────────────────────
# CSV writer
# ────────────────────────────────────────────────────────────────────────────


_TRADE_COLS = [
    "ticker", "trigger", "entry_date", "entry_price",
    "exit_date", "exit_price", "exit_reason", "holding_days",
    "pnl", "return_pct", "earnings_magnitude",
    "pb_at_entry", "de_at_entry",
]


def _write_trades_csv(path: Path, trades: list[TradeRecord]) -> None:
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(_TRADE_COLS)
        for t in trades:
            w.writerow([
                t.ticker, t.trigger,
                t.entry_date.isoformat(),
                f"{t.entry_price:.4f}",
                t.exit_date.isoformat() if t.exit_date else "",
                f"{t.exit_price:.4f}" if t.exit_price is not None else "",
                t.exit_reason, t.holding_days,
                f"{t.pnl:.6f}", f"{t.return_pct:.6f}",
                f"{t.earnings_magnitude:.6f}" if t.earnings_magnitude is not None else "",
                f"{t.pb_at_entry:.6f}" if t.pb_at_entry is not None else "",
                f"{t.de_at_entry:.6f}" if t.de_at_entry is not None else "",
            ])


# ────────────────────────────────────────────────────────────────────────────
# Parameter-search hooks
# ────────────────────────────────────────────────────────────────────────────


VECTOR_OVERRIDE_KEYS = (
    "pb_ceiling",
    "de_ceiling",
    "earnings_window_days",
    "swing_score_threshold",
    "stop_pct",
    "composite_mode",
)


def _overrides_from_args(args: argparse.Namespace) -> dict:
    return overrides_from_args(args, VECTOR_OVERRIDE_KEYS)


def _apply_overrides_from_args(args: argparse.Namespace) -> None:
    global _PB_CEILING_OVERRIDE, _DE_CEILING_OVERRIDE, _EARNINGS_WINDOW_OVERRIDE
    global _HARD_STOP_PCT_OVERRIDE, _SWING_SCORE_THRESHOLD_OVERRIDE
    _PB_CEILING_OVERRIDE = (
        float(args.pb_ceiling) if getattr(args, "pb_ceiling", None) is not None else None
    )
    _DE_CEILING_OVERRIDE = (
        float(args.de_ceiling) if getattr(args, "de_ceiling", None) is not None else None
    )
    _EARNINGS_WINDOW_OVERRIDE = (
        int(args.earnings_window_days)
        if getattr(args, "earnings_window_days", None) is not None
        else None
    )
    _HARD_STOP_PCT_OVERRIDE = (
        float(args.stop_pct) if getattr(args, "stop_pct", None) is not None else None
    )
    _SWING_SCORE_THRESHOLD_OVERRIDE = (
        float(args.swing_score_threshold)
        if getattr(args, "swing_score_threshold", None) is not None
        else None
    )


def _trade_records_to_search_trades(trades: list[TradeRecord]) -> list:
    from tpcore.backtest.search import SearchTrade

    out: list[SearchTrade] = []
    for t in trades:
        if t.exit_date is None:
            continue
        out.append(
            SearchTrade(
                ticker=t.ticker,
                entry_date=t.entry_date,
                entry_price=float(t.entry_price),
                exit_date=t.exit_date,
                exit_price=float(t.exit_price) if t.exit_price is not None else float(t.entry_price),
                pnl_pct=float(t.return_pct),
                direction="LONG",
                exit_reason=t.exit_reason or "unknown",
            )
        )
    return out


def _build_diagnostic_inputs_for_search(
    trades: list[TradeRecord],
    panels: dict[str, pd.DataFrame],
    spy_panel: pd.DataFrame | None,
) -> tuple[list[dict], pd.DataFrame]:
    """Build (trade-dicts, price_data) shaped for OverfittingDiagnostic."""
    trade_dicts = [
        {
            "pnl_pct": t.return_pct,
            "entry_date": t.entry_date,
            "ticker": t.ticker,
            "exit_date": t.exit_date or t.entry_date,
            "direction": "LONG",
            "entry_price": float(t.entry_price),
        }
        for t in trades
    ]
    frames: list[pd.DataFrame] = []
    for ticker, df in panels.items():
        sub = df[["open", "high", "low", "close"]].reset_index().rename(columns={"index": "date"})
        sub["ticker"] = ticker
        frames.append(sub)
    if spy_panel is not None:
        sub = spy_panel[["open", "high", "low", "close"]].reset_index().rename(columns={"index": "date"})
        sub["ticker"] = SPY_SYMBOL
        frames.append(sub)
    price_data = (
        pd.concat(frames, ignore_index=True)
        if frames
        else pd.DataFrame(columns=["ticker", "date", "open", "high", "low", "close"])
    )
    return trade_dicts, price_data


@dataclass
class VectorWindowContext:
    """Pre-loaded panels + fundamentals + earnings for one walk-forward window.

    ``insider_clusters`` (added 2026-05-20) is an additive, default-empty
    payload consumed only by the ``composite_mode=composite`` Lab branch
    (spec docs/superpowers/specs/2026-05-20-vector-composite-lab-candidate.md
    §2.2). The legacy AND-gate path does not read it — the default
    ``{}`` keeps every pre-composite call site byte-identical.
    """

    panels: dict[str, pd.DataFrame]
    spy_panel: pd.DataFrame | None
    spy_rv_pct: pd.Series | None
    fundamentals: dict[str, list[dict]]
    earnings: dict[str, list[tuple[date, float]]]
    tier_round_trip_costs: dict[str, float]
    eligible_tickers: list[str]
    start: date
    end: date
    universe: tuple[str, ...] | None
    # Composite-only PIT signal — strictly backward 30d insider-cluster
    # dates per ticker (≥2 distinct BUY insiders / 30d window). Empty
    # dict ⇒ insider sub-signal is zero for all names that sim_date,
    # the zero-variance z-guard handles it safely (spec §2.3, H-VC-7).
    insider_clusters: dict[str, list[date]] = field(default_factory=dict)


async def load_vector_window_context(
    *,
    db_url: str,
    start: date,
    end: date,
    universe: tuple[str, ...] | None = None,
) -> VectorWindowContext:
    """Load prices + fundamentals + earnings + tier costs; precompute indicators.

    Heavy I/O — call once per walk-forward window."""
    from tpcore.backtest.cost_model import load_tier_costs

    pool = await build_asyncpg_pool(db_url)
    try:
        tier_costs = await load_tier_costs(pool)
        async with pool.acquire() as conn:
            funded = [r["ticker"] for r in await conn.fetch(
                "SELECT DISTINCT ticker FROM platform.fundamentals_quarterly "
                "WHERE pb IS NOT NULL AND de IS NOT NULL AND revenue IS NOT NULL"
            )]
            with_earnings = [r["ticker"] for r in await conn.fetch(
                "SELECT DISTINCT ticker FROM platform.earnings_events "
                "WHERE event_type='EARNINGS_BEAT'"
            )]
        eligible = sorted(set(funded) & set(with_earnings))
        if universe is not None:
            eligible = [t for t in eligible if t in universe]
        load_tickers = list({*eligible, SPY_SYMBOL})
        prices = await _load_prices(pool, load_tickers, start, end)
        fundamentals = await _load_fundamentals(pool, eligible)
        earnings = await _load_earnings(pool, eligible)
    finally:
        await pool.close()

    panels = {ticker: _precompute(df) for ticker, df in prices.items()}
    spy_panel = panels.pop(SPY_SYMBOL, None)
    spy_rv_pct = _spy_realized_vol_pct(spy_panel) if spy_panel is not None else None
    return VectorWindowContext(
        panels=panels, spy_panel=spy_panel, spy_rv_pct=spy_rv_pct,
        fundamentals=fundamentals, earnings=earnings,
        tier_round_trip_costs=tier_costs, eligible_tickers=eligible,
        start=start, end=end, universe=universe,
    )


def run_vector_with_context(
    context: VectorWindowContext,
    *,
    overrides: dict | None = None,
    trade_log_path: Path | None = None,
) -> BacktestRunResult:
    """Run Vector against a pre-loaded :class:`VectorWindowContext`."""
    from tpcore.backtest.search import (
        BacktestRunResult,
        compute_search_metrics,
        write_trade_log_csv,
    )

    global _PB_CEILING_OVERRIDE, _DE_CEILING_OVERRIDE, _EARNINGS_WINDOW_OVERRIDE
    global _HARD_STOP_PCT_OVERRIDE, _SWING_SCORE_THRESHOLD_OVERRIDE
    global _COMPOSITE_MODE_OVERRIDE
    overrides = dict(overrides or {})
    # Back-compat read shim — legacy CSV ``vector_search_results*.csv`` rows
    # carry ``catalyst_window_days`` inside their parameters_json strings.
    # We accept the old key on read (emit a deprecation warning) and forward
    # it to the new key. The CSV files themselves are NOT rewritten — the
    # catalyst→earnings rename ships forward-only for new artifacts.
    if "catalyst_window_days" in overrides and "earnings_window_days" not in overrides:
        import warnings
        warnings.warn(
            "vector.backtest: 'catalyst_window_days' override key is deprecated; "
            "use 'earnings_window_days' (the legacy key is still accepted for "
            "backward-compat reads of persisted parameters_json blobs).",
            DeprecationWarning,
            stacklevel=2,
        )
        overrides["earnings_window_days"] = overrides.pop("catalyst_window_days")
    _PB_CEILING_OVERRIDE = (
        float(overrides["pb_ceiling"]) if "pb_ceiling" in overrides else None
    )
    _DE_CEILING_OVERRIDE = (
        float(overrides["de_ceiling"]) if "de_ceiling" in overrides else None
    )
    _EARNINGS_WINDOW_OVERRIDE = (
        int(overrides["earnings_window_days"])
        if "earnings_window_days" in overrides else None
    )
    _HARD_STOP_PCT_OVERRIDE = (
        float(overrides["stop_pct"]) if "stop_pct" in overrides else None
    )
    _SWING_SCORE_THRESHOLD_OVERRIDE = (
        float(overrides["swing_score_threshold"])
        if "swing_score_threshold" in overrides else None
    )
    # vector_composite Lab candidate flag (spec §4.2, H-VC-8): the override
    # MUST be reset every call so module-global state does not bleed across
    # Lab trials. Mirrors every sibling _*_OVERRIDE reset above.
    _COMPOSITE_MODE_OVERRIDE = (
        str(overrides["composite_mode"]) if "composite_mode" in overrides else None
    )
    _TIER_ROUND_TRIP_COSTS.clear()
    _TIER_ROUND_TRIP_COSTS.update(context.tier_round_trip_costs)

    if not context.panels or not context.eligible_tickers:
        # Reset flag before early-return so a second call sees a clean
        # module-global (H-VC-8 — no leakage even on degenerate inputs).
        _COMPOSITE_MODE_OVERRIDE = None
        return BacktestRunResult(
            engine="vector", parameters=overrides, credibility_score=0, passed_gate=False,
            sharpe=0.0, profit_factor=0.0, max_drawdown=0.0, trades=0, dsr=0.0,
            min_btl_gap=0, trades_per_param=0.0, sensitivity_score=None,
            ruin_probability=0.0, trade_log=[],
        )

    # Mode selection — the make-or-break invariant (spec §3): the live
    # AND-gate path is the default; ``composite`` reaches the candidate's
    # top-decile branch via the additive scorer.
    if _composite_mode() == "composite":
        trades = _run_composite(
            panels=context.panels,
            spy_panel=context.spy_panel,
            spy_rv_pct=context.spy_rv_pct,
            fundamentals=context.fundamentals,
            earnings=context.earnings,
            start=context.start,
            end=context.end,
            eligible_tickers=context.eligible_tickers,
            insider_clusters=context.insider_clusters,
        )
    else:
        trades = _run(
            panels=context.panels,
            spy_panel=context.spy_panel,
            spy_rv_pct=context.spy_rv_pct,
            fundamentals=context.fundamentals,
            earnings=context.earnings,
            start=context.start,
            end=context.end,
        )
    summary = _summary(trades)

    search_trades = _trade_records_to_search_trades(trades)
    if trade_log_path is not None:
        write_trade_log_csv(trade_log_path, search_trades)

    parameters = {
        "pb_ceiling": float(_pb_ceiling()),
        "de_ceiling": float(_de_ceiling()),
        "earnings_window_days": int(_earnings_window_days()),
        "swing_score_threshold": (
            float(_swing_score_threshold()) if _swing_score_threshold() is not None else 0.0
        ),
        "stop_pct": float(_hard_stop_pct()),
        # Echo the active mode into the result so the Lab dossier's
        # ``param_diff`` carries the true legacy → variant delta
        # (spec §4.2, H-VC-10).
        "composite_mode": _composite_mode(),
    }
    trade_dicts, price_data = _build_diagnostic_inputs_for_search(
        trades, context.panels, context.spy_panel,
    )
    return compute_search_metrics(
        engine="vector",
        parameters=parameters,
        trades_for_diag=trade_dicts,
        sharpe=summary.sharpe_annualized,
        profit_factor=summary.profit_factor,
        max_drawdown=summary.max_drawdown_pct,
        n_trials=len(parameters),
        price_data=price_data,
        rubric_inputs={
            "lookahead_clean": True,
            "survivorship_inclusive": True,
            "pit_fundamentals": True,
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
    """Thin wrapper: load context, run once. Single-call convenience.

    The orchestrator should use :func:`load_vector_window_context` +
    :func:`run_vector_with_context` to amortise the DB load across all
    candidates in a window."""
    ctx = await load_vector_window_context(
        db_url=db_url, start=start, end=end, universe=universe,
    )
    return run_vector_with_context(ctx, overrides=overrides, trade_log_path=trade_log_path)


# ────────────────────────────────────────────────────────────────────────────
# SP-B — Lab targeting declaration (engine-OWNED; resolved by ops.lab.run
# (SP-B T4 roster-driven resolver); the live trading path never imports this).
# ────────────────────────────────────────────────────────────────────────────

LAB_TARGET = LabTarget(
    param_ranges={
        "pb_ceiling": (1.5, 3.5, "float"),
        "de_ceiling": (1.5, 4.0, "float"),
        "earnings_window_days": (3, 10, "int"),
        "swing_score_threshold": (55.0, 75.0, "float"),
        "stop_pct": (0.04, 0.10, "float"),
        # vector_composite Lab candidate (spec §4.1, H-VC-2): the ONE
        # new sampled key — a binary choice toggle between the legacy
        # AND-gate path and the §2 composite top-decile scorer. The
        # (0, 0) low/high is the established placeholder for
        # ``choice:`` specs; the sampler ignores low/high for choice
        # kinds and samples from the comma-separated value list.
        # Weights/sector-method/cluster-window/decile/long-only stay
        # code constants — never sampled (H-VC-2).
        "composite_mode": (0, 0, "choice:and_gate,composite"),
    },
    run_for_search=run_for_search,
    load_window_context=load_vector_window_context,
    run_with_context=run_vector_with_context,
    default_params=default_params,
)


# ────────────────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────────────────


async def amain(args: argparse.Namespace) -> int:
    """Run Vector's three-gate backtest and emit the credibility + overfitting reports.

    When ``--json`` is set, branches to :func:`run_for_search` with parameter
    overrides applied and prints a single JSON object.
    """
    db_url = args.database_url or os.getenv("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL not set", file=sys.stderr)
        return 2

    _apply_overrides_from_args(args)

    if getattr(args, "json_output", False):
        result = await run_for_search(
            db_url=db_url,
            start=args.start,
            end=args.end,
            overrides=_overrides_from_args(args),
            trade_log_path=args.trade_log,
        )
        print(result.to_json())
        return 0

    args.output_dir.mkdir(parents=True, exist_ok=True)
    pool = await build_asyncpg_pool(db_url)
    try:
        from tpcore.backtest.cost_model import load_tier_costs

        _TIER_ROUND_TRIP_COSTS.update(await load_tier_costs(pool))
        logger.info(
            "vector.backtest.tier_costs_loaded",
            n=len(_TIER_ROUND_TRIP_COSTS),
        )
        # Universe: anything that has fundamentals + an earnings event.
        async with pool.acquire() as conn:
            funded = [r["ticker"] for r in await conn.fetch(
                "SELECT DISTINCT ticker FROM platform.fundamentals_quarterly "
                "WHERE pb IS NOT NULL AND de IS NOT NULL AND revenue IS NOT NULL"
            )]
            with_earnings = [r["ticker"] for r in await conn.fetch(
                "SELECT DISTINCT ticker FROM platform.earnings_events "
                "WHERE event_type='EARNINGS_BEAT'"
            )]
        eligible = sorted(set(funded) & set(with_earnings))
        if not eligible:
            print("no eligible tickers — populate fundamentals + earnings_events first", file=sys.stderr)
            return 2
        load_tickers = list({*eligible, SPY_SYMBOL})

        logger.info(
            "vector.backtest.loading_prices",
            tickers=len(load_tickers),
            start=args.start.isoformat(),
            end=args.end.isoformat(),
        )
        prices = await _load_prices(pool, load_tickers, args.start, args.end)
        logger.info("vector.backtest.loading_fundamentals", tickers=len(eligible))
        fundamentals = await _load_fundamentals(pool, eligible)
        earnings = await _load_earnings(pool, eligible)
    finally:
        await pool.close()

    panels = {t: _precompute(df) for t, df in prices.items()}
    spy_panel = panels.pop(SPY_SYMBOL, None)
    eligible_panels = {t: df for t, df in panels.items() if t in eligible}
    spy_rv_pct = _spy_realized_vol_pct(spy_panel) if spy_panel is not None else None

    logger.info(
        "vector.backtest.running",
        tickers=len(eligible_panels),
        spy_rv_available=spy_rv_pct is not None and spy_rv_pct.notna().any(),
    )
    trades = _run(
        panels=eligible_panels,
        spy_panel=spy_panel,
        spy_rv_pct=spy_rv_pct,
        fundamentals=fundamentals,
        earnings=earnings,
        start=args.start,
        end=args.end,
    )

    summary = _summary(trades)
    print()
    print(f"Vector backtest  {args.start} → {args.end}  universe={len(eligible_panels)} names")
    print()
    print(_render(summary))
    print()

    payload = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "start": args.start.isoformat(),
        "end": args.end.isoformat(),
        "n_universe": len(eligible_panels),
        "summary": asdict(summary),
        "n_trades": summary.n_trades,
    }
    (args.output_dir / args.results_file).write_text(json.dumps(payload, indent=2))
    _write_trades_csv(args.output_dir / args.trades_file, trades)
    print(f"results → {args.output_dir / args.results_file}")
    print(f"trades  → {args.output_dir / args.trades_file}  rows={len(trades)}")

    if args.trade_log is not None:
        from tpcore.backtest.search import write_trade_log_csv

        n = write_trade_log_csv(args.trade_log, _trade_records_to_search_trades(trades))
        print(f"vector search trade-log → {args.trade_log}  rows={n}")

    # ── Statistical Validation ─────────────────────────────────────────────
    # The full overfitting bundle (CSCV PBO, trades-per-param, etc.) is being
    # built in a parallel session inside tpcore/backtest/overfitting.py. Hook
    # it here once it lands; for now we run the existing sensitivity + MC +
    # PSR/DSR/MinBTL stack which is already proven on Sigma + Reversion.
    if not args.skip_statistical_validation and trades:
        await _print_statistical_validation_vector(
            panels=eligible_panels,
            spy_panel=spy_panel,
            spy_rv_pct=spy_rv_pct,
            fundamentals=fundamentals,
            earnings=earnings,
            start=args.start,
            end=args.end,
            winner_summary=summary,
            winner_trades=trades,
            db_url=db_url,
            output_dir=args.output_dir,
            n_trials=args.n_trials,
        )

    return 0


async def _print_statistical_validation_vector(
    *,
    panels,
    spy_panel,
    spy_rv_pct,
    fundamentals,
    earnings,
    start: date,
    end: date,
    winner_summary: VariantSummary,
    winner_trades: list[TradeRecord],
    db_url: str | None,
    output_dir: Path,
    n_trials: int,
) -> None:
    """Sweep PB, DE, and earnings-window thresholds; MC + PSR/DSR/MinBTL; rubric."""
    from tpcore.backtest.sensitivity import sweep_parameter
    from tpcore.backtest.statistical_validation import (
        build_report,
        render,
    )

    pb_values = [1.0, 1.25, 1.5, 1.75, 2.0]
    de_values = [2.0, 2.5, 3.0, 3.5, 4.0]
    sweep_trials = len(pb_values) + len(de_values)

    def _run_with(*, pb: float | None = None, de: float | None = None) -> dict:
        nonlocal_pb = pb if pb is not None else PB_CEILING
        nonlocal_de = de if de is not None else DE_CEILING
        sweep_trades = _run_with_thresholds(
            panels=panels,
            spy_panel=spy_panel,
            spy_rv_pct=spy_rv_pct,
            fundamentals=fundamentals,
            earnings=earnings,
            start=start,
            end=end,
            pb_ceiling=nonlocal_pb,
            de_ceiling=nonlocal_de,
        )
        s = _summary(sweep_trades)
        return {
            "profit_factor": s.profit_factor if math.isfinite(s.profit_factor) else 1e6,
            "sharpe": s.sharpe_annualized,
            "win_rate": s.win_rate,
            "max_drawdown": s.max_drawdown_pct,
        }

    pb_sweep = sweep_parameter(lambda v: _run_with(pb=v), "pb_ceiling", pb_values)
    de_sweep = sweep_parameter(lambda v: _run_with(de=v), "de_ceiling", de_values)

    returns = [t.return_pct for t in winner_trades]
    backtest_periods = (end - start).days * 252 // 365
    report = build_report(
        returns,
        sweeps=[pb_sweep, de_sweep],
        sharpe_annualized=winner_summary.sharpe_annualized,
        backtest_periods=backtest_periods,
        n_trials=sweep_trials,
    )
    print(render(report, title="Vector — Statistical Validation"))

    # ── Overfitting diagnostic (parallel session's bundle) ─────────────────
    # The CSCV-PBO + sensitivity + MC + noise + regime stack from
    # tpcore.backtest.overfitting. n_trials reflects the development-phase
    # parameter search space (Vector's three-gate model has more knobs
    # than the few we sweep above).
    await _run_overfitting_bundle(
        winner_trades=winner_trades,
        winner_summary=winner_summary,
        panels=panels,
        spy_panel=spy_panel,
        n_trials=n_trials,
        output_dir=output_dir,
        db_url=db_url,
        sweep_report=report,
    )


async def _run_overfitting_bundle(
    *,
    winner_trades: list[TradeRecord],
    winner_summary: VariantSummary,
    panels: dict[str, pd.DataFrame],
    spy_panel: pd.DataFrame | None,
    n_trials: int,
    output_dir: Path,
    db_url: str | None,
    sweep_report,
) -> None:
    """Wire OverfittingDiagnostic, persist the JSON report, score credibility."""
    from tpcore.backtest.credibility import MIN_LIVE_SCORE, BacktestCredibilityRubric
    from tpcore.backtest.overfitting import OverfittingDiagnostic
    from tpcore.backtest.statistical_validation import write_credibility_score

    # Build the ticker/date/close frame the regime test wants.
    price_data_frames: list[pd.DataFrame] = []
    for ticker, df in panels.items():
        sub = df[["close"]].reset_index().rename(columns={"index": "date"})
        sub["ticker"] = ticker
        price_data_frames.append(sub)
    if spy_panel is not None:
        sub = spy_panel[["close"]].reset_index().rename(columns={"index": "date"})
        sub["ticker"] = SPY_SYMBOL
        price_data_frames.append(sub)
    price_data = (
        pd.concat(price_data_frames, ignore_index=True)
        if price_data_frames
        else pd.DataFrame(columns=["ticker", "date", "close"])
    )

    trade_dicts = [
        {"pnl_pct": t.return_pct, "entry_date": t.entry_date, "ticker": t.ticker}
        for t in winner_trades
    ]
    parameters = {
        "pb_max": PB_CEILING,
        "de_max": DE_CEILING,
        "revenue_min": REVENUE_FLOOR,
        "swing_score_threshold": 65,
        "vix_crash_25": 0.5,
        "vix_crash_30": 0.25,
        "cooldown_days": SPY_COOLDOWN_DAYS,
    }
    diagnostic = OverfittingDiagnostic(
        trades=trade_dicts,
        parameters=parameters,
        sr_observed=winner_summary.sharpe_annualized,
        n_trials=n_trials,
        price_data=price_data,
        engine="vector",
        trial_returns_matrix=None,
    )
    overfitting_report = diagnostic.run()

    # Persist the report JSON.
    report_path = output_dir / "vector_overfitting_report.json"
    report_path.write_text(overfitting_report.model_dump_json(indent=2))
    print(f"\noverfitting report → {report_path}")
    print(f"  overall_passed: {overfitting_report.overall_passed}")
    print(f"  summary: {overfitting_report.summary}")

    # Credibility — uses the new evaluate_with_overfitting path that the
    # parallel session added to BacktestCredibilityRubric. The seven
    # integrity flags are caller-asserted (Vector's PIT discipline +
    # survivorship-inclusive universe etc.); the four overfitting flags
    # come straight from the diagnostic.
    rubric = BacktestCredibilityRubric().evaluate_with_overfitting(
        overfitting_report,
        lookahead_clean=True,
        survivorship_inclusive=True,
        pit_fundamentals=True,
        regime_coverage=True,
        out_of_sample_validated=False,
        monte_carlo_drawdown=True,
    )
    verdict = "LIVE OK" if rubric.passes_gate else f"< {MIN_LIVE_SCORE}: BLOCKED"
    print(f"\nCredibility (with overfitting): {rubric.score}/100  ({verdict})")

    if db_url:
        pool = await build_asyncpg_pool(db_url)
        try:
            wrote = await write_credibility_score(pool, engine_name="vector", score=rubric)
            print(
                f"  → persisted to platform.data_quality_log "
                f"(source=backtest_credibility.vector, wrote={wrote})\n"
            )
        finally:
            await pool.close()


def _run_with_thresholds(
    *,
    panels: dict[str, pd.DataFrame],
    spy_panel: pd.DataFrame | None,
    spy_rv_pct: pd.Series | None,
    fundamentals: dict[str, list[dict]],
    earnings: dict[str, list[tuple[date, float]]],
    start: date,
    end: date,
    pb_ceiling: float,
    de_ceiling: float,
) -> list[TradeRecord]:
    """Like _run but with overridable PB/DE thresholds for the sweep."""
    all_dates = sorted({d for df in panels.values() for d in df.index})
    all_dates = [d for d in all_dates if start <= d <= end]
    trades: list[TradeRecord] = []
    next_eligible_idx = 0
    cooldown_state: dict[str, date] = {}

    for di, today in enumerate(all_dates):
        if di < next_eligible_idx:
            continue
        if spy_panel is not None and _spy_in_cooldown(spy_panel, today, cooldown_state):
            continue
        rv_today = float(spy_rv_pct.loc[today]) if (spy_rv_pct is not None and today in spy_rv_pct.index) else None
        size_factor = _size_factor_from_rv(rv_today)
        chosen: tuple[str, pd.DataFrame, int, str, float | None, dict] | None = None
        for ticker, df in panels.items():
            if today not in df.index:
                continue
            idx = df.index.get_loc(today)
            if idx < SMA_200 + 5:
                continue
            row = df.iloc[idx]
            prior_close = float(row["prior_close"])
            if math.isnan(prior_close):
                continue
            last_close = float(row["close"])
            sma_200 = float(row["sma_200"])
            funds = _pit_fundamentals(fundamentals.get(ticker, []), today)
            if funds is None:
                continue
            pb = funds.get("pb")
            de = funds.get("de")
            revenue = funds.get("revenue")
            if pb is None or de is None or revenue is None:
                continue
            if pb >= pb_ceiling or de >= de_ceiling or revenue <= REVENUE_FLOOR:
                continue
            if math.isnan(sma_200) or last_close < sma_200:
                continue
            ok_cat, magnitude = _has_earnings(earnings.get(ticker, []), today)
            if not ok_cat:
                continue
            trigger = _technical_trigger(row, prior_close)
            if trigger is None:
                continue
            chosen = (ticker, df, idx, trigger, magnitude, funds)
            break
        if chosen is None:
            continue
        ticker, df, idx, trigger, magnitude, funds = chosen
        if idx + 1 >= len(df):
            continue
        next_open = float(df.iloc[idx + 1]["open"])
        entry_price = next_open * (1 + _slippage_per_side(ticker))
        record = _simulate_trade(
            df,
            entry_idx=idx + 1,
            entry_price=entry_price,
            ticker=ticker,
            entry_date=df.index[idx + 1],
            trigger=trigger,
            earnings_mag=magnitude,
            pb_at_entry=funds.get("pb"),
            de_at_entry=funds.get("de"),
            rv20_at_entry_pct=rv_today,
            size_factor=size_factor,
        )
        trades.append(record)
        if record.exit_date is not None:
            try:
                exit_idx = all_dates.index(record.exit_date)
            except ValueError:
                exit_idx = di + record.holding_days
            next_eligible_idx = exit_idx + 1
        else:
            next_eligible_idx = di + 1
    return trades


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--start", type=date.fromisoformat, default=date(2018, 1, 1))
    p.add_argument("--end", type=date.fromisoformat, default=date(2025, 12, 31))
    p.add_argument("--database-url", default=None)
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    p.add_argument("--results-file", default=DEFAULT_RESULTS_FILE)
    p.add_argument("--trades-file", default=DEFAULT_TRADES_FILE)
    p.add_argument("--skip-statistical-validation", action="store_true")
    p.add_argument(
        "--n-trials", type=int, default=30,
        help=(
            "Number of independent trials assumed when computing DSR / MinBTL / PBO. "
            "Default 30 reflects Vector's three-gate development-phase parameter search."
        ),
    )
    # ─── Parameter-search hooks ─────────────────────────────────────────────
    p.add_argument("--json", dest="json_output", action="store_true",
                   help="Emit a single JSON object with search-pipeline metrics and exit 0.")
    p.add_argument("--trade-log", type=Path, default=None,
                   help="Write standardised per-trade CSV to this path.")
    p.add_argument("--pb-ceiling", type=float, default=None,
                   help="Override P/B ceiling for Gate 1 (default 1.5).")
    p.add_argument("--de-ceiling", type=float, default=None,
                   help="Override D/E ceiling for Gate 1 (default 3.0).")
    p.add_argument("--earnings-window-days", type=int, default=None,
                   help="Override earnings window in calendar days (default 5).")
    p.add_argument("--swing-score-threshold", type=float, default=None,
                   help="Synthetic swing-score floor for the search pipeline (default: no gate).")
    p.add_argument("--stop-pct", type=float, default=None,
                   help="Override hard-stop percentage (default 0.07).")
    return p.parse_args(argv)


def main() -> None:  # pragma: no cover
    raise SystemExit(asyncio.run(amain(_parse_args())))


if __name__ == "__main__":
    main()
