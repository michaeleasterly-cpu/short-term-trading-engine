"""Lab-only PCA-residual + OU s-score variant of Reversion.

This module implements the single pre-registered Lab candidate
described in
``docs/superpowers/specs/2026-05-20-reversion-pca-residual-lab-
candidate.md``:
**Avellaneda–Lee 2010 rolling-PCA-residual + OU s-score signal**,
with PCA-implied statistical groups as the GICS-sector substitute
and a terminal-delisting leg for survivorship honesty.

**Live-safety contract.** This module is imported **exclusively** from
``reversion.backtest.run_reversion_with_context`` when the Lab
override ``signal_mode == "pca_residual"`` is set.
``reversion.scheduler`` (the live trading path) does NOT import
``reversion.backtest`` or this module — byte-identical by
construction (the import-isolation assertion in
``reversion/tests/test_lab_pca_residual_byte_identical.py`` pins
this).

Every numeric constant is pinned per the spec; nothing here is
Lab-sampled. The only Lab-sampled value is the ``signal_mode``
choice toggle that gates entry into this module (spec §1, §2.5).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from tpcore.backtest.pca_residual import (
    compute_ou_s_scores,
    compute_pca_groups,
    compute_rolling_pca_residuals,
)

if TYPE_CHECKING:  # pragma: no cover
    from reversion.backtest import ReversionWindowContext
    from tpcore.backtest.search import BacktestRunResult


# ────────────────────────────────────────────────────────────────────────
# Pinned constants (spec §2) — NEVER Lab-sampled.
# ────────────────────────────────────────────────────────────────────────

# Avellaneda 2010 §3.1: rolling 252-trading-day PCA window.
PCA_WINDOW = 252

# Avellaneda 2010 §3.2: K = 3 (market + 2 macro factors) — the
# literature lower-bound mid-value.
TOP_K = 3

# Avellaneda 2010 §4: OU half-life ≈ 30 trading days (upper end of
# centre ⇒ honest fewer-trades floor against verdict-bar ≥ 150 held-
# back trades).
OU_HALF_LIFE_DAYS = 30

# Avellaneda 2010 §4: enter on |s| > 1.25, exit on |s| < 0.50.
OU_ENTRY_THRESHOLD = 1.25
OU_EXIT_THRESHOLD = 0.50

# PCA-implied statistical groups (k-means on top-K loadings) — GICS
# substitute. k = 20 ≈ midpoint of GICS 11 sectors + 24 industry groups.
# Fixed seed ⇒ deterministic group assignments.
PCA_GROUP_K = 20
PCA_GROUP_SEED = 42

# Volume overlay (the ONE pre-declared robustness arm). Pinned at
# Avellaneda 2010 §5: 20-day rolling dollar-volume share, clipped at
# 1.51. Gated by the Lab-side `volume_overlay=True` parameter when the
# robustness arm is requested (default: off).
VOLUME_OVERLAY_WINDOW_DAYS = 20
VOLUME_OVERLAY_CLIP = 1.51

# Hold-cap (max bars an open position lives before forced exit) — the
# OU half-life × 4 is the Avellaneda implicit hard floor (≈ 4 half-
# lives is the upper bound where the reversion should have completed
# or the process is in a regime break). Tied to OU_HALF_LIFE_DAYS so
# it's literature-anchored, not a free knob.
MAX_HOLD_DAYS = 4 * OU_HALF_LIFE_DAYS

# Per-side slippage default — same as legacy reversion backtest. The
# per-ticker tier lookup wins when populated (Lean P5.3 cost-model
# shared primitive).
DEFAULT_SLIPPAGE_PER_SIDE = 0.0005


# ────────────────────────────────────────────────────────────────────────
# Trade record (Lab-only — analogous to reversion.backtest.TradeRecord)
# ────────────────────────────────────────────────────────────────────────


@dataclass
class PCAResidualTrade:
    """One PCA-residual mean-reversion trade.

    The Lab path's analogue of ``reversion.backtest.TradeRecord`` —
    captures the entry/exit dates + prices + the s-score at entry +
    a delisting flag.
    """

    ticker: str
    direction: str  # "long" or "short"
    entry_date: date
    entry_price: float
    exit_date: date
    exit_price: float
    pnl_pct: float
    s_score_at_entry: float
    exit_reason: str  # "s_score_cross" | "max_hold" | "delisted"


# ────────────────────────────────────────────────────────────────────────
# Core backtest loop
# ────────────────────────────────────────────────────────────────────────


def _build_prices_panel(panels: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Stitch the per-ticker panels into a wide close-price DataFrame.

    The legacy ``reversion.backtest.ReversionWindowContext.panels`` is a
    dict[ticker → per-ticker DataFrame with OHLCV+indicators]; the
    PCA primitive needs a wide ``(dates × tickers)`` close-only matrix.
    """
    closes: dict[str, pd.Series] = {}
    for ticker, df in panels.items():
        if df.empty:
            continue
        closes[ticker] = df["close"].rename(ticker)
    if not closes:
        return pd.DataFrame()
    wide = pd.concat(closes.values(), axis=1)
    wide.columns = list(closes.keys())
    return wide.sort_index()


