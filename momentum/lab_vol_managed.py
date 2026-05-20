"""Lab-only vol-managed 12-1 + earnings-beat overlay variant of Momentum.

This module implements the single pre-registered Lab candidate described in
``docs/superpowers/specs/2026-05-20-momentum-vol-managed-lab-candidate.md``:
**vol-managed sizing (annualized target σ = 0.40) + recent EARNINGS_BEAT
overlay (≤ 90 calendar days backward)** layered onto Momentum's existing
top-decile 12-1 selection + monthly rebalance.

**Live-safety contract.** This module is imported **exclusively** from
``momentum.backtest.run_momentum_with_context`` when the Lab override
``vol_managed_mode == "vol_managed"`` is set. ``momentum.scheduler`` (the
live trading path) does NOT import ``momentum.backtest`` or this module
— byte-identical by construction (the import-isolation assertion in
``momentum/tests/test_lab_vol_managed_byte_identical.py`` pins this).

Every numeric constant is pinned per the spec; nothing here is
Lab-sampled. The only Lab-sampled value is the ``vol_managed_mode``
choice toggle that gates entry into this module (spec §2.5).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

if TYPE_CHECKING:  # pragma: no cover
    from momentum.backtest import MomentumWindowContext
    from tpcore.backtest.search import BacktestRunResult


# ────────────────────────────────────────────────────────────────────────────
# Pinned constants (spec §1.2, §2.2, §2.3) — NEVER Lab-sampled.
# ────────────────────────────────────────────────────────────────────────────

# Annualized target portfolio vol for the vol-managed sizing rule.
# Pinned at 0.40 per Daniel & Moskowitz (2016) "Momentum Crashes" Table 5
# (12% monthly ≈ 41% annualized) and Barroso & Santa-Clara (2015). The
# canonical academic value for monthly-rebalanced 12-1 momentum.
TARGET_ANNUAL_VOL = 0.40

# Realized-vol estimation window: 60 trading days (≈ 3 calendar months),
# the standard short-window vol estimator for a 12-1 strategy.
VOL_WINDOW_TRADING_DAYS = 60

# Vol-scale clip — bounds per-name leverage. The lower bound prevents a
# malformed sub-window from blowing exposure up unbounded via a very-low
# σ stub; the upper bound caps the upside vol-scale at 2× legacy
# exposure (matches the literature's typical cap).
VOL_SCALE_LOW = 0.5
VOL_SCALE_HIGH = 2.0

# σ degenerate guard: σ ≤ this threshold ⇒ vol-scale = 1.0 (neutral).
VOL_DEGENERATE_FLOOR = 1e-6

# Earnings-beat overlay window: 90 calendar days backward (strictly
# backward, [t − 90, t]). Pinned per the deep-research adjudication
# (TODO.md L463-470).
EARNINGS_LOOKBACK_DAYS = 90

# Trading days per year — standard convention for annualizing daily
# realized vol via sqrt(252).
TRADING_DAYS_PER_YEAR = 252


# ────────────────────────────────────────────────────────────────────────────
# Pure scoring helpers (unit-tested independently of the DB / panels)
# ────────────────────────────────────────────────────────────────────────────


def compute_realized_annual_vol(closes: np.ndarray) -> float:
    """Annualized realized volatility of daily log-returns over the input
    window.

    ``closes`` is a 1-D array of consecutive close prices, length ≥ 2.
    Returns ``sigma_annual = std(daily_log_returns) * sqrt(252)``.

    Degenerate guard: if the input has < 2 prices, or all prices ≤ 0, or
    every log-return is zero (constant series), returns 0.0 (caller's
    ``compute_vol_scale`` then collapses to scale = 1.0 via the
    σ-degenerate floor — spec §2.2).
    """
    if closes.size < 2:
        return 0.0
    if not np.all(closes > 0):
        return 0.0
    log_rets = np.diff(np.log(closes))
    if log_rets.size < 1:
        return 0.0
    std = float(np.std(log_rets, ddof=1)) if log_rets.size >= 2 else 0.0
    return std * math.sqrt(TRADING_DAYS_PER_YEAR)


def compute_vol_scale(sigma_annual: float) -> float:
    """Vol-scale ``s = clip(TARGET_ANNUAL_VOL / σ, VOL_SCALE_LOW,
    VOL_SCALE_HIGH)`` with the σ-degenerate guard.

    A degenerate (σ ≤ 1e-6) input collapses to ``s = 1.0`` — the most
    conservative neutral (no scaling). Otherwise the standard
    vol-managed sizing rule (spec §2.2) applies.
    """
    if sigma_annual <= VOL_DEGENERATE_FLOOR:
        return 1.0
    raw = TARGET_ANNUAL_VOL / sigma_annual
    return float(max(VOL_SCALE_LOW, min(VOL_SCALE_HIGH, raw)))


def has_recent_earnings_beat(
    events: list[tuple[date, float]] | None,
    as_of: date,
) -> bool:
    """True iff at least one ``EARNINGS_BEAT`` event in the strictly
    backward window ``[as_of − EARNINGS_LOOKBACK_DAYS, as_of]`` has
    ``magnitude_pct > 0``.

    ``events`` is a per-ticker list of ``(event_date, magnitude_pct)``
    tuples (as loaded by ``momentum.backtest._load_earnings_beats``).
    Returns False on ``None`` / empty input (the overlay's documented
    semantic: name excluded — spec §2.3, §7).
    """
    if not events:
        return False
    lo = as_of - timedelta(days=EARNINGS_LOOKBACK_DAYS)
    for ev_date, magnitude in events:
        # Strictly backward + positive magnitude (spec §2.3).
        if lo <= ev_date <= as_of and magnitude > 0:
            return True
    return False


# ────────────────────────────────────────────────────────────────────────────
# Vol-managed + earnings-beat-overlay backtest core
# ────────────────────────────────────────────────────────────────────────────


@dataclass
class VolManagedTrade:
    """Single vol-managed trade record — analogous to
    ``momentum.backtest.MomentumTrade`` but with the vol-scale applied.
    """

    ticker: str
    entry_date: date
    entry_price: float
    exit_date: date
    exit_price: float
    raw_pnl_pct: float       # legacy slippage-adjusted PnL (pre-scale)
    vol_scale: float         # s_n = clip(0.40 / σ_n, 0.5, 2.0)
    pnl_pct: float           # = raw_pnl_pct * vol_scale (the scaled return)
    score_at_entry: float
    sigma_annual: float

    @property
    def exit_reason(self) -> str:
        return "scheduled_rebalance"


def _compute_one_rebalance_vol_managed(
    panels: dict[str, pd.DataFrame],
    rebalance_date: date,
    *,
    lookback: int,
    skip: int,
    hold: int,
    top_decile_pct: float,
    tier_round_trip_costs: dict[str, float],
    earnings_by_ticker: dict[str, list[tuple[date, float]]],
) -> list[VolManagedTrade]:
    """One rebalance step under the vol-managed + earnings-beat overlay.

    Pipeline (spec §2.1–§2.4):

    1. Compute legacy 12-1 raw scores on the eligible universe + apply
       the tradeable-common-stock filter (identical to the legacy
       backtest).
    2. Top decile by score (identical).
    3. **Earnings-beat overlay**: exclude any decile name without a
       positive EARNINGS_BEAT in ``[rebalance_date − 90, rebalance_date]``.
    4. Open + close trades exactly like the legacy path (next bar's
       open × 1+slip → ``hold`` bars later close × 1−slip).
    5. **Vol-managed scaling**: compute ``σ_n`` over the 60 bars
       strictly prior to entry; ``s_n = clip(0.40 / σ_n, 0.5, 2.0)`` with
       the σ-degenerate guard. Scale the realized PnL pct by ``s_n``.
    """
    # Lazy import so the legacy path never depends on this module's
    # internals — purely cosmetic; the call already routes only when
    # vol_managed_mode is on, but keeping the import here documents
    # the directionality (lab_vol_managed depends on backtest, not
    # the other way around).
    from decimal import Decimal as _Decimal

    from momentum.models import is_tradeable_common_stock

    scores: dict[str, float] = {}
    for ticker, df in panels.items():
        if rebalance_date not in df.index:
            continue
        idx = df.index.get_loc(rebalance_date)
        if idx < skip + lookback:
            continue
        p_now = float(df.iloc[idx - skip]["close"])
        p_then = float(df.iloc[idx - skip - lookback]["close"])
        if p_then <= 0 or math.isnan(p_now) or math.isnan(p_then):
            continue
        if not is_tradeable_common_stock(ticker, _Decimal(str(p_now))):
            continue
        scores[ticker] = (p_now / p_then) - 1.0

    if not scores:
        return []

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    n_decile = max(1, int(len(ranked) * top_decile_pct))
    top = ranked[:n_decile]

    # Earnings-beat overlay filter (spec §2.3). A name without a
    # qualifying recent positive beat is excluded.
    decile_with_beat = [
        (ticker, score) for ticker, score in top
        if has_recent_earnings_beat(
            earnings_by_ticker.get(ticker), rebalance_date,
        )
    ]

    trades: list[VolManagedTrade] = []
    for ticker, score in decile_with_beat:
        df = panels[ticker]
        entry_idx = df.index.get_loc(rebalance_date) + 1
        if entry_idx >= len(df):
            continue
        exit_idx = min(entry_idx + hold, len(df) - 1)
        if exit_idx <= entry_idx:
            continue
        slip_rt = tier_round_trip_costs.get(ticker)
        slip = slip_rt / 2.0 if slip_rt is not None else 0.0005
        entry_px = float(df.iloc[entry_idx]["open"]) * (1.0 + slip)
        exit_px = float(df.iloc[exit_idx]["close"]) * (1.0 - slip)
        if entry_px <= 0:
            continue
        raw_pnl_pct = (exit_px / entry_px) - 1.0

        # Vol-managed sizing (spec §2.2): σ measured strictly backward
        # over the 60 bars [entry_idx − 60, entry_idx − 1].
        vol_window_lo = max(0, entry_idx - VOL_WINDOW_TRADING_DAYS)
        vol_window_hi = entry_idx  # exclusive — bars strictly BEFORE entry
        closes_window = df.iloc[vol_window_lo:vol_window_hi]["close"].to_numpy(
            dtype=float,
        )
        sigma_annual = compute_realized_annual_vol(closes_window)
        vol_scale = compute_vol_scale(sigma_annual)
        scaled_pnl_pct = raw_pnl_pct * vol_scale

        entry_d = df.index[entry_idx]
        exit_d = df.index[exit_idx]
        if isinstance(entry_d, pd.Timestamp):
            entry_d = entry_d.date()
        if isinstance(exit_d, pd.Timestamp):
            exit_d = exit_d.date()
        trades.append(VolManagedTrade(
            ticker=ticker,
            entry_date=entry_d,
            entry_price=entry_px,
            exit_date=exit_d,
            exit_price=exit_px,
            raw_pnl_pct=raw_pnl_pct,
            vol_scale=vol_scale,
            pnl_pct=scaled_pnl_pct,
            score_at_entry=score,
            sigma_annual=sigma_annual,
        ))
    return trades


def _run_vol_managed_backtest(
    panels: dict[str, pd.DataFrame],
    *,
    start: date,
    end: date,
    lookback: int,
    skip: int,
    hold: int,
    top_decile_pct: float,
    tier_round_trip_costs: dict[str, float],
    earnings_by_ticker: dict[str, list[tuple[date, float]]],
) -> list[VolManagedTrade]:
    """Walk every month-end in ``[start, end]``; produce vol-managed +
    earnings-overlay per-position trades."""
    # Re-use legacy month-end selection (NYSE session boundaries) so
    # the rebalance schedule is byte-identical to legacy when the universe
    # is identical.
    from momentum.backtest import _month_end_dates_within

    all_dates = sorted({d for df in panels.values() for d in df.index})
    rebal_dates = _month_end_dates_within(pd.DatetimeIndex(all_dates), start, end)
    trades: list[VolManagedTrade] = []
    for rd in rebal_dates:
        trades.extend(_compute_one_rebalance_vol_managed(
            panels, rd,
            lookback=lookback, skip=skip, hold=hold,
            top_decile_pct=top_decile_pct,
            tier_round_trip_costs=tier_round_trip_costs,
            earnings_by_ticker=earnings_by_ticker,
        ))
    return trades


def _compute_summary_for_vol_managed(
    trades: list[VolManagedTrade],
) -> tuple[float, float, float, float]:
    """Return (sharpe_annualized, profit_factor, max_drawdown_pct, avg_return).

    Mirrors ``momentum.backtest._compute_summary`` but operates on
    ``VolManagedTrade.pnl_pct`` (already vol-scaled).
    """
    if not trades:
        return 0.0, 0.0, 0.0, 0.0
    returns = np.array([t.pnl_pct for t in trades], dtype=float)
    n = len(returns)
    span_days = (trades[-1].entry_date - trades[0].entry_date).days or 1
    trades_per_year = n / (span_days / 365.25) if span_days else n
    std = float(returns.std(ddof=1)) if n > 1 else 0.0
    avg = float(returns.mean())
    if std > 0 and n > 1:
        sharpe = avg / std * math.sqrt(trades_per_year)
    else:
        sharpe = 0.0
    equity = np.concatenate(([1.0], 1.0 + np.cumsum(returns)))
    peak = np.maximum.accumulate(equity)
    max_dd = float(((equity - peak) / peak).min())
    wins = returns[returns > 0]
    losses = returns[returns < 0]
    gross_w = float(wins.sum()) if len(wins) else 0.0
    gross_l = float(-losses.sum()) if len(losses) else 0.0
    pf = float(gross_w / gross_l) if gross_l > 0 else float("inf")
    return sharpe, pf, max_dd, avg


# ────────────────────────────────────────────────────────────────────────────
# Public entry point — dispatched to from momentum.backtest when
# ``vol_managed_mode == "vol_managed"``.
# ────────────────────────────────────────────────────────────────────────────


def run_vol_managed_with_context(
    context: MomentumWindowContext,
    *,
    lookback: int,
    skip: int,
    hold: int,
    top_decile_pct: float,
    trade_log_path: Path | None = None,
) -> BacktestRunResult:
    """Run the vol-managed 12-1 + earnings-beat-overlay variant against a
    pre-loaded :class:`MomentumWindowContext`.

    The caller (``momentum.backtest.run_momentum_with_context``) has
    already plumbed the module-level overrides; this function reads
    them indirectly via the ``lookback`` / ``skip`` / ``hold`` /
    ``top_decile_pct`` arguments that the caller threaded through.

    Earnings overlay data MUST be present on ``context.earnings_by_
    ticker``. When the field is None (loader was bypassed — e.g. a
    hand-built fixture without earnings rows), the overlay's exclude-no-
    confirmation rule fires for every name and the trade set is empty
    (a deterministic, honest no-op).
    """
    # Local imports defer the tpcore/search dep + the dataclass cycle.
    from tpcore.backtest.search import (
        BacktestRunResult,
        SearchTrade,
        compute_search_metrics,
        write_trade_log_csv,
    )

    if not context.panels:
        return BacktestRunResult(
            engine="momentum",
            parameters={
                "lookback_days": int(lookback),
                "skip_days": int(skip),
                "hold_days": int(hold),
                "top_decile_pct": float(top_decile_pct),
                "vol_managed_mode": "vol_managed",
            },
            credibility_score=0, passed_gate=False,
            sharpe=0.0, profit_factor=0.0, max_drawdown=0.0, trades=0, dsr=0.0,
            min_btl_gap=0, trades_per_param=0.0, sensitivity_score=None,
            ruin_probability=0.0, trade_log=[],
        )

    earnings = context.earnings_by_ticker or {}
    trades = _run_vol_managed_backtest(
        context.panels,
        start=context.start, end=context.end,
        lookback=lookback, skip=skip, hold=hold,
        top_decile_pct=top_decile_pct,
        tier_round_trip_costs=context.tier_round_trip_costs,
        earnings_by_ticker=earnings,
    )

    search_trades = [
        SearchTrade(
            ticker=t.ticker, entry_date=t.entry_date,
            entry_price=t.entry_price, exit_date=t.exit_date,
            exit_price=t.exit_price, pnl_pct=t.pnl_pct,
            direction="LONG", exit_reason=t.exit_reason,
        ) for t in trades
    ]
    if trade_log_path is not None:
        write_trade_log_csv(trade_log_path, search_trades)

    sharpe, profit_factor, max_dd, _ = _compute_summary_for_vol_managed(trades)
    trades_for_diag = [
        {
            "pnl_pct": float(t.pnl_pct),
            "entry_date": t.entry_date,
            "exit_date": t.exit_date,
            "direction": "LONG",
            "ticker": t.ticker,
            "entry_price": float(t.entry_price),
        } for t in trades
    ]
    # Mirror the legacy price_data shape — every selected ticker's
    # bars feed the credibility rubric's price-coverage check.
    frames: list[pd.DataFrame] = []
    for ticker, df in context.panels.items():
        sub = df[["open", "high", "low", "close"]].reset_index().rename(
            columns={"index": "date"},
        )
        sub["ticker"] = ticker
        frames.append(sub)
    price_data = (
        pd.concat(frames, ignore_index=True) if frames
        else pd.DataFrame(columns=["ticker", "date", "open", "high", "low", "close"])
    )

    parameters = {
        "lookback_days": int(lookback),
        "skip_days": int(skip),
        "hold_days": int(hold),
        "top_decile_pct": float(top_decile_pct),
        "vol_managed_mode": "vol_managed",
    }
    return compute_search_metrics(
        engine="momentum",
        parameters=parameters,
        trades_for_diag=trades_for_diag,
        sharpe=sharpe,
        profit_factor=profit_factor,
        max_drawdown=max_dd,
        n_trials=len(parameters),
        price_data=price_data,
        rubric_inputs={
            "lookahead_clean": True,
            "survivorship_inclusive": False,
            "pit_fundamentals": True,  # earnings-beat overlay is PIT
            "regime_coverage": True,
            "monte_carlo_drawdown": True,
        },
        search_trades=search_trades,
    )


__all__ = [
    "EARNINGS_LOOKBACK_DAYS",
    "TARGET_ANNUAL_VOL",
    "TRADING_DAYS_PER_YEAR",
    "VOL_DEGENERATE_FLOOR",
    "VOL_SCALE_HIGH",
    "VOL_SCALE_LOW",
    "VOL_WINDOW_TRADING_DAYS",
    "VolManagedTrade",
    "compute_realized_annual_vol",
    "compute_vol_scale",
    "has_recent_earnings_beat",
    "run_vol_managed_with_context",
]
