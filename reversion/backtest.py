"""Backtest comparison: Reversion's earnings-quality gate vs no gate.

Both variants run over the same trading window using the SAME indicator
helpers and trade-simulation rules as the live engine — imported from
``reversion.plugs.setup_detection`` and modeled on
``reversion.plugs.execution_risk`` — so the science doesn't drift from
production.

Variants
--------
**baseline**       all qualifying setups → trades.
**quality-gated**  candidates with ``EarningsQualityGrade.LOW`` are
                   suppressed; the rest proceed. (Mirrors what the live
                   Reversion lifecycle does today.)

PIT integrity
-------------
* Prices come from ``platform.prices_daily``.
* Fundamentals come from ``platform.fundamentals_quarterly``. For each
  simulation date we use only filings with ``filing_date <= sim_date``.
* No FMP calls during the run — the cache must be pre-populated by
  ``scripts/backfill_fundamentals.py``.

Trade simulation (Reversion §4.2 rules)
---------------------------------------
* One position at a time; if a trade is still open, skip new entries.
* Entry: next bar's open × (1 ± slippage), depending on direction.
* Exits (LONG fade — entered BUY):
    - Day's low ≤ stop (entry × 0.92) → stopped out.
    - Day's high ≥ 20-day MA → fill 75% (Tier 1).
    - After Tier 1 fills, day's high ≥ 50-day MA → fill 25% (Tier 2).
    - 5-day time stop without touching 20-day MA → exit at next close.
* Exits (SHORT fade — entered SELL): symmetric — stop above entry, MAs
  below entry, "high" and "low" semantics flip.

Reality flag (FMP free tier)
----------------------------
``platform.fundamentals_quarterly`` only covers ~late-Feb-2025 onward
on the free tier (verified empirically). Default backtest window
(``--start 2025-03-01``) is chosen so the gate has data to act on from
day 1; before that the gated path returns zero trades regardless of
price action. Override ``--start`` at your own risk.
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
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd
import structlog

from reversion.models import Direction
from reversion.plugs.setup_detection import (
    BB_NUM_STD,
    BB_PERIOD,
    MA_50_PERIOD,
    MAX_ADX_FOR_REVERSION,
    MIN_AVG_VOLUME,
    MIN_PRICE,
    SPY_SYMBOL,
    Z_SCORE_THRESHOLD,
    ZSCORE_PERIOD,
    _bb_breach_consecutive,
    _compute_adx,
    _compute_bbands,
    _compute_rsi,
    _compute_zscore,
    _has_rsi_divergence,
    _is_hammer,
    _is_shooting_star,
    _score_exhaustion,
    _score_market_context,
    _score_statistical_extremity,
    _spy_realized_vol_proxy,
    _volume_ratio,
)
from tpcore.backtest.cli_overrides import overrides_from_args
from tpcore.backtest.cost_model import (
    slippage_per_side as _tpcore_slippage_per_side,
)
from tpcore.backtest.price_loader import load_prices
from tpcore.db import build_asyncpg_pool
from tpcore.fundamentals.earnings_quality import (
    EarningsQualityGrade,
    EarningsQualityResult,
    check_earnings_quality,
)
from tpcore.lab.target import LabTarget

if TYPE_CHECKING:  # pragma: no cover
    from tpcore.backtest.search import BacktestRunResult

logger = structlog.get_logger(__name__)

# ────────────────────────────────────────────────────────────────────────────
# Backtest knobs (mirror reversion.models / execution_risk)
# ────────────────────────────────────────────────────────────────────────────

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


HARD_STOP_PCT = 0.08
TIER1_FRACTION = 0.75
MAX_HOLD_DAYS = 30
TIME_STOP_DAYS = 5  # bars without touching 20-day MA → time-out exit
SCORE_THRESHOLD = 50  # plan §4.2 weak floor

# Default volume-climax threshold for a candidate to qualify (vol_ratio is
# 5-day-volume / 20-day-volume; ≥ this counts as climax). The search pipeline
# overrides via --volume-climax-multiplier.
VOLUME_CLIMAX_MULTIPLIER_DEFAULT = 1.0  # 1.0 ≈ "no climax gate" (always passes)

# Parameter-search overrides — None means use the module defaults above.
_HARD_STOP_PCT_OVERRIDE: float | None = None
_MAX_HOLD_DAYS_OVERRIDE: int | None = None
_VOLUME_CLIMAX_OVERRIDE: float | None = None


def _hard_stop_pct() -> float:
    return _HARD_STOP_PCT_OVERRIDE if _HARD_STOP_PCT_OVERRIDE is not None else HARD_STOP_PCT


def _max_hold_days() -> int:
    return _MAX_HOLD_DAYS_OVERRIDE if _MAX_HOLD_DAYS_OVERRIDE is not None else MAX_HOLD_DAYS


def _volume_climax_threshold() -> float:
    return (
        _VOLUME_CLIMAX_OVERRIDE
        if _VOLUME_CLIMAX_OVERRIDE is not None
        else VOLUME_CLIMAX_MULTIPLIER_DEFAULT
    )


def default_params() -> dict[str, Any]:
    """Current live defaults for EXACTLY this engine's
    ops.lab.run.PARAM_RANGES keys (SP3 O1 seam, spec §7.1). Pure — reads
    the module accessors, no DB. The parity test pins the keyset ==
    PARAM_RANGES['reversion']."""
    return {
        "z_threshold": float(Z_SCORE_THRESHOLD),
        "volume_climax_multiplier": float(_volume_climax_threshold()),
        "max_hold_days": int(_max_hold_days()),
        "stop_pct": float(_hard_stop_pct()),
    }


DEFAULT_OUTPUT_DIR = Path("backtests")
DEFAULT_RESULTS_FILE = "earnings_quality_backtest.json"
DEFAULT_REJECTED_FILE = "rejected_by_quality.csv"
DEFAULT_TRADES_FILE = "reversion_trades.csv"


# ────────────────────────────────────────────────────────────────────────────
# Data classes
# ────────────────────────────────────────────────────────────────────────────


@dataclass
class TradeRecord:
    variant: str
    ticker: str
    direction: str  # "long" or "short"
    entry_date: date
    entry_price: float
    tier1_exit_date: date | None = None
    tier1_exit_price: float | None = None
    tier2_exit_date: date | None = None
    tier2_exit_price: float | None = None
    stopped_out: bool = False
    timed_out: bool = False
    holding_days: int = 0
    pnl: float = 0.0
    return_pct: float = 0.0
    quality_grade: str | None = None
    fcf_to_ni: float | None = None
    accruals: float | None = None
    # Diagnostic snapshot at entry (consumed by reversion/diagnose_backtest.py)
    z_score_at_entry: float | None = None
    rsi_at_entry: float | None = None
    adx_at_entry: float | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("entry_date", "tier1_exit_date", "tier2_exit_date"):
            if d[k] is not None:
                d[k] = d[k].isoformat()
        return d

    @property
    def exit_reason(self) -> str:
        """Categorical label for the exit: 'stop' | 'time_out' | 'target' | 'max_hold'."""
        if self.stopped_out:
            return "stop"
        if self.timed_out:
            return "time_out"
        if self.tier1_exit_date is not None and self.tier2_exit_date is not None:
            return "target"  # Tier 1 + Tier 2 both filled — clean reversion
        return "max_hold"  # ran out the 30-day cap without hitting target or stop


@dataclass
class VariantSummary:
    variant: str
    n_trades: int
    win_rate: float
    avg_return_pct: float
    sharpe_annualized: float
    max_drawdown_pct: float
    profit_factor: float
    by_year: dict[int, dict] = field(default_factory=dict)


# ────────────────────────────────────────────────────────────────────────────
# Data loading
# ────────────────────────────────────────────────────────────────────────────


async def _load_prices(
    pool, tickers: list[str], start: date, end: date
) -> dict[str, pd.DataFrame]:
    # Lean P5.3 (#2): thin delegate to the shared tpcore loader. The
    # min-bar floor (MA_50_PERIOD + 5) is reversion's intentional
    # divergence, preserved via the explicit ``min_bars`` parameter.
    return await load_prices(
        pool, tickers, start, end, min_bars=MA_50_PERIOD + 5
    )


async def _load_fundamentals(pool, tickers: list[str]) -> dict[str, list[dict]]:
    """Pull every cached row for ``tickers``. PIT filtering is done in-loop."""
    sql = """
        SELECT ticker, filing_date, period_end_date, period_label,
               net_income, fcf, operating_cash_flow, capex, revenue,
               total_assets, total_liabilities, current_assets, current_liabilities,
               receivables, cash_and_equivalents, shares_outstanding
        FROM platform.fundamentals_quarterly
        WHERE ticker = ANY($1)
        ORDER BY ticker, filing_date DESC
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, tickers)
    by_ticker: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_ticker[r["ticker"]].append(
            {
                "filing_date": r["filing_date"],
                "period_end_date": r["period_end_date"],
                "period": r["period_label"],
                "net_income": Decimal(str(r["net_income"])) if r["net_income"] is not None else None,
                "fcf": Decimal(str(r["fcf"])) if r["fcf"] is not None else None,
                "revenue": Decimal(str(r["revenue"])) if r["revenue"] is not None else None,
                "receivables": Decimal(str(r["receivables"])) if r["receivables"] is not None else None,
                "capex": Decimal(str(r["capex"])) if r["capex"] is not None else None,
                "total_assets": Decimal(str(r["total_assets"])) if r["total_assets"] is not None else None,
                "total_liabilities": Decimal(str(r["total_liabilities"])) if r["total_liabilities"] is not None else None,
                "current_assets": Decimal(str(r["current_assets"])) if r["current_assets"] is not None else None,
                "current_liabilities": Decimal(str(r["current_liabilities"])) if r["current_liabilities"] is not None else None,
                "cash_and_equivalents": Decimal(str(r["cash_and_equivalents"])) if r["cash_and_equivalents"] is not None else None,
                "shares_outstanding": Decimal(str(r["shares_outstanding"])) if r["shares_outstanding"] is not None else None,
                "operating_cash_flow": Decimal(str(r["operating_cash_flow"])) if r["operating_cash_flow"] is not None else None,
            }
        )
    return by_ticker


