"""Chart adapter for the operator dashboard.

Currently backed by ``streamlit-lightweight-charts-pro`` (TradingView Lightweight
Charts wrapper, has first-class `TradeData` markers). If that 0.x-versioned
package breaks on a future Streamlit bump, this module is the single place
to swap in a Plotly equivalent — public API
(:func:`render_ticker_chart`) does not change.

Public API:
    render_ticker_chart(ticker, ohlc, closed_trades, active_entries) -> None
        Render a candlestick chart with entry/exit markers.
"""
from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st


# Lazy import — keeps the dashboard import-able even when the chart lib
# isn't installed (e.g. CI). The first render() call surfaces a clear
# error instead of crashing at module-load time.
def _lib():
    import streamlit_lightweight_charts_pro as slc  # noqa: F401
    return slc


def render_ticker_chart(
    ticker: str,
    ohlc: pd.DataFrame,
    closed_trades: list[dict[str, Any]],
    active_entries: list[dict[str, Any]],
) -> None:
    """Render a candlestick chart for ``ticker`` with trade markers.

    Args:
        ticker: symbol (e.g. ``"AAPL"``).
        ohlc: DataFrame with columns ``date``, ``open``, ``high``, ``low``, ``close``
            (one row per trading day). Must be sorted ascending by ``date``.
        closed_trades: list of dicts with keys ``entry_date``, ``entry_price``,
            ``exit_date``, ``exit_price``, ``pnl_pct``. Rendered as
            ``TradeData`` — entry/exit linked, color-coded by profitability.
        active_entries: list of dicts with keys ``entry_date``, ``entry_price``,
            ``qty``. Currently-held positions with no exit yet. Rendered as
            arrow annotations below the bar.
    """
    if ohlc.empty:
        st.info(f"No bars available for {ticker} in this window.")
        return

    try:
        slc = _lib()
    except ImportError as exc:
        st.error(
            f"Chart library not installed — `pip install streamlit-lightweight-charts-pro`. "
            f"({exc})"
        )
        return

    # Build candlestick data
    candles = []
    for _, row in ohlc.iterrows():
        d = row["date"]
        time_str = d.isoformat() if hasattr(d, "isoformat") else str(d)
        candles.append(
            slc.CandlestickData(
                time=time_str,
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
            )
        )

    series = slc.CandlestickSeries(data=candles)
    chart = slc.Chart(series=series)

    # Closed trades — first-class TradeData primitive. Each entry/exit pair
    # gets linked markers + an optional connecting line.
    if closed_trades:
        trades = []
        for i, t in enumerate(closed_trades):
            entry = t["entry_date"]
            exit_d = t["exit_date"]
            trades.append(
                slc.TradeData(
                    entry_time=entry.isoformat() if hasattr(entry, "isoformat") else str(entry),
                    entry_price=float(t["entry_price"]),
                    exit_time=exit_d.isoformat() if hasattr(exit_d, "isoformat") else str(exit_d),
                    exit_price=float(t["exit_price"]),
                    is_profitable=float(t.get("pnl_pct", 0.0)) > 0,
                    id=f"trade_{ticker}_{i}",
                    additional_data={
                        "pnl_pct": float(t.get("pnl_pct", 0.0)),
                        "exit_reason": str(t.get("exit_reason", "")),
                    },
                )
            )
        try:
            chart.add_trades(trades)
        except Exception as exc:  # noqa: BLE001 — non-fatal; chart still useful without trade markers
            st.warning(f"Could not add trade markers ({exc}); rendering bars only.")

    # Active entries — currently-held positions, no exit yet. Use arrow
    # annotations below the entry bar so they're visually distinct from
    # closed trades' linked markers.
    for i, e in enumerate(active_entries):
        d = e["entry_date"]
        time_str = d.isoformat() if hasattr(d, "isoformat") else str(d)
        try:
            arrow = slc.create_arrow_annotation(
                time=time_str,
                price=float(e["entry_price"]),
                text=f"OPEN {int(e.get('qty', 0))}",
                color="#0a8a3a",
            )
            chart.add_annotation(arrow)
        except Exception:  # noqa: BLE001
            # Fall back: just skip the marker, render bars + closed trades.
            pass

    chart.render(key=f"chart_{ticker}")