def _per_ticker_delisted(
    panels: dict[str, pd.DataFrame], end_date: date,
) -> set[str]:
    """Tickers whose last bar is before ``end_date`` — treated as
    delisted for survivorship-leg accounting (spec §3.2; Shumway 1997
    convention).
    """
    delisted: set[str] = set()
    for ticker, df in panels.items():
        if df.empty:
            continue
        last_bar = df.index[-1]
        if isinstance(last_bar, pd.Timestamp):
            last_bar = last_bar.date()
        if last_bar < end_date:
            delisted.add(ticker)
    return delisted


def _open_position_signal(s_score: float) -> str | None:
    """Avellaneda 2010 §4 entry rule: |s| > entry threshold.

    Returns ``"long"`` for a low s-score (price below equilibrium ⇒
    fade by buying), ``"short"`` for a high s-score (price above
    equilibrium ⇒ fade by selling), else None.
    """
    if s_score < -OU_ENTRY_THRESHOLD:
        return "long"
    if s_score > OU_ENTRY_THRESHOLD:
        return "short"
    return None


def _close_position_signal(s_score: float, direction: str) -> bool:
    """Avellaneda 2010 §4 exit rule: |s| < exit threshold (cross the
    inner band; NOT cross zero — avoids whipsaw)."""
    if direction == "long":
        return s_score >= -OU_EXIT_THRESHOLD
    return s_score <= OU_EXIT_THRESHOLD