def _pit_fundamentals(rows: list[dict], as_of: date) -> dict | None:
    """Latest filing with ``filing_date <= as_of``, or None.

    Returns a payload in the same shape ``check_earnings_quality`` expects:
    latest period at the top level + ``history`` of earlier rows.
    """
    eligible = [r for r in rows if r["filing_date"] <= as_of]
    if not eligible:
        return None
    latest = eligible[0]
    history = eligible[1:]
    out = dict(latest)
    out["history"] = history
    return out


# ────────────────────────────────────────────────────────────────────────────
# Indicator precompute (per ticker, full window)
# ────────────────────────────────────────────────────────────────────────────


def _precompute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["adx"] = _compute_adx(df)
    df["rsi"] = _compute_rsi(df["close"])
    df["z_score"] = _compute_zscore(df["close"])
    sma, upper, lower = _compute_bbands(df, period=BB_PERIOD, num_std=BB_NUM_STD)
    df["bb_mid"] = sma
    df["bb_upper"] = upper
    df["bb_lower"] = lower
    df["ma_50"] = df["close"].rolling(MA_50_PERIOD, min_periods=MA_50_PERIOD).mean()
    return df


# ────────────────────────────────────────────────────────────────────────────
# Per-day candidate scan (Reversion logic, lifted to work on precomputed panels)
# ────────────────────────────────────────────────────────────────────────────


@dataclass
class _DayCandidate:
    ticker: str
    direction: Direction
    score: float
    z_score: float
    adx: float
    rsi: float
    bb_breach_days: int
    vol_ratio: float
    has_reversal: bool
    has_divergence: bool
    panel_idx: int  # index into the ticker's panel for this date
    # Captured for the trade simulator:
    ma_20: float
    ma_50: float
    last_close: float


def _scan_day(
    today: date,
    panels: dict[str, pd.DataFrame],
    spy_panel: pd.DataFrame | None,
    *,
    z_threshold: float = Z_SCORE_THRESHOLD,
) -> list[_DayCandidate]:
    """Run Reversion's setup detection for ``today``, returning all qualifiers."""
    spy_z = float("nan")
    vix = float("nan")
    if spy_panel is not None and today in spy_panel.index:
        spy_pos = spy_panel.index.get_loc(today)
        if spy_pos >= ZSCORE_PERIOD + 5:
            spy_z = float(spy_panel["z_score"].iloc[spy_pos])
            vix = _spy_realized_vol_proxy(spy_panel["close"].iloc[: spy_pos + 1])

    candidates: list[_DayCandidate] = []
    for ticker, df in panels.items():
        if today not in df.index:
            continue
        idx = df.index.get_loc(today)
        if idx < MA_50_PERIOD + 5:
            continue
        row = df.iloc[idx]
        last_close = float(row["close"])
        avg_vol_20 = float(df["volume"].iloc[max(0, idx - ZSCORE_PERIOD + 1) : idx + 1].mean())
        if last_close < float(MIN_PRICE) or avg_vol_20 < MIN_AVG_VOLUME:
            continue
        adx = float(row["adx"])
        if math.isnan(adx) or adx > MAX_ADX_FOR_REVERSION:
            continue
        z = float(row["z_score"])
        if math.isnan(z) or abs(z) < z_threshold:
            continue
        direction = Direction.LONG if z < 0 else Direction.SHORT

        rsi = float(row["rsi"])
        upper_now = float(row["bb_upper"])
        lower_now = float(row["bb_lower"])
        if math.isnan(upper_now) or math.isnan(lower_now):
            continue

        bb_breach = _bb_breach_consecutive(
            df["close"].iloc[: idx + 1], df["bb_upper"].iloc[: idx + 1], df["bb_lower"].iloc[: idx + 1]
        )
        vol_ratio = _volume_ratio(df["volume"].iloc[: idx + 1])
        if vol_ratio < _volume_climax_threshold():
            # Volume-climax gate (search-overridable). ≤ default 1.0 → no-op.
            continue

        last = df.iloc[idx]
        if direction is Direction.LONG:
            reversal = _is_hammer(
                float(last["open"]), float(last["high"]), float(last["low"]), last_close
            )
        else:
            reversal = _is_shooting_star(
                float(last["open"]), float(last["high"]), float(last["low"]), last_close
            )

        rsi_series = df["rsi"].iloc[: idx + 1]
        divergence = _has_rsi_divergence(df["close"].iloc[: idx + 1], rsi_series, direction)

        ma_50 = float(row["ma_50"])
        ma_20 = float(row["bb_mid"])
        if math.isnan(ma_50) or math.isnan(ma_20):
            continue

        se = _score_statistical_extremity(z, bb_breach, rsi)
        ec = _score_exhaustion(vol_ratio, reversal, divergence)
        mc = _score_market_context(z_score=z, spy_z_score=spy_z, vix_value=vix, direction=direction)
        score = se + ec + mc
        if score < SCORE_THRESHOLD:
            continue

        candidates.append(
            _DayCandidate(
                ticker=ticker,
                direction=direction,
                score=score,
                z_score=z,
                adx=adx,
                rsi=rsi,
                bb_breach_days=bb_breach,
                vol_ratio=vol_ratio,
                has_reversal=reversal,
                has_divergence=divergence,
                panel_idx=idx,
                ma_20=ma_20,
                ma_50=ma_50,
                last_close=last_close,
            )
        )
    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates


