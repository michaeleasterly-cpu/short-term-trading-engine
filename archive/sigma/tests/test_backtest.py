"""Unit tests for the trade simulation in ``sigma.backtest``.

The full script needs ``platform.prices_daily`` populated to run end-to-end;
these tests exercise the simulator + metrics on synthetic in-memory bars
so we have a correctness signal independent of DB state.
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from sigma.backtest import (
    HARD_STOP_PCT,
    SLIPPAGE_PER_SIDE,
    TradeRecord,
    compute_summary,
    simulate_trade,
)


def _bars(rows: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    """Build a date-indexed bar frame from ``[(open, high, low, close), ...]``."""
    start = date(2026, 1, 5)
    idx = [start + timedelta(days=i) for i in range(len(rows))]
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)
    df["volume"] = 1_000_000
    return df


# ────────────────────────────────────────────────────────────────────────────
# simulate_trade — three exit paths
# ────────────────────────────────────────────────────────────────────────────


def test_simulate_trade_full_ride_to_upper_band() -> None:
    """High enough on day 1 to fill tier 1, day 2 to fill tier 2."""
    df = _bars(
        [
            (100.0, 100.0, 100.0, 100.0),  # entry-window prior bar
            (100.0, 102.5, 100.0, 102.5),  # day 1: hits mid-band 102 → tier 1 fills
            (102.5, 105.5, 102.5, 105.5),  # day 2: hits upper 105 → tier 2 fills
        ]
    )
    rec = simulate_trade(
        df,
        entry_idx=0,
        entry_price=100.0,
        mid_band=102.0,
        upper_band=105.0,
        variant="x",
        ticker="X",
        entry_date=df.index[0],
    )
    assert rec.tier1_exit_date == df.index[1]
    assert rec.tier2_exit_date == df.index[2]
    assert not rec.stopped_out
    # Tier 1 sells 0.5 at 102 * (1 - slippage); tier 2 sells 0.5 at 105 * (1 - slippage).
    expected_pnl = 0.5 * (102.0 * (1 - SLIPPAGE_PER_SIDE) - 100.0) + 0.5 * (
        105.0 * (1 - SLIPPAGE_PER_SIDE) - 100.0
    )
    assert rec.pnl == pytest.approx(expected_pnl)
    assert rec.return_pct > 0


def test_simulate_trade_immediate_stop_out() -> None:
    """Day-1 low pierces the −3% stop → full position dumped at stop."""
    entry = 100.0
    stop_level = entry * (1 - HARD_STOP_PCT)
    df = _bars(
        [
            (entry, entry, entry, entry),
            (entry, entry, stop_level - 0.5, stop_level),  # day 1: low < stop
        ]
    )
    rec = simulate_trade(
        df,
        entry_idx=0,
        entry_price=entry,
        mid_band=102.0,
        upper_band=105.0,
        variant="x",
        ticker="X",
        entry_date=df.index[0],
    )
    assert rec.stopped_out is True
    assert rec.tier1_exit_date is None
    assert rec.holding_days == 1
    # Both tiers exit at stop * (1 - slippage); pnl = (sell - entry) on full notional.
    sell = stop_level * (1 - SLIPPAGE_PER_SIDE)
    assert rec.pnl == pytest.approx(sell - entry)
    assert rec.return_pct < 0


def test_simulate_trade_time_out_closes_at_last_close() -> None:
    """Neither stop nor either band is touched within MAX_HOLD_DAYS."""
    rows = [(100.0, 100.5, 99.5, 100.0)] * 35  # tight oscillation, never hits 102 or 95
    df = _bars(rows)
    rec = simulate_trade(
        df,
        entry_idx=0,
        entry_price=100.0,
        mid_band=102.0,
        upper_band=105.0,
        variant="x",
        ticker="X",
        entry_date=df.index[0],
    )
    assert rec.tier1_exit_date is None  # tier 1 never fills
    assert rec.tier2_exit_date is not None  # forced close on time-out
    assert rec.holding_days >= 1
    # Time-out closes at last close × (1-slippage); for our flat 100s that's ~100*(1-slip).
    assert rec.return_pct == pytest.approx(-SLIPPAGE_PER_SIDE, abs=1e-6)


# ────────────────────────────────────────────────────────────────────────────
# compute_summary — basic metrics
# ────────────────────────────────────────────────────────────────────────────


def test_compute_summary_handles_empty_trade_list() -> None:
    s = compute_summary("baseline", [])
    assert s.n_trades == 0
    assert s.win_rate == 0.0
    assert s.sharpe_annualized == 0.0


def test_compute_summary_basic_metrics() -> None:
    """Three trades: two winners (+5%, +3%), one loser (−2%)."""
    trades = [
        TradeRecord(
            variant="b", ticker="A", entry_date=date(2024, 1, 5),
            entry_price=100.0, return_pct=0.05, pnl=5.0, notional=100.0,
        ),
        TradeRecord(
            variant="b", ticker="A", entry_date=date(2024, 6, 10),
            entry_price=100.0, return_pct=-0.02, pnl=-2.0, notional=100.0,
        ),
        TradeRecord(
            variant="b", ticker="A", entry_date=date(2024, 12, 20),
            entry_price=100.0, return_pct=0.03, pnl=3.0, notional=100.0,
        ),
    ]
    s = compute_summary("baseline", trades)
    assert s.n_trades == 3
    assert s.win_rate == pytest.approx(2 / 3)
    assert s.avg_return_pct == pytest.approx((0.05 - 0.02 + 0.03) / 3)
    # Profit factor = (0.05+0.03) / 0.02 = 4.0
    assert s.profit_factor == pytest.approx(4.0)
    assert 2024 in s.by_year
    assert s.by_year[2024]["n_trades"] == 3