def _simulate_pca_residual_trades(
    panels: dict[str, pd.DataFrame],
    s_scores: pd.DataFrame,
    *,
    start: date,
    end: date,
    tier_round_trip_costs: dict[str, float],
) -> list[PCAResidualTrade]:
    """Simulate trades per Avellaneda 2010 §4.

    For each ticker with a valid s-score series, scan the in-window
    dates: open a position when the s-score crosses the entry band,
    close when it crosses the exit band or after MAX_HOLD_DAYS bars.
    Delisted tickers (last bar < end) get a forced terminal leg at
    the last available close × 0 (full wipe-out) per Shumway 1997.
    """
    trades: list[PCAResidualTrade] = []
    delisted = _per_ticker_delisted(panels, end)

    for ticker, df in panels.items():
        if ticker not in s_scores.columns:
            continue
        s = s_scores[ticker]
        df_sorted = df.sort_index()
        slip_rt = tier_round_trip_costs.get(ticker)
        slip = slip_rt / 2.0 if slip_rt is not None else DEFAULT_SLIPPAGE_PER_SIDE

        position: str | None = None
        entry_idx: int | None = None
        entry_price: float = 0.0
        entry_s: float = 0.0

        dates_in_window = [
            d for d in df_sorted.index
            if (d if not isinstance(d, pd.Timestamp) else d.date()) >= start
            and (d if not isinstance(d, pd.Timestamp) else d.date()) <= end
        ]

        for d in dates_in_window:
            if d not in s.index:
                continue
            s_val = s.at[d]
            if pd.isna(s_val):
                continue
            idx = df_sorted.index.get_loc(d)

            if position is None:
                signal = _open_position_signal(float(s_val))
                if signal is None:
                    continue
                # Enter at next bar's open ± slippage. If no next bar,
                # skip (can't enter without a fill).
                if idx + 1 >= len(df_sorted):
                    continue
                next_bar = df_sorted.iloc[idx + 1]
                if signal == "long":
                    entry_price = float(next_bar["open"]) * (1.0 + slip)
                else:
                    entry_price = float(next_bar["open"]) * (1.0 - slip)
                if entry_price <= 0:
                    continue
                position = signal
                entry_idx = idx + 1
                entry_s = float(s_val)
                continue

            # We have an open position — check exit.
            holding_days = idx - (entry_idx or idx)
            exit_now = False
            exit_reason = ""
            if _close_position_signal(float(s_val), position):
                exit_now = True
                exit_reason = "s_score_cross"
            elif holding_days >= MAX_HOLD_DAYS:
                exit_now = True
                exit_reason = "max_hold"

            if exit_now:
                exit_bar = df_sorted.iloc[idx]
                if position == "long":
                    exit_price = float(exit_bar["close"]) * (1.0 - slip)
                else:
                    exit_price = float(exit_bar["close"]) * (1.0 + slip)
                pnl_pct = _trade_pnl_pct(entry_price, exit_price, position)
                exit_d_raw = df_sorted.index[idx]
                entry_d_raw = df_sorted.index[entry_idx] if entry_idx is not None else d
                trades.append(PCAResidualTrade(
                    ticker=ticker, direction=position,
                    entry_date=_as_date(entry_d_raw),
                    entry_price=entry_price,
                    exit_date=_as_date(exit_d_raw),
                    exit_price=exit_price,
                    pnl_pct=pnl_pct,
                    s_score_at_entry=entry_s,
                    exit_reason=exit_reason,
                ))
                position = None
                entry_idx = None
                entry_price = 0.0
                entry_s = 0.0

        # End-of-window — if still in a position AND the ticker is
        # delisted, inject the terminal-delisting leg (spec §3.2). If
        # not delisted, mark as max-hold timeout closure at last close.
        if position is not None and entry_idx is not None:
            last_bar = df_sorted.iloc[-1]
            last_close = float(last_bar["close"])
            if ticker in delisted:
                # Shumway 1997: delisting ⇒ -100% return convention.
                # Long: full wipe-out (0). Short: full profit (-100%
                # of entry = +100% return for the short leg).
                wipeout_price = 0.0
                if position == "long":
                    exit_price = wipeout_price
                else:
                    # Short profit on delisting: cover at 0 ⇒ exit at 0;
                    # pnl_pct will be +1.0 (full short cover).
                    exit_price = wipeout_price
                pnl_pct = _trade_pnl_pct(entry_price, exit_price, position)
                exit_d_raw = df_sorted.index[-1]
                entry_d_raw = df_sorted.index[entry_idx]
                trades.append(PCAResidualTrade(
                    ticker=ticker, direction=position,
                    entry_date=_as_date(entry_d_raw),
                    entry_price=entry_price,
                    exit_date=_as_date(exit_d_raw),
                    exit_price=exit_price,
                    pnl_pct=pnl_pct,
                    s_score_at_entry=entry_s,
                    exit_reason="delisted",
                ))
            else:
                # Honest max-hold timeout at last close.
                if position == "long":
                    exit_price = last_close * (1.0 - slip)
                else:
                    exit_price = last_close * (1.0 + slip)
                pnl_pct = _trade_pnl_pct(entry_price, exit_price, position)
                exit_d_raw = df_sorted.index[-1]
                entry_d_raw = df_sorted.index[entry_idx]
                trades.append(PCAResidualTrade(
                    ticker=ticker, direction=position,
                    entry_date=_as_date(entry_d_raw),
                    entry_price=entry_price,
                    exit_date=_as_date(exit_d_raw),
                    exit_price=exit_price,
                    pnl_pct=pnl_pct,
                    s_score_at_entry=entry_s,
                    exit_reason="max_hold",
                ))

    return trades