# ────────────────────────────────────────────────────────────────────────────
# Trade simulation (Reversion exit rules)
# ────────────────────────────────────────────────────────────────────────────


def _simulate_trade(
    df: pd.DataFrame,
    *,
    entry_idx: int,
    direction: Direction,
    entry_price: float,
    target_20ma: float,
    target_50ma: float,
    variant: str,
    ticker: str,
    entry_date: date,
    quality_grade: str | None,
    fcf_to_ni: float | None,
    accruals: float | None,
    z_score_at_entry: float | None = None,
    rsi_at_entry: float | None = None,
    adx_at_entry: float | None = None,
) -> TradeRecord:
    """Walk forward applying Reversion's exit rules.

    Stop sign convention: LONG stop = entry × (1 − 0.08); SHORT stop =
    entry × (1 + 0.08). Time stop after 5 bars without touching the 20-day
    MA. Tier 1 = 75% qty at the 20-MA; Tier 2 = 25% at the 50-MA.
    """
    is_long = direction is Direction.LONG
    if is_long:
        stop_price = entry_price * (1.0 - _hard_stop_pct())
    else:
        stop_price = entry_price * (1.0 + _hard_stop_pct())
    tier1_qty = TIER1_FRACTION
    tier2_qty = 1.0 - TIER1_FRACTION
    record = TradeRecord(
        variant=variant,
        ticker=ticker,
        direction=direction.value,
        entry_date=entry_date,
        entry_price=entry_price,
        quality_grade=quality_grade,
        fcf_to_ni=fcf_to_ni,
        accruals=accruals,
        z_score_at_entry=z_score_at_entry,
        rsi_at_entry=rsi_at_entry,
        adx_at_entry=adx_at_entry,
    )

    bars_left = min(_max_hold_days(), len(df) - entry_idx - 1)
    pnl = 0.0
    bars_without_touching_20ma = 0

    for i in range(1, bars_left + 1):
        bar = df.iloc[entry_idx + i]
        high = float(bar["high"])
        low = float(bar["low"])

        # Stop check first (most punitive).
        stop_hit = (is_long and low <= stop_price) or (not is_long and high >= stop_price)
        if stop_hit:
            sell_px = stop_price * ((1.0 - _slippage_per_side(ticker)) if is_long else (1.0 + _slippage_per_side(ticker)))
            remaining = (tier1_qty if record.tier1_exit_date is None else 0.0) + tier2_qty
            pnl += remaining * (sell_px - entry_price) * (1 if is_long else -1)
            record.stopped_out = True
            record.tier2_exit_date = bar.name
            record.tier2_exit_price = sell_px
            record.holding_days = i
            break

        # Tier 1 fill?
        tier1_hit = (is_long and high >= target_20ma) or (not is_long and low <= target_20ma)
        if record.tier1_exit_date is None and tier1_hit:
            sell_px = target_20ma * ((1.0 - _slippage_per_side(ticker)) if is_long else (1.0 + _slippage_per_side(ticker)))
            pnl += tier1_qty * (sell_px - entry_price) * (1 if is_long else -1)
            record.tier1_exit_date = bar.name
            record.tier1_exit_price = sell_px
            bars_without_touching_20ma = 0  # reset on touch

        # Tier 2 fill — only after tier 1.
        tier2_hit = (is_long and high >= target_50ma) or (not is_long and low <= target_50ma)
        if record.tier1_exit_date is not None and tier2_hit:
            sell_px = target_50ma * ((1.0 - _slippage_per_side(ticker)) if is_long else (1.0 + _slippage_per_side(ticker)))
            pnl += tier2_qty * (sell_px - entry_price) * (1 if is_long else -1)
            record.tier2_exit_date = bar.name
            record.tier2_exit_price = sell_px
            record.holding_days = i
            break

        # Time stop bookkeeping (only before tier 1 fills).
        if record.tier1_exit_date is None:
            touched = (is_long and high >= target_20ma) or (not is_long and low <= target_20ma)
            if not touched:
                bars_without_touching_20ma += 1
            else:
                bars_without_touching_20ma = 0
            if bars_without_touching_20ma >= TIME_STOP_DAYS:
                # Force-close at this bar's close.
                close = float(bar["close"])
                sell_px = close * ((1.0 - _slippage_per_side(ticker)) if is_long else (1.0 + _slippage_per_side(ticker)))
                remaining = tier1_qty + tier2_qty
                pnl += remaining * (sell_px - entry_price) * (1 if is_long else -1)
                record.timed_out = True
                record.tier2_exit_date = bar.name
                record.tier2_exit_price = sell_px
                record.holding_days = i
                break
    else:
        # max-hold expired without exit.
        bar = df.iloc[entry_idx + bars_left]
        sell_px = float(bar["close"]) * ((1.0 - _slippage_per_side(ticker)) if is_long else (1.0 + _slippage_per_side(ticker)))
        remaining = (tier1_qty if record.tier1_exit_date is None else 0.0) + tier2_qty
        pnl += remaining * (sell_px - entry_price) * (1 if is_long else -1)
        record.tier2_exit_date = bar.name
        record.tier2_exit_price = sell_px
        record.holding_days = bars_left

    record.pnl = pnl
    record.return_pct = pnl / entry_price if entry_price else 0.0
    return record


# ────────────────────────────────────────────────────────────────────────────
# Variant runner
# ────────────────────────────────────────────────────────────────────────────


def _run_variant(
    *,
    variant: str,
    panels: dict[str, pd.DataFrame],
    spy_panel: pd.DataFrame | None,
    fundamentals: dict[str, list[dict]],
    start: date,
    end: date,
    z_threshold: float = Z_SCORE_THRESHOLD,
    filter_mode: str = "none",
) -> tuple[list[TradeRecord], list[TradeRecord]]:
    """Walk every trading day. Returns ``(trades, rejected_trades)``.

    ``z_threshold`` overrides the ``Z_SCORE_THRESHOLD`` module default.
    ``filter_mode`` controls the earnings-quality filter:

    * ``"none"``       — no EQ filter (baseline).
    * ``"not_low"``    — reject LOW or no-data (current production gate).
    * ``"high_only"``  — require HIGH (combined-filter variant).

    ``rejected_trades`` is non-empty only on the baseline pass — for
    each baseline trade whose grade *would* have failed the
    ``"not_low"`` filter, we stash the would-be P&L for CSV export.
    """
    all_dates = sorted({d for df in panels.values() for d in df.index})
    all_dates = [d for d in all_dates if start <= d <= end]

    trades: list[TradeRecord] = []
    rejected: list[TradeRecord] = []
    next_eligible_idx = 0

    for di, today in enumerate(all_dates):
        if di < next_eligible_idx:
            continue
        candidates = _scan_day(today, panels, spy_panel, z_threshold=z_threshold)
        if not candidates:
            continue

        # Try candidates in score order; first one to pass the filter wins.
        chosen: _DayCandidate | None = None
        chosen_grade: str | None = None
        chosen_ratio: tuple[float | None, float | None] = (None, None)
        for cand in candidates:
            grade, fcf_ni, accr = _grade_or_none(fundamentals.get(cand.ticker, []), today)
            if not _passes_filter(grade, filter_mode):
                continue
            chosen = cand
            chosen_grade = grade.value if grade else "no_data"
            chosen_ratio = (
                (float(fcf_ni) if fcf_ni is not None else None),
                (float(accr) if accr is not None else None),
            )
            break

        if chosen is None:
            continue

        # Place the trade.
        df = panels[chosen.ticker]
        idx = chosen.panel_idx
        if idx + 1 >= len(df):
            continue
        next_open = float(df.iloc[idx + 1]["open"])
        is_long = chosen.direction is Direction.LONG
        entry_price = next_open * (
            1.0 + _slippage_per_side(chosen.ticker)
            if is_long
            else 1.0 - _slippage_per_side(chosen.ticker)
        )
        record = _simulate_trade(
            df,
            entry_idx=idx + 1,
            direction=chosen.direction,
            entry_price=entry_price,
            target_20ma=chosen.ma_20,
            target_50ma=chosen.ma_50,
            variant=variant,
            ticker=chosen.ticker,
            entry_date=df.index[idx + 1],
            quality_grade=chosen_grade,
            fcf_to_ni=chosen_ratio[0],
            accruals=chosen_ratio[1],
            z_score_at_entry=float(chosen.z_score),
            rsi_at_entry=float(chosen.rsi),
            adx_at_entry=float(chosen.adx),
        )
        trades.append(record)

        # On the baseline pass, also note if THIS specific trade would have
        # been rejected by the quality gate, and capture the would-be P&L.
        if filter_mode == "none" and chosen_grade in ("low", "no_data"):
            rejected.append(record)

        if record.tier2_exit_date is not None:
            try:
                exit_idx = all_dates.index(record.tier2_exit_date)
            except ValueError:
                exit_idx = di + record.holding_days
            next_eligible_idx = exit_idx + 1
        else:
            next_eligible_idx = di + 1

    return trades, rejected


def _passes_filter(grade: EarningsQualityGrade | None, filter_mode: str) -> bool:
    """Return True if a candidate with this grade is allowed under ``filter_mode``."""
    if filter_mode == "none":
        return True
    if filter_mode == "not_low":
        return grade not in (EarningsQualityGrade.LOW, None)
    if filter_mode == "high_only":
        return grade is EarningsQualityGrade.HIGH
    raise ValueError(f"unknown filter_mode {filter_mode!r}")


def _grade_or_none(
    rows: list[dict], as_of: date
) -> tuple[EarningsQualityGrade | None, Decimal | None, Decimal | None]:
    """Run the earnings-quality screen against the latest PIT filing.

    Returns ``(grade, fcf_to_ni, accruals)``. All three None when no
    eligible filing exists.
    """
    payload = _pit_fundamentals(rows, as_of)
    if payload is None:
        return None, None, None
    result: EarningsQualityResult = check_earnings_quality(payload)
    return result.grade, result.fcf_to_ni_ratio, result.accruals_ratio


# ────────────────────────────────────────────────────────────────────────────
# Metrics
# ────────────────────────────────────────────────────────────────────────────


def _compute_summary(variant: str, trades: list[TradeRecord]) -> VariantSummary:
    if not trades:
        return VariantSummary(
            variant=variant, n_trades=0, win_rate=0.0, avg_return_pct=0.0,
            sharpe_annualized=0.0, max_drawdown_pct=0.0, profit_factor=0.0,
        )
    returns = np.array([t.return_pct for t in trades], dtype=float)
    n = len(returns)
    wins = returns[returns > 0]
    losses = returns[returns < 0]
    win_rate = float(len(wins) / n)
    avg_return = float(returns.mean())

    span_days = (trades[-1].entry_date - trades[0].entry_date).days or 1
    trades_per_year = n / (span_days / 365.25) if span_days else n
    if returns.std(ddof=1) > 0 and n > 1:
        sharpe = float(avg_return / returns.std(ddof=1) * math.sqrt(trades_per_year))
    else:
        sharpe = 0.0

    equity = np.concatenate(([1.0], 1.0 + np.cumsum(returns)))
    peak = np.maximum.accumulate(equity)
    drawdown = (equity - peak) / peak
    max_dd = float(drawdown.min())

    gross_wins = float(wins.sum()) if len(wins) else 0.0
    gross_losses = float(-losses.sum()) if len(losses) else 0.0
    profit_factor = float(gross_wins / gross_losses) if gross_losses > 0 else float("inf")

    by_year: dict[int, dict] = {}
    for year in sorted({t.entry_date.year for t in trades}):
        yr_returns = np.array([t.return_pct for t in trades if t.entry_date.year == year])
        yr_wins = yr_returns[yr_returns > 0]
        by_year[year] = {
            "n_trades": int(len(yr_returns)),
            "win_rate": float(len(yr_wins) / len(yr_returns)) if len(yr_returns) else 0.0,
            "avg_return_pct": float(yr_returns.mean()) if len(yr_returns) else 0.0,
            "total_return_pct": float(yr_returns.sum()),
        }

    return VariantSummary(
        variant=variant, n_trades=n, win_rate=win_rate, avg_return_pct=avg_return,
        sharpe_annualized=sharpe, max_drawdown_pct=max_dd, profit_factor=profit_factor,
        by_year=by_year,
    )


# ────────────────────────────────────────────────────────────────────────────
# Output
# ────────────────────────────────────────────────────────────────────────────


def _render(summaries: list[VariantSummary]) -> str:
    def fmt_pct(x: float) -> str:
        return f"{x*100:+.2f}%"

    def fmt_pf(x: float) -> str:
        return "inf" if math.isinf(x) else f"{x:.2f}"

    rows: list[tuple[str, list[str]]] = [
        ("trades", [str(s.n_trades) for s in summaries]),
        ("win rate", [fmt_pct(s.win_rate) for s in summaries]),
        ("avg return / trade", [fmt_pct(s.avg_return_pct) for s in summaries]),
        ("Sharpe (annualized)", [f"{s.sharpe_annualized:+.2f}" for s in summaries]),
        ("max drawdown", [fmt_pct(s.max_drawdown_pct) for s in summaries]),
        ("profit factor", [fmt_pf(s.profit_factor) for s in summaries]),
    ]
    width_label = max(len(r[0]) for r in rows + [("metric", [])])
    headers = [s.variant for s in summaries]
    col_w = max(
        max((len(v) for r in rows for v in r[1]), default=0),
        max(len(h) for h in headers),
    )
    out = []
    out.append("  " + "metric".ljust(width_label) + "    " + "    ".join(h.rjust(col_w) for h in headers))
    out.append("  " + "-" * width_label + "    " + "    ".join("-" * col_w for _ in headers))
    for label, vals in rows:
        out.append("  " + label.ljust(width_label) + "    " + "    ".join(v.rjust(col_w) for v in vals))
    return "\n".join(out)