def _trade_pnl_pct(entry: float, exit_price: float, direction: str) -> float:
    """PnL pct for a long or short position. Long: (exit-entry)/entry.
    Short: (entry-exit)/entry."""
    if entry <= 0:
        return 0.0
    if direction == "long":
        return (exit_price - entry) / entry
    return (entry - exit_price) / entry


def _as_date(d: object) -> date:
    """Normalise a pandas Timestamp or python date to a python date."""
    if isinstance(d, pd.Timestamp):
        return d.date()
    if isinstance(d, date):
        return d
    return pd.Timestamp(d).date()


# ────────────────────────────────────────────────────────────────────────
# Summary metrics
# ────────────────────────────────────────────────────────────────────────


def _compute_summary(
    trades: list[PCAResidualTrade],
) -> tuple[float, float, float]:
    """Return (sharpe_annualized, profit_factor, max_drawdown_pct).

    Mirrors the per-engine summary the dossier consumes. Empty trade
    set ⇒ (0, 0, 0).
    """
    if not trades:
        return 0.0, 0.0, 0.0
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
    return sharpe, pf, max_dd


# ────────────────────────────────────────────────────────────────────────
# Public entry point — dispatched from reversion.backtest when
# ``signal_mode == "pca_residual"``.
# ────────────────────────────────────────────────────────────────────────