def _write_trades_csv(path: Path, trades: list[TradeRecord]) -> None:
    """One row per trade with everything the diagnostic script needs."""
    with path.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "ticker", "direction", "entry_date", "entry_price",
                "tier1_exit_date", "tier1_exit_price",
                "tier2_exit_date", "tier2_exit_price",
                "exit_reason", "stopped_out", "timed_out", "holding_days",
                "pnl", "return_pct", "quality_grade",
                "fcf_to_ni", "accruals",
                "z_score_at_entry", "rsi_at_entry", "adx_at_entry",
            ]
        )
        for t in trades:
            writer.writerow(
                [
                    t.ticker, t.direction, t.entry_date.isoformat(),
                    f"{t.entry_price:.4f}",
                    t.tier1_exit_date.isoformat() if t.tier1_exit_date else "",
                    f"{t.tier1_exit_price:.4f}" if t.tier1_exit_price is not None else "",
                    t.tier2_exit_date.isoformat() if t.tier2_exit_date else "",
                    f"{t.tier2_exit_price:.4f}" if t.tier2_exit_price is not None else "",
                    t.exit_reason,
                    str(t.stopped_out).lower(), str(t.timed_out).lower(),
                    t.holding_days,
                    f"{t.pnl:.6f}", f"{t.return_pct:.6f}",
                    t.quality_grade or "",
                    f"{t.fcf_to_ni:.4f}" if t.fcf_to_ni is not None else "",
                    f"{t.accruals:.6f}" if t.accruals is not None else "",
                    f"{t.z_score_at_entry:.4f}" if t.z_score_at_entry is not None else "",
                    f"{t.rsi_at_entry:.4f}" if t.rsi_at_entry is not None else "",
                    f"{t.adx_at_entry:.4f}" if t.adx_at_entry is not None else "",
                ]
            )


def _conclusion(baseline: VariantSummary, treatment: VariantSummary, rejected: list[TradeRecord]) -> str:
    """Free-form summary contrasting ``baseline`` to ``treatment``.

    ``treatment`` may be the quality-gate-only run or the combined-filter
    run; the wording uses the treatment's own variant label so the prose
    stays accurate regardless of which comparison the caller makes.
    """
    label = treatment.variant
    if baseline.sharpe_annualized == 0:
        sharpe_line = "baseline Sharpe is zero — no comparison possible"
    else:
        delta = (
            (treatment.sharpe_annualized - baseline.sharpe_annualized)
            / abs(baseline.sharpe_annualized)
        )
        direction = "improved" if delta > 0 else "did not improve"
        sharpe_line = (
            f"{label} {direction} Sharpe by {delta*100:+.1f}% "
            f"(baseline {baseline.sharpe_annualized:+.2f} → {label} {treatment.sharpe_annualized:+.2f})"
        )
    n_rejected = len(rejected)
    losers = sum(1 for t in rejected if t.return_pct < 0)
    winners = sum(1 for t in rejected if t.return_pct > 0)
    rejected_line = (
        f"the LOW-grade-only gate would have rejected {n_rejected} trades — "
        f"{losers} losers, {winners} winners "
        f"({(winners/n_rejected*100) if n_rejected else 0:.0f}% would have been profitable)"
    )
    return sharpe_line + "\n  " + rejected_line


# ────────────────────────────────────────────────────────────────────────────
# Parameter-search hooks
# ────────────────────────────────────────────────────────────────────────────


REVERSION_OVERRIDE_KEYS = (
    "z_threshold",
    "earnings_quality",
    "volume_climax_multiplier",
    "max_hold_days",
    "stop_pct",
)


_EARNINGS_QUALITY_TO_FILTER = {
    "HIGH": "high_only",
    "MEDIUM_AND_HIGH": "not_low",
    "NONE": "none",
}


def _overrides_from_args(args: argparse.Namespace) -> dict:
    return overrides_from_args(args, REVERSION_OVERRIDE_KEYS)


def _apply_overrides_from_args(args: argparse.Namespace) -> None:
    global _HARD_STOP_PCT_OVERRIDE, _MAX_HOLD_DAYS_OVERRIDE, _VOLUME_CLIMAX_OVERRIDE
    _HARD_STOP_PCT_OVERRIDE = (
        float(args.stop_pct) if getattr(args, "stop_pct", None) is not None else None
    )
    _MAX_HOLD_DAYS_OVERRIDE = (
        int(args.max_hold_days) if getattr(args, "max_hold_days", None) is not None else None
    )
    _VOLUME_CLIMAX_OVERRIDE = (
        float(args.volume_climax_multiplier)
        if getattr(args, "volume_climax_multiplier", None) is not None
        else None
    )


def _trade_records_to_search_trades(trades: list[TradeRecord]) -> list:
    from tpcore.backtest.search import SearchTrade

    out: list[SearchTrade] = []
    for t in trades:
        if t.tier2_exit_date is None and t.tier1_exit_date is None:
            continue
        exit_d = t.tier2_exit_date or t.tier1_exit_date
        exit_p = (
            t.tier2_exit_price
            if t.tier2_exit_price is not None
            else (t.tier1_exit_price if t.tier1_exit_price is not None else t.entry_price)
        )
        out.append(
            SearchTrade(
                ticker=t.ticker,
                entry_date=t.entry_date,
                entry_price=float(t.entry_price),
                exit_date=exit_d,
                exit_price=float(exit_p) if exit_p is not None else float(t.entry_price),
                pnl_pct=float(t.return_pct),
                direction=t.direction.upper(),
                exit_reason=t.exit_reason,
            )
        )
    return out


# Mega-cap universe matched to the standard Reversion backtest set so that
# search runs are apples-to-apples with the standard backtest's results.
_REVERSION_SEARCH_UNIVERSE = (
    "SPY", "QQQ", "IWM",
    "AAPL", "MSFT", "AMZN", "GOOGL", "META", "TSLA", "NVDA",
    "JPM", "V", "WMT", "DIS", "NFLX", "BA", "CAT", "GE", "GM", "F",
    "XOM", "CVX", "PFE", "JNJ", "MRK", "ABBV", "PG", "KO", "PEP",
    "MCD", "SBUX", "HD", "LOW", "TGT", "COST",
    "LMT", "RTX", "NOC", "GD",
    "SO", "DUK", "NEE",
    "PLTR", "UBER", "ABNB", "SNAP", "RBLX", "RIVN", "LCID", "FSLR",
)


@dataclass
class ReversionWindowContext:
    """Pre-loaded panels + fundamentals for one walk-forward window."""

    panels: dict[str, pd.DataFrame]
    spy_panel: pd.DataFrame | None
    fundamentals: dict[str, list[dict]]
    tier_round_trip_costs: dict[str, float]
    funded_tickers: list[str]
    start: date
    end: date
    universe: tuple[str, ...]


async def load_reversion_window_context(
    *,
    db_url: str,
    start: date,
    end: date,
    universe: tuple[str, ...] | None = None,
) -> ReversionWindowContext:
    """Load prices + fundamentals + tier costs; precompute indicators.

    Heavy I/O — call once per walk-forward window."""
    from tpcore.backtest.cost_model import load_tier_costs

    universe = universe or _REVERSION_SEARCH_UNIVERSE
    pool = await build_asyncpg_pool(db_url)
    try:
        tier_costs = await load_tier_costs(pool)
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT ticker FROM platform.fundamentals_quarterly
                WHERE ticker = ANY($1::text[])
                ORDER BY ticker
                """,
                list(universe),
            )
        funded_tickers = [r["ticker"] for r in rows]
        load_tickers = list({*funded_tickers, SPY_SYMBOL})
        prices = await _load_prices(pool, load_tickers, start, end)
        fundamentals = await _load_fundamentals(pool, funded_tickers)
    finally:
        await pool.close()

    panels = {ticker: _precompute_indicators(df) for ticker, df in prices.items()}
    spy_panel = panels.pop(SPY_SYMBOL, None)
    return ReversionWindowContext(
        panels=panels, spy_panel=spy_panel, fundamentals=fundamentals,
        tier_round_trip_costs=tier_costs, funded_tickers=funded_tickers,
        start=start, end=end, universe=universe,
    )


def run_reversion_with_context(
    context: ReversionWindowContext,
    *,
    overrides: dict | None = None,
    trade_log_path: Path | None = None,
) -> BacktestRunResult:
    """Run Reversion's combined-filter variant against a pre-loaded context."""
    from tpcore.backtest.search import (
        BacktestRunResult,
        compute_search_metrics,
        write_trade_log_csv,
    )

    global _HARD_STOP_PCT_OVERRIDE, _MAX_HOLD_DAYS_OVERRIDE, _VOLUME_CLIMAX_OVERRIDE
    overrides = dict(overrides or {})
    _HARD_STOP_PCT_OVERRIDE = (
        float(overrides["stop_pct"]) if "stop_pct" in overrides else None
    )
    _MAX_HOLD_DAYS_OVERRIDE = (
        int(overrides["max_hold_days"]) if "max_hold_days" in overrides else None
    )
    _VOLUME_CLIMAX_OVERRIDE = (
        float(overrides["volume_climax_multiplier"])
        if "volume_climax_multiplier" in overrides else None
    )
    _TIER_ROUND_TRIP_COSTS.clear()
    _TIER_ROUND_TRIP_COSTS.update(context.tier_round_trip_costs)

    z_thr = float(overrides.get("z_threshold", 3.0))
    # Default to "NONE" (no EQ filter) when caller doesn't specify — the
    # search pipeline operates on a wider universe where fundamentals
    # coverage is sparse, and the EQ gate would reject most candidates.
    eq = overrides.get("earnings_quality", "NONE")
    filter_mode = _EARNINGS_QUALITY_TO_FILTER.get(eq, "none")

    if not context.panels or not context.funded_tickers:
        return BacktestRunResult(
            engine="reversion", parameters=overrides, credibility_score=0, passed_gate=False,
            sharpe=0.0, profit_factor=0.0, max_drawdown=0.0, trades=0, dsr=0.0,
            min_btl_gap=0, trades_per_param=0.0, sensitivity_score=None,
            ruin_probability=0.0, trade_log=[],
        )

    trades, _ = _run_variant(
        variant="search",
        panels=context.panels,
        spy_panel=context.spy_panel,
        fundamentals=context.fundamentals,
        start=context.start,
        end=context.end,
        z_threshold=z_thr,
        filter_mode=filter_mode,
    )
    summary = _compute_summary("search", trades)

    search_trades = _trade_records_to_search_trades(trades)
    if trade_log_path is not None:
        write_trade_log_csv(trade_log_path, search_trades)

    parameters = {
        "z_threshold": z_thr,
        "earnings_quality": eq,
        "volume_climax_multiplier": _volume_climax_threshold(),
        "max_hold_days": int(_max_hold_days()),
        "stop_pct": float(_hard_stop_pct()),
    }
    trades_for_diag = _trades_to_diagnostic_dicts(trades)
    price_data = _panels_to_price_data(context.panels, context.spy_panel)
    return compute_search_metrics(
        engine="reversion",
        parameters=parameters,
        trades_for_diag=trades_for_diag,
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

    The parameter-search orchestrator should use
    :func:`load_reversion_window_context` + :func:`run_reversion_with_context`
    so the DB load is amortised across all candidates."""
    ctx = await load_reversion_window_context(
        db_url=db_url, start=start, end=end, universe=universe,
    )
    return run_reversion_with_context(ctx, overrides=overrides, trade_log_path=trade_log_path)


# ────────────────────────────────────────────────────────────────────────────
# SP-B — Lab targeting declaration (engine-OWNED; resolved by ops.lab.run
# (SP-B T4 roster-driven resolver); the live trading path never imports this).
# ────────────────────────────────────────────────────────────────────────────

LAB_TARGET = LabTarget(
    param_ranges={
        "z_threshold": (2.0, 4.0, "float"),
        "volume_climax_multiplier": (1.2, 3.0, "float"),
        "max_hold_days": (3, 12, "int"),
        "stop_pct": (0.04, 0.12, "float"),
    },
    run_for_search=run_for_search,
    load_window_context=load_reversion_window_context,
    run_with_context=run_reversion_with_context,
    default_params=default_params,
)


# ────────────────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────────────────


async def amain(args: argparse.Namespace) -> int:
    """Run Reversion's three-variant backtest and emit the credibility + overfitting reports.

    When ``--json`` is set, branches to :func:`run_for_search` over the
    Reversion winner variant (combined-filter) with parameter overrides.
    """
    db_url = args.database_url or os.getenv("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL not set — pass --database-url or export it.", file=sys.stderr)
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
            "reversion.backtest.tier_costs_loaded",
            n=len(_TIER_ROUND_TRIP_COSTS),
        )
        # Universe = anything that has fundamentals, PLUS SPY for market
        # context. After the Phase 1 universe expansion fundamentals_quarterly
        # holds 5,981 tickers — too many to backtest through the pooler
        # without a statement_timeout crash. Constrain to the 50-name
        # mega-cap backtest set used by Sigma so the credibility number
        # is apples-to-apples vs the pre-expansion 47-name run. To widen
        # later (e.g. via FMP Premium), drop the BACKTEST_UNIVERSE filter
        # below.
        _REVERSION_BACKTEST_UNIVERSE = (
            "SPY", "QQQ", "IWM",
            "AAPL", "MSFT", "AMZN", "GOOGL", "META", "TSLA", "NVDA",
            "JPM", "V", "WMT", "DIS", "NFLX", "BA", "CAT", "GE", "GM", "F",
            "XOM", "CVX", "PFE", "JNJ", "MRK", "ABBV", "PG", "KO", "PEP",
            "MCD", "SBUX", "HD", "LOW", "TGT", "COST",
            "LMT", "RTX", "NOC", "GD",
            "SO", "DUK", "NEE",
            "PLTR", "UBER", "ABNB", "SNAP", "RBLX", "RIVN", "LCID", "FSLR",
        )
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT ticker FROM platform.fundamentals_quarterly
                WHERE ticker = ANY($1::text[])
                ORDER BY ticker
                """,
                list(_REVERSION_BACKTEST_UNIVERSE),
            )
        funded_tickers = [r["ticker"] for r in rows]
        if not funded_tickers:
            print(
                "platform.fundamentals_quarterly is empty — populate the cache first via "
                "scripts/backfill_fundamentals.py.",
                file=sys.stderr,
            )
            return 2
        load_tickers = list({*funded_tickers, SPY_SYMBOL})

        logger.info(
            "reversion.backtest.loading_prices",
            tickers=len(load_tickers),
            start=args.start.isoformat(),
            end=args.end.isoformat(),
        )
        prices = await _load_prices(pool, load_tickers, args.start, args.end)
        logger.info("reversion.backtest.loading_fundamentals", tickers=len(funded_tickers))
        fundamentals = await _load_fundamentals(pool, funded_tickers)
    finally:
        await pool.close()

    if SPY_SYMBOL not in prices:
        print(
            "SPY missing from prices_daily for the window — sector-z and VIX proxy "
            "won't compute, market-context will score zero everywhere.",
            file=sys.stderr,
        )

    panels = {ticker: _precompute_indicators(df) for ticker, df in prices.items()}
    spy_panel = panels.pop(SPY_SYMBOL, None)

    logger.info("reversion.backtest.running_variant", variant="baseline", config="z=2.0, no EQ filter")
    baseline_trades, rejected = _run_variant(
        variant="baseline",
        panels=panels,
        spy_panel=spy_panel,
        fundamentals=fundamentals,
        start=args.start,
        end=args.end,
        z_threshold=2.0,
        filter_mode="none",
    )
    logger.info("reversion.backtest.running_variant", variant="quality-gated", config="z=2.0, reject LOW or no-data")
    gated_trades, _ = _run_variant(
        variant="quality-gated",
        panels=panels,
        spy_panel=spy_panel,
        fundamentals=fundamentals,
        start=args.start,
        end=args.end,
        z_threshold=2.0,
        filter_mode="not_low",
    )
    logger.info("reversion.backtest.running_variant", variant="combined-filter", config="z=3.0, require HIGH")
    combined_trades, _ = _run_variant(
        variant="combined-filter",
        panels=panels,
        spy_panel=spy_panel,
        fundamentals=fundamentals,
        start=args.start,
        end=args.end,
        z_threshold=3.0,
        filter_mode="high_only",
    )

    summaries = [
        _compute_summary("baseline", baseline_trades),
        _compute_summary("quality-gated", gated_trades),
        _compute_summary("combined-filter", combined_trades),
    ]

    print()
    print(
        f"Reversion earnings-quality backtest  {args.start} → {args.end}  "
        f"universe={len(panels)} names (+ SPY)"
    )
    print()
    print(_render(summaries))
    print()
    # Headline conclusion is baseline vs combined-filter (the new hypothesis).
    # Keep the gate-rejected trade audit visible too.
    print(_conclusion(summaries[0], summaries[2], rejected))
    print()

    payload = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "start": args.start.isoformat(),
        "end": args.end.isoformat(),
        "n_universe": len(panels),
        "n_with_fundamentals": len(fundamentals),
        "variants": {s.variant: asdict(s) for s in summaries},
        "rejected_trade_count": len(rejected),
    }
    results_path = args.output_dir / args.results_file
    results_path.write_text(json.dumps(payload, indent=2))
    print(f"results → {results_path}")

    # Per-trade CSV for the baseline variant — consumed by
    # reversion/diagnose_backtest.py for the diagnostic cuts.
    trades_path = args.output_dir / args.trades_file
    _write_trades_csv(trades_path, baseline_trades)
    print(f"baseline trades → {trades_path}  rows={len(baseline_trades)}")

    if rejected and not args.skip_rejected_csv:
        rejected_path = args.output_dir / args.rejected_file
        with rejected_path.open("w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(
                [
                    "ticker", "direction", "entry_date", "entry_price",
                    "tier1_exit_date", "tier1_exit_price",
                    "tier2_exit_date", "tier2_exit_price",
                    "stopped_out", "timed_out", "holding_days",
                    "pnl", "return_pct", "quality_grade",
                    "fcf_to_ni", "accruals",
                ]
            )
            for t in rejected:
                writer.writerow(
                    [
                        t.ticker, t.direction, t.entry_date.isoformat(),
                        f"{t.entry_price:.4f}",
                        t.tier1_exit_date.isoformat() if t.tier1_exit_date else "",
                        f"{t.tier1_exit_price:.4f}" if t.tier1_exit_price is not None else "",
                        t.tier2_exit_date.isoformat() if t.tier2_exit_date else "",
                        f"{t.tier2_exit_price:.4f}" if t.tier2_exit_price is not None else "",
                        str(t.stopped_out).lower(), str(t.timed_out).lower(),
                        t.holding_days,
                        f"{t.pnl:.6f}", f"{t.return_pct:.6f}",
                        t.quality_grade or "",
                        f"{t.fcf_to_ni:.4f}" if t.fcf_to_ni is not None else "",
                        f"{t.accruals:.6f}" if t.accruals is not None else "",
                    ]
                )
        print(f"rejected-by-quality → {rejected_path}  rows={len(rejected)}")

    if args.trade_log is not None:
        from tpcore.backtest.search import write_trade_log_csv

        n = write_trade_log_csv(
            args.trade_log, _trade_records_to_search_trades(combined_trades)
        )
        print(f"combined-filter search trade-log → {args.trade_log}  rows={n}")

    # ── Statistical Validation + credibility rubric ────────────────────────
    # Winner: combined-filter (z≥3, EQ=HIGH). Sweep z_threshold and filter_mode
    # around it; MC + PSR/DSR/MinBTL on the winner's trades; persist the
    # rubric score for the Capital Gate to read.
    if not args.skip_statistical_validation and combined_trades:
        await _print_statistical_validation_reversion(
            panels=panels,
            spy_panel=spy_panel,
            fundamentals=fundamentals,
            start=args.start,
            end=args.end,
            winner_summary=summaries[2],
            winner_trades=combined_trades,
            db_url=db_url,
        )

    # ── Nine-test Overfitting Diagnostic (plan §6) ───────────────────────────
    if not args.skip_statistical_validation and combined_trades:
        await _run_overfitting_diagnostic_reversion(
            winner_trades=combined_trades,
            winner_summary=summaries[2],
            panels=panels,
            spy_panel=spy_panel,
            output_dir=args.output_dir,
        )

    return 0