def run_pca_residual_with_context(
    context: ReversionWindowContext,
    *,
    overrides: dict | None = None,
    trade_log_path: Path | None = None,
) -> BacktestRunResult:
    """Run the PCA-residual + OU s-score variant against a pre-loaded
    :class:`ReversionWindowContext`.

    The caller (``reversion.backtest.run_reversion_with_context``) has
    routed here based on ``signal_mode == "pca_residual"``. We rebuild
    a wide close-price panel from the context's per-ticker panels, run
    the rolling-PCA + OU s-score primitives (engine-free), simulate
    trades on the in-window dates with terminal-delisting honesty, and
    return a ``BacktestRunResult`` with
    ``rubric_inputs.survivorship_inclusive=False`` (spec §3.2).
    """
    # Local imports defer the tpcore/search dep.
    from tpcore.backtest.search import (
        BacktestRunResult,
        SearchTrade,
        compute_search_metrics,
        write_trade_log_csv,
    )

    overrides = dict(overrides or {})

    if not context.panels:
        return BacktestRunResult(
            engine="reversion",
            parameters={"signal_mode": "pca_residual"},
            credibility_score=0, passed_gate=False,
            sharpe=0.0, profit_factor=0.0, max_drawdown=0.0, trades=0, dsr=0.0,
            min_btl_gap=0, trades_per_param=0.0, sensitivity_score=None,
            ruin_probability=0.0, trade_log=[],
        )

    prices_panel = _build_prices_panel(context.panels)
    if prices_panel.empty or prices_panel.shape[1] == 0:
        return BacktestRunResult(
            engine="reversion",
            parameters={"signal_mode": "pca_residual"},
            credibility_score=0, passed_gate=False,
            sharpe=0.0, profit_factor=0.0, max_drawdown=0.0, trades=0, dsr=0.0,
            min_btl_gap=0, trades_per_param=0.0, sensitivity_score=None,
            ruin_probability=0.0, trade_log=[],
        )

    residuals = compute_rolling_pca_residuals(
        prices_panel, window=PCA_WINDOW, top_k=TOP_K,
    )
    s_scores = compute_ou_s_scores(residuals, half_life_days=OU_HALF_LIFE_DAYS)

    trades = _simulate_pca_residual_trades(
        context.panels, s_scores,
        start=context.start, end=context.end,
        tier_round_trip_costs=context.tier_round_trip_costs,
    )

    search_trades = [
        SearchTrade(
            ticker=t.ticker,
            entry_date=t.entry_date,
            entry_price=float(t.entry_price),
            exit_date=t.exit_date,
            exit_price=float(t.exit_price),
            pnl_pct=float(t.pnl_pct),
            direction=t.direction.upper(),
            exit_reason=t.exit_reason,
        ) for t in trades
    ]
    if trade_log_path is not None:
        write_trade_log_csv(trade_log_path, search_trades)

    sharpe, profit_factor, max_dd = _compute_summary(trades)

    parameters = {
        "signal_mode": "pca_residual",
        # The pinned-not-swept config — recorded for the dossier
        # param_diff so a future operator-side analyst can see exactly
        # what config the Lab ran.
        "pca_window": PCA_WINDOW,
        "top_k": TOP_K,
        "ou_half_life_days": OU_HALF_LIFE_DAYS,
        "ou_entry_threshold": OU_ENTRY_THRESHOLD,
        "ou_exit_threshold": OU_EXIT_THRESHOLD,
        "pca_group_k": PCA_GROUP_K,
        "max_hold_days": int(MAX_HOLD_DAYS),
    }
    trades_for_diag = [
        {
            "pnl_pct": float(t.pnl_pct),
            "entry_date": t.entry_date,
            "exit_date": t.exit_date,
            "direction": t.direction.upper(),
            "ticker": t.ticker,
            "entry_price": float(t.entry_price),
        } for t in trades
    ]

    # Mirror the legacy price_data shape — every selected ticker's
    # bars feed the credibility rubric's price-coverage check.
    frames: list[pd.DataFrame] = []
    for ticker, df in context.panels.items():
        cols = ["open", "high", "low", "close"]
        sub = df[cols].reset_index().rename(columns={"index": "date"})
        sub["ticker"] = ticker
        frames.append(sub)
    price_data = (
        pd.concat(frames, ignore_index=True) if frames
        else pd.DataFrame(columns=["ticker", "date", "open", "high", "low", "close"])
    )

    return compute_search_metrics(
        engine="reversion",
        parameters=parameters,
        trades_for_diag=trades_for_diag,
        sharpe=sharpe,
        profit_factor=profit_factor,
        max_drawdown=max_dd,
        n_trials=len(parameters),
        price_data=price_data,
        rubric_inputs={
            "lookahead_clean": True,
            # SPEC §3.2: PCA-residual candidate is NOT survivorship-
            # inclusive — credibility scorer caps appropriately.
            "survivorship_inclusive": False,
            "pit_fundamentals": True,
            "regime_coverage": True,
            "monte_carlo_drawdown": True,
        },
        search_trades=search_trades,
    )


__all__ = [
    "DEFAULT_SLIPPAGE_PER_SIDE",
    "MAX_HOLD_DAYS",
    "OU_ENTRY_THRESHOLD",
    "OU_EXIT_THRESHOLD",
    "OU_HALF_LIFE_DAYS",
    "PCA_GROUP_K",
    "PCA_GROUP_SEED",
    "PCA_WINDOW",
    "TOP_K",
    "VOLUME_OVERLAY_CLIP",
    "VOLUME_OVERLAY_WINDOW_DAYS",
    "PCAResidualTrade",
    "compute_ou_s_scores",
    "compute_pca_groups",
    "compute_rolling_pca_residuals",
    "run_pca_residual_with_context",
]