async def _print_statistical_validation_reversion(
    *,
    panels,
    spy_panel,
    fundamentals,
    start: date,
    end: date,
    winner_summary,
    winner_trades,
    db_url: str | None,
) -> None:
    """Sweep z_threshold and filter_mode; MC + PSR/DSR/MinBTL; rubric; persist."""
    from tpcore.backtest.sensitivity import sweep_parameter
    from tpcore.backtest.statistical_validation import (
        build_report,
        evaluate_rubric_from_report,
        render,
        render_rubric,
        write_credibility_score,
    )
    from tpcore.db import build_asyncpg_pool

    z_values = [2.0, 2.5, 3.0, 3.5]
    filter_modes = ["none", "not_low", "high_only"]
    n_trials = len(z_values) + len(filter_modes)

    def _run_with(*, z: float | None = None, mode: str | None = None) -> dict:
        trades, _ = _run_variant(
            variant="sweep",
            panels=panels,
            spy_panel=spy_panel,
            fundamentals=fundamentals,
            start=start,
            end=end,
            z_threshold=z if z is not None else 3.0,
            filter_mode=mode if mode is not None else "high_only",
        )
        s = _compute_summary("sweep", trades)
        return {
            "profit_factor": s.profit_factor if math.isfinite(s.profit_factor) else 1e6,
            "sharpe": s.sharpe_annualized,
            "win_rate": s.win_rate,
            "max_drawdown": s.max_drawdown_pct,
        }

    z_sweep = sweep_parameter(lambda v: _run_with(z=v), "z_threshold", z_values)
    mode_sweep = sweep_parameter(lambda v: _run_with(mode=v), "filter_mode", filter_modes)

    returns = [t.return_pct for t in winner_trades]
    backtest_periods = (end - start).days * 252 // 365  # approximate trading days
    report = build_report(
        returns,
        sweeps=[z_sweep, mode_sweep],
        sharpe_annualized=winner_summary.sharpe_annualized,
        backtest_periods=backtest_periods,
        n_trials=n_trials,
    )
    print(render(report, title="Reversion — Statistical Validation"))

    # Reversion uses fundamentals (PIT-safe via FMP cache), runs on a
    # survivorship-clean universe, and the validation here is in-sample only
    # (no held-out OOS yet).
    rubric = evaluate_rubric_from_report(
        report,
        lookahead_clean=True,
        survivorship_inclusive=True,
        pit_fundamentals=True,
        regime_coverage=True,
        out_of_sample_validated=False,
        monte_carlo_drawdown=True,
    )
    print(render_rubric(rubric))

    if db_url:
        pool = await build_asyncpg_pool(db_url)
        try:
            wrote = await write_credibility_score(pool, engine_name="reversion", score=rubric)
            print(
                f"  → persisted to platform.data_quality_log "
                f"(source=backtest_credibility.reversion, wrote={wrote})\n"
            )
        finally:
            await pool.close()


def _trades_to_diagnostic_dicts(trades: list[TradeRecord]) -> list[dict]:
    """Project Reversion TradeRecords onto the OverfittingDiagnostic schema.

    Reversion runs both directions; the ``direction`` field is ``"LONG"`` /
    ``"SHORT"`` upper-cased to match the diagnostic's expectation.
    """
    out: list[dict] = []
    for t in trades:
        exit_date = t.tier2_exit_date or t.tier1_exit_date or t.entry_date
        out.append(
            {
                "pnl_pct": float(t.return_pct),
                "entry_date": t.entry_date,
                "exit_date": exit_date,
                "direction": str(t.direction).upper(),
                "ticker": t.ticker,
                "entry_price": float(t.entry_price),
            }
        )
    return out


def _panels_to_price_data(
    panels: dict[str, pd.DataFrame], spy_panel: pd.DataFrame | None
) -> pd.DataFrame:
    """Stack indicator panels (plus SPY) into the long-form frame the
    diagnostic expects (columns: ticker, date, close, high, low, open).

    Including SPY here is what enables the diagnostic's regime-coverage
    test to compute its VIX proxy / ADX / 20-day-return classifiers.
    """
    frames: list[pd.DataFrame] = []
    cols = ["open", "high", "low", "close"]
    for ticker, panel in panels.items():
        df = panel[cols].copy().reset_index()
        df["ticker"] = ticker
        frames.append(df)
    if spy_panel is not None:
        spy_df = spy_panel[cols].copy().reset_index()
        spy_df["ticker"] = "SPY"
        frames.append(spy_df)
    if not frames:
        return pd.DataFrame(columns=["ticker", "date", "open", "high", "low", "close"])
    return pd.concat(frames, ignore_index=True)


async def _run_overfitting_diagnostic_reversion(
    *,
    winner_trades: list[TradeRecord],
    winner_summary: VariantSummary,
    panels: dict[str, pd.DataFrame],
    spy_panel: pd.DataFrame | None,
    output_dir: Path,
) -> None:
    """Run the nine-test overfitting diagnostic on the combined-filter variant.

    Saves ``backtests/reversion_overfitting_report.json`` and prints a
    plain-English summary plus the credibility score.
    """
    from tpcore.backtest.credibility import BacktestCredibilityRubric
    from tpcore.backtest.overfitting import OverfittingDiagnostic

    if not winner_trades:
        print("Overfitting diagnostic skipped — winner has no trades.")
        return

    trades = _trades_to_diagnostic_dicts(winner_trades)
    price_data = _panels_to_price_data(panels, spy_panel)

    diag = OverfittingDiagnostic(
        trades=trades,
        parameters={
            "z_threshold": 3.0,
            "quality_grade": "HIGH",
            "rsi_threshold": 25,
            "bb_consecutive_days": 2,
            "volume_ratio": 2.0,
        },
        sr_observed=float(winner_summary.sharpe_annualized),
        # Development parameters are now locked. n_trials reflects only the
        # distinct parameter combinations actually retained in the live
        # config — earlier exploratory sweeps no longer count once the
        # winner is frozen.
        n_trials=20,
        price_data=price_data,
        engine="reversion",
    )
    report = diag.run()

    out_path = output_dir / "reversion_overfitting_report.json"
    out_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")

    print()
    print("─── Reversion — Overfitting Diagnostic ───")
    print(report.summary)
    print(f"  → report saved: {out_path}")

    score = BacktestCredibilityRubric().evaluate_with_overfitting(
        report,
        lookahead_clean=True,
        survivorship_inclusive=True,
        pit_fundamentals=True,
        regime_coverage=True,
        out_of_sample_validated=False,
        monte_carlo_drawdown=True,
    )
    print(
        f"  Credibility (overfitting-aware): {score.score}/100  "
        f"[gate ≥ 60: {'PASS' if score.passes_gate else 'FAIL'}]"
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--start", type=date.fromisoformat, default=date(2025, 3, 1))
    p.add_argument("--end", type=date.fromisoformat, default=date(2025, 12, 31))
    p.add_argument("--database-url", default=None)
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    p.add_argument("--results-file", default=DEFAULT_RESULTS_FILE)
    p.add_argument("--rejected-file", default=DEFAULT_REJECTED_FILE)
    p.add_argument("--trades-file", default=DEFAULT_TRADES_FILE)
    p.add_argument("--skip-rejected-csv", action="store_true")
    p.add_argument(
        "--skip-statistical-validation",
        action="store_true",
        help="Skip the Statistical Validation section (saves ~30s of compute).",
    )
    # ─── Parameter-search hooks ─────────────────────────────────────────────
    p.add_argument("--json", dest="json_output", action="store_true",
                   help="Emit a single JSON object with search-pipeline metrics and exit 0.")
    p.add_argument("--trade-log", type=Path, default=None,
                   help="Write standardised per-trade CSV to this path.")
    p.add_argument("--z-threshold", type=float, default=None,
                   help="Override z-score threshold (default 3.0 for the winner variant).")
    p.add_argument("--earnings-quality", choices=("HIGH", "MEDIUM_AND_HIGH"), default=None,
                   help="HIGH → only HIGH-grade earnings; MEDIUM_AND_HIGH → reject only LOW/no-data.")
    p.add_argument("--volume-climax-multiplier", type=float, default=None,
                   help="Min volume-climax ratio for a candidate (default 1.0 = no gate).")
    p.add_argument("--max-hold-days", type=int, default=None,
                   help="Override max holding period in trading days (default 30).")
    p.add_argument("--stop-pct", type=float, default=None,
                   help="Override hard-stop percentage (default 0.08).")
    return p.parse_args(argv)


def main() -> None:  # pragma: no cover - CLI shim
    raise SystemExit(asyncio.run(amain(_parse_args())))


if __name__ == "__main__":  # pragma: no cover
    main()
