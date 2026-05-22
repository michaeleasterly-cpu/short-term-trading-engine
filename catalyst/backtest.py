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
Three pre-registered Lab toggles (each independent, each its own
single-spec candidate):

1. ``cluster_window_days`` (SP-F, PR #159) — ``choice:30,45``. The
   default 30 mirrors ``CATALYST_CLUSTER_WINDOW_DAYS`` in
   :mod:`catalyst.models`; 45 is the alternative-window variant.
   Seam: ``_CLUSTER_WINDOW_OVERRIDE``. Test:
   ``catalyst/tests/test_lab_cluster_window_byte_identical.py``.
2. ``event_confirmation_mode`` (event-confirmed insider-cluster drift,
   spec ``docs/superpowers/specs/2026-05-20-catalyst-insider-cluster-
   event-lab-candidate.md``) — ``choice:off,positive_beat_30d,
   beat_30d_only``. The default ``"off"`` is the legacy cluster-only
   fire-rule; ``"positive_beat_30d"`` adds the strictly-backward 30d
   positive earnings-beat confirmation predicate ON TOP of the
   cluster requirement; ``"beat_30d_only"`` is the pure-PEAD branch
   that bypasses the insider cluster entirely and fires on each
   positive BEAT event (no cluster floor, no aggregate-$ floor). The
   beat_30d_only arm was added 2026-05-22 to express the autonomous
   finder's PEAD hypothesis (3-probe scorecard, candidate
   ``catalyst_pead_expansion_range``). Seam:
   ``_EVENT_CONFIRMATION_MODE_OVERRIDE``. Test:
   ``catalyst/tests/test_lab_event_confirmation_byte_identical.py``.
3. ``hold_days`` (post-2026-05-22 surface enrichment) — ``int 5..30``.
   The hard time-stop horizon when neither TP nor SL fires. Default
   20 sessions (matches the PEAD hypothesis's "20-session hold").
   Seam: ``_HOLD_DAYS_OVERRIDE``.

All overrides are module-level globals reset per call inside
:func:`run_catalyst_with_context`. The LIVE trading path
(``catalyst/scheduler.py``) never imports this backtest module and so
is byte-identical when every flag is at its default (proven by the
characterization tests above + the hold_days byte-identical test).

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
from dataclasses import dataclass, field
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
# When the Lab override (``_HOLD_DAYS_OVERRIDE``) is None we fall back
# to ``HOLDING_PERIOD_DAYS`` (30) so byte-identicality vs the
# pre-enrichment behaviour is preserved for legacy callers. The
# ``catalyst_pead_expansion_range`` finder hypothesis recommends 20
# sessions; that value is supplied at probe time via the
# ``--param-overrides '{"hold_days": 20}'`` flag, NOT hard-coded here.


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


# ════════════════════════════════════════════════════════════════════════
# Event-confirmed insider-cluster drift — second Lab toggle (single-spec
# Lab candidate; spec
# docs/superpowers/specs/2026-05-20-catalyst-insider-cluster-event-lab-
# candidate.md).
#
# When the override is "positive_beat_30d", a cluster fires ONLY IF the
# same ticker has a positive earnings beat
# (``earnings_events.event_type='EARNINGS_BEAT' AND magnitude_pct > 0``)
# in the strictly-backward 30-calendar-day window ``[cursor - 30,
# cursor]``. When the override is None or "off" (the default + legacy),
# the legacy cluster-only fire-rule is used — byte-identical to today.
# The live trading path never enters this module; the override is a
# backtest-only global, so the live constants
# (``catalyst.models.CATALYST_*``) are byte-identical when the flag is
# off (proven by ``catalyst/tests/
# test_lab_event_confirmation_byte_identical.py``).
# ════════════════════════════════════════════════════════════════════════

_EVENT_CONFIRMATION_MODE_OVERRIDE: str | None = None
_EVENT_CONFIRMATION_WINDOW_DAYS: int = 30  # pinned; not Lab-sampled.
_EVENT_CONFIRMATION_OFF = "off"
_EVENT_CONFIRMATION_POSITIVE_BEAT_30D = "positive_beat_30d"
# Pure-PEAD: fire on each strictly-positive earnings BEAT event, no
# insider-cluster requirement (cluster floor + aggregate-$ floor BOTH
# skipped). Added 2026-05-22 (engine surface enrichment) to let the
# autonomous finder express the catalyst_pead_expansion_range
# hypothesis — the prior off/positive_beat_30d arms both REQUIRED the
# insider cluster, stripping pure-PEAD candidates to n_trades=2.
_EVENT_CONFIRMATION_BEAT_30D_ONLY = "beat_30d_only"


def _event_confirmation_mode() -> str:
    """The active event-confirmation mode for THIS backtest run.

    Returns the legacy ``"off"`` unless the off-by-default Lab override
    is set to one of the three declared arms (``"positive_beat_30d"``,
    ``"beat_30d_only"``). Pure. An explicit ``"off"`` override is
    accepted as a synonym for ``None`` (so the
    ``choice:off,positive_beat_30d,beat_30d_only`` toggle has a real
    legacy-default value to flip to in the Lab sampler).
    """
    if _EVENT_CONFIRMATION_MODE_OVERRIDE == _EVENT_CONFIRMATION_POSITIVE_BEAT_30D:
        return _EVENT_CONFIRMATION_POSITIVE_BEAT_30D
    if _EVENT_CONFIRMATION_MODE_OVERRIDE == _EVENT_CONFIRMATION_BEAT_30D_ONLY:
        return _EVENT_CONFIRMATION_BEAT_30D_ONLY
    return _EVENT_CONFIRMATION_OFF


# ════════════════════════════════════════════════════════════════════════
# Hold-days knob — third Lab toggle (post-2026-05-22 enrichment).
#
# The hard time-stop horizon when neither TP nor SL fires. Lab-sampled
# int in [5, 30]; off-by-default override is reset per call inside
# ``run_catalyst_with_context``. None ⇒ the legacy
# ``HOLDING_PERIOD_DAYS`` constant (30) — byte-identical to the
# pre-enrichment behaviour.
# ════════════════════════════════════════════════════════════════════════

_HOLD_DAYS_OVERRIDE: int | None = None


def _hold_days() -> int:
    """The active hold-days horizon for THIS backtest run.

    Returns the legacy ``HOLDING_PERIOD_DAYS`` unless the off-by-default
    Lab override is set. Pure.
    """
    return (
        _HOLD_DAYS_OVERRIDE
        if _HOLD_DAYS_OVERRIDE is not None
        else HOLDING_PERIOD_DAYS
    )


def _has_positive_beat(
    earnings_events: pd.DataFrame,
    *,
    ticker: str,
    cursor: date_t,
    window_days: int = _EVENT_CONFIRMATION_WINDOW_DAYS,
) -> bool:
    """Pure: does ``ticker`` have a positive earnings beat in the
    strictly-backward ``[cursor - window_days, cursor]`` window?

    The predicate is strictly backward — no row dated after ``cursor``
    enters the result (lookahead-honest, spec §9). Only rows with
    ``event_type='EARNINGS_BEAT' AND magnitude_pct > 0`` count.

    Args:
        earnings_events: dataframe with columns
            ``{ticker, event_date, event_type, magnitude_pct}`` (the
            schema of ``platform.earnings_events``). Empty / None
            inputs return ``False`` — a degenerate-but-honest empty
            window predicate (no blow-up).
        ticker: the ticker to test.
        cursor: the right edge of the window (inclusive).
        window_days: calendar days back from ``cursor``. Defaults to
            ``_EVENT_CONFIRMATION_WINDOW_DAYS`` (30, pinned).

    Returns: ``True`` iff at least one matching row exists.
    """
    if earnings_events is None or earnings_events.empty:
        return False
    start = cursor - timedelta(days=window_days)
    df = earnings_events
    mask = (
        (df["ticker"] == ticker)
        & (df["event_type"] == "EARNINGS_BEAT")
        & (df["magnitude_pct"] > 0)
        & (df["event_date"] >= start)
        & (df["event_date"] <= cursor)
    )
    return bool(mask.any())


def default_params() -> dict[str, Any]:
    """Current live defaults for the Lab-sampled keys (the SP3 O1
    dossier-param-diff seam). The legacy default carries the true
    ``legacy → variant`` delta into the dossier ``param_diff``."""
    return {
        "cluster_window_days": int(CATALYST_CLUSTER_WINDOW_DAYS),
        "event_confirmation_mode": _EVENT_CONFIRMATION_OFF,
        # The hold_days knob defaults to the legacy HOLDING_PERIOD_DAYS
        # constant (30) so the dossier reflects "current engine" as the
        # legacy time-stop horizon, while the Lab samples 5..30. Test:
        # `test_catalyst_backtest.py::test_default_params_carries_legacy_hold_days`.
        "hold_days": int(HOLDING_PERIOD_DAYS),
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


async def _fetch_earnings_events(
    pool,
    *,
    universe: tuple[str, ...],
    start: date_t,
    end: date_t,
) -> pd.DataFrame:
    """Strictly-additive: load ``platform.earnings_events`` rows for
    the universe, restricted to ``event_type='EARNINGS_BEAT'`` AND
    ``magnitude_pct > 0`` (positive beats).

    Consumed only by the ``event_confirmation_mode="positive_beat_30d"``
    variant (spec §8). The legacy code path ignores the returned
    DataFrame, so adding this read is byte-identical to the legacy
    behaviour (proven by the C1 characterization test).
    """
    sql = """
        SELECT ticker, event_date, event_type, magnitude_pct
        FROM platform.earnings_events
        WHERE ticker = ANY($1)
          AND event_type = 'EARNINGS_BEAT'
          AND magnitude_pct > 0
          AND event_date BETWEEN $2 AND $3
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, list(universe), start, end)
    if not rows:
        return pd.DataFrame(
            columns=["ticker", "event_date", "event_type", "magnitude_pct"]
        )
    return pd.DataFrame([
        {"ticker": r["ticker"],
         "event_date": r["event_date"],
         "event_type": r["event_type"],
         "magnitude_pct": float(r["magnitude_pct"]) if r["magnitude_pct"] is not None else 0.0}
        for r in rows
    ])


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
    hold_days: int = HOLDING_PERIOD_DAYS,
) -> SearchTrade | None:
    """Single-entry flat-bracket simulator.

    Enter at the next available close on/after ``entry_date``; exit on
    the first session whose intra-bar (close used as a conservative
    proxy here — same as Vector's first-cut path) hits the TP, SL, or
    trailing-stop trigger; or at ``hold_days`` (time stop).

    ``hold_days`` defaults to the legacy ``HOLDING_PERIOD_DAYS`` (30) so
    pre-enrichment callers stay byte-identical. The Lab-sampled value
    (range 5..30, post-2026-05-22 enrichment) is threaded in via
    :func:`_build_trades`.
    """
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
    horizon = min(len(cut) - 1, max(1, int(hold_days)))
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


def _passes_universe_filters(
    *,
    prices_by_ticker: dict[str, pd.DataFrame],
    ticker: str,
    cursor: date_t,
) -> bool:
    """Shared universe / liquidity / trend gate. Pure.

    Returns True iff ``ticker`` at ``cursor`` clears
        - has at least ``SMA_TREND_PERIOD`` sessions of price data
          strictly up to and including ``cursor``;
        - last close ≥ ``MIN_PRICE``;
        - 20-session average volume ≥ ``MIN_AVG_VOLUME``;
        - last close > 50-SMA (uptrend confirmation).

    Mirrors the gate that the legacy cluster path enforced inline, so
    every mode (off / positive_beat_30d / beat_30d_only) sees the same
    universe-membership rules at entry time.
    """
    prices = prices_by_ticker.get(ticker)
    if prices is None or prices.empty:
        return False
    cut = prices[prices.index <= pd.Timestamp(cursor)].dropna(
        subset=["close"])
    if len(cut) < SMA_TREND_PERIOD:
        return False
    last_close = float(cut["close"].iloc[-1])
    if last_close < float(MIN_PRICE):
        return False
    avg_vol_series = cut["volume"].rolling(20, min_periods=20).mean()
    avg_vol_raw = avg_vol_series.iloc[-1]
    if pd.isna(avg_vol_raw) or int(avg_vol_raw) < MIN_AVG_VOLUME:
        return False
    sma_series = cut["close"].rolling(
        SMA_TREND_PERIOD, min_periods=SMA_TREND_PERIOD).mean()
    sma_val = sma_series.iloc[-1]
    if pd.isna(sma_val) or last_close <= float(sma_val):
        return False
    return True


def _build_trades_beat_only(
    *,
    universe: tuple[str, ...],
    earnings_events: pd.DataFrame | None,
    prices_by_ticker: dict[str, pd.DataFrame],
    round_trip_costs: dict[str, Decimal],
    start: date_t,
    end: date_t,
    hold_days: int,
) -> tuple[list[SearchTrade], list[dict[str, Any]]]:
    """Pure-PEAD branch: iterate ``platform.earnings_events`` BEAT events
    in [start, end]; for each, enter the next session and hold for
    ``hold_days`` (subject to TP/SL/trailing-stop inside the
    :func:`_simulate_trade` flat-bracket).

    NO insider-cluster requirement (``CATALYST_MIN_DISTINCT_INSIDERS``
    skipped) and NO aggregate-value requirement
    (``CATALYST_MIN_AGGREGATE_USD`` skipped). Universe + liquidity +
    SMA gates are applied at entry time (same gates the cluster path
    uses), so the engine still trades only liquid uptrending names.

    Added 2026-05-22 (engine surface enrichment) to express the
    autonomous finder's PEAD hypothesis (3-probe scorecard, candidate
    ``catalyst_pead_expansion_range``). The prior off/positive_beat_30d
    arms both REQUIRED the insider cluster, stripping pure-PEAD
    candidates to n_trades=2.
    """
    trades: list[SearchTrade] = []
    trades_for_diag: list[dict[str, Any]] = []
    if not prices_by_ticker:
        return trades, trades_for_diag
    if earnings_events is None or earnings_events.empty:
        return trades, trades_for_diag

    universe_set = set(universe)
    # Filter the loaded events to the [start, end] window AND the
    # universe; the loader already restricted to event_type='BEAT' AND
    # magnitude_pct > 0 so the in-DataFrame predicate is just the date
    # / universe slice.
    df = earnings_events
    mask = (
        df["ticker"].isin(universe_set)
        & (df["event_date"] >= start)
        & (df["event_date"] <= end)
    )
    qualifying = df[mask].sort_values(["event_date", "ticker"])

    for _, row in qualifying.iterrows():
        ticker = str(row["ticker"])
        event_date = row["event_date"]
        # Apply universe / liquidity / SMA gates AT THE EVENT DATE
        # (the same point-in-time cut the legacy cluster path used).
        if not _passes_universe_filters(
            prices_by_ticker=prices_by_ticker,
            ticker=ticker, cursor=event_date,
        ):
            continue
        prices = prices_by_ticker.get(ticker)
        if prices is None or prices.empty:
            continue
        # Entry on event_date+1 (next available session) — strictly
        # forward (no lookahead). ``_simulate_trade`` already advances
        # from the first row on/after entry_date.
        entry_cursor = event_date + timedelta(days=1)
        rtc = float(round_trip_costs.get(ticker, Decimal("0.001")))
        trade = _simulate_trade(
            ticker=ticker, entry_date=entry_cursor,
            prices=prices, round_trip_cost=rtc,
            hold_days=hold_days,
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
    return trades, trades_for_diag


def _build_trades(
    *,
    universe: tuple[str, ...],
    insider_rows: pd.DataFrame,
    prices_by_ticker: dict[str, pd.DataFrame],
    cluster_window_days: int,
    round_trip_costs: dict[str, Decimal],
    start: date_t,
    end: date_t,
    earnings_events: pd.DataFrame | None = None,
    event_confirmation_mode: str = _EVENT_CONFIRMATION_OFF,
    hold_days: int = HOLDING_PERIOD_DAYS,
) -> tuple[list[SearchTrade], list[dict[str, Any]]]:
    """Walk every (ticker, signal-date) pair in the window where the
    cluster floor + the liquidity/trend gates pass; emit one
    :class:`SearchTrade` per qualified signal.

    When ``event_confirmation_mode == "positive_beat_30d"`` an
    additional gate is applied: the cluster fires only if the same
    ticker has a positive earnings beat in the strictly-backward 30d
    window ``[cursor - 30, cursor]`` (spec §2.2). When the mode is
    ``"beat_30d_only"`` the insider-cluster loop is BYPASSED entirely
    and trades are driven by ``platform.earnings_events`` BEAT rows
    (pure PEAD). When the mode is ``"off"`` (the default + legacy),
    both branches are no-ops and the behaviour is byte-identical to
    the legacy code path.

    ``hold_days`` (post-2026-05-22 enrichment) sets the time-stop
    horizon; defaults to the legacy ``HOLDING_PERIOD_DAYS`` so
    pre-enrichment callers stay byte-identical.
    """
    # Pure-PEAD branch (beat_30d_only): event-driven, not cluster-driven.
    # Short-circuits the cluster loop entirely.
    if event_confirmation_mode == _EVENT_CONFIRMATION_BEAT_30D_ONLY:
        return _build_trades_beat_only(
            universe=universe,
            earnings_events=earnings_events,
            prices_by_ticker=prices_by_ticker,
            round_trip_costs=round_trip_costs,
            start=start, end=end,
            hold_days=hold_days,
        )

    trades: list[SearchTrade] = []
    trades_for_diag: list[dict[str, Any]] = []
    if not prices_by_ticker:
        return trades, trades_for_diag

    apply_event_confirmation = (
        event_confirmation_mode == _EVENT_CONFIRMATION_POSITIVE_BEAT_30D
    )

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
            # Event-confirmation gate (spec §2.2) — strictly-backward
            # 30d window; the predicate is False for any ticker
            # without a positive earnings beat in the window. The
            # legacy path skips this entire branch (mode=="off").
            if apply_event_confirmation and not _has_positive_beat(
                earnings_events, ticker=ticker, cursor=cursor,
                window_days=_EVENT_CONFIRMATION_WINDOW_DAYS,
            ):
                continue
            if not _passes_universe_filters(
                prices_by_ticker=prices_by_ticker,
                ticker=ticker, cursor=cursor,
            ):
                continue
            prices = prices_by_ticker.get(ticker)
            if prices is None or prices.empty:
                continue
            rtc = float(round_trip_costs.get(ticker, Decimal("0.001")))
            trade = _simulate_trade(
                ticker=ticker, entry_date=cursor,
                prices=prices, round_trip_cost=rtc,
                hold_days=hold_days,
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

    ``earnings_events`` is the strictly-additive read consumed only by
    the ``event_confirmation_mode="positive_beat_30d"`` variant (spec
    §8); the legacy code path ignores it. Defaults to an empty
    DataFrame so existing callers (and the legacy code path) remain
    byte-identical without any modification.
    """

    universe: tuple[str, ...]
    insider_rows: pd.DataFrame
    prices_by_ticker: dict[str, pd.DataFrame]
    round_trip_costs: dict[str, Decimal]
    start: date_t
    end: date_t
    earnings_events: pd.DataFrame = field(
        default_factory=lambda: pd.DataFrame(
            columns=["ticker", "event_date", "event_type", "magnitude_pct"]
        )
    )


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
        # Strictly-additive: consumed by both
        # ``event_confirmation_mode="positive_beat_30d"`` (predicate
        # over the strictly-backward 30d window) AND
        # ``event_confirmation_mode="beat_30d_only"`` (event-driven
        # entries on each BEAT in [start, end]). The window is widened
        # by ``_EVENT_CONFIRMATION_WINDOW_DAYS`` so the first cursor's
        # backward 30d window is fully covered for the positive_beat_30d
        # predicate; the beat_30d_only branch's [start, end] slice is a
        # subset of this and trivially covered.
        earnings_events = await _fetch_earnings_events(
            pool, universe=u,
            start=start - timedelta(days=_EVENT_CONFIRMATION_WINDOW_DAYS),
            end=end,
        )
    finally:
        await pool.close()
    return CatalystWindowContext(
        universe=u, insider_rows=insider_rows,
        prices_by_ticker=prices_by_ticker,
        round_trip_costs=round_trip_costs,
        start=start, end=end,
        earnings_events=earnings_events,
    )


def run_catalyst_with_context(
    context: CatalystWindowContext,
    *,
    overrides: dict | None = None,
    trade_log_path: Path | None = None,
) -> BacktestRunResult:
    """Run catalyst against a pre-loaded :class:`CatalystWindowContext`.

    Lab toggles read into off-by-default module overrides and **reset
    per call** in the ``finally:`` block so no module-global state
    bleeds across Lab trials (the per-call reset discipline):

    - ``cluster_window_days`` (legacy SP-F toggle): ``choice:30,45``.
    - ``event_confirmation_mode`` (the event-confirmed insider-cluster
      drift candidate + the post-2026-05-22 pure-PEAD arm):
      ``choice:off,positive_beat_30d,beat_30d_only``. When
      ``"positive_beat_30d"`` a cluster fires only if the same ticker
      has a positive earnings beat in the strictly-backward 30d window;
      when ``"beat_30d_only"`` the insider-cluster requirement is
      bypassed and trades fire on each positive BEAT event.
    - ``hold_days`` (post-2026-05-22 enrichment): ``int 5..30``. The
      time-stop horizon used by :func:`_simulate_trade`. Defaults to
      the legacy ``HOLDING_PERIOD_DAYS`` (30) when the override is
      None (byte-identical to pre-enrichment behaviour).
    """
    global _CLUSTER_WINDOW_OVERRIDE, _EVENT_CONFIRMATION_MODE_OVERRIDE
    global _HOLD_DAYS_OVERRIDE
    overrides = dict(overrides or {})
    _CLUSTER_WINDOW_OVERRIDE = (
        int(overrides["cluster_window_days"])
        if "cluster_window_days" in overrides
        else None
    )
    _EVENT_CONFIRMATION_MODE_OVERRIDE = (
        str(overrides["event_confirmation_mode"])
        if "event_confirmation_mode" in overrides
        else None
    )
    _HOLD_DAYS_OVERRIDE = (
        int(overrides["hold_days"])
        if "hold_days" in overrides
        else None
    )
    try:
        active_window = _cluster_window()
        active_event_mode = _event_confirmation_mode()
        active_hold_days = _hold_days()
        trades, trades_for_diag = _build_trades(
            universe=context.universe,
            insider_rows=context.insider_rows,
            prices_by_ticker=context.prices_by_ticker,
            cluster_window_days=active_window,
            round_trip_costs=context.round_trip_costs,
            start=context.start, end=context.end,
            earnings_events=context.earnings_events,
            event_confirmation_mode=active_event_mode,
            hold_days=active_hold_days,
        )
    finally:
        _CLUSTER_WINDOW_OVERRIDE = None
        _EVENT_CONFIRMATION_MODE_OVERRIDE = None
        _HOLD_DAYS_OVERRIDE = None

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

    parameters: dict[str, Any] = {
        "cluster_window_days": int(active_window),
        "event_confirmation_mode": str(active_event_mode),
        "hold_days": int(active_hold_days),
    }
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
        # SP-F (PR #159) — alternative cluster-window toggle.
        # choice:<csv> (NOT a range/grid).
        "cluster_window_days": (30, 45, "choice:30,45"),
        # Event-confirmed insider-cluster drift + pure-PEAD arm.
        # choice:off,positive_beat_30d,beat_30d_only —
        #   - ``off`` is the legacy cluster-only fire rule (denominator);
        #   - ``positive_beat_30d`` requires BOTH the cluster AND a
        #     positive earnings beat in the strictly-backward 30d
        #     window (spec
        #     docs/superpowers/specs/2026-05-20-catalyst-insider-cluster-
        #     event-lab-candidate.md);
        #   - ``beat_30d_only`` (added 2026-05-22 — engine surface
        #     enrichment) bypasses the cluster entirely and fires on
        #     each positive BEAT event (pure PEAD). The autonomous
        #     finder's ``catalyst_pead_expansion_range`` candidate
        #     needs this arm — the prior two arms stripped pure-PEAD
        #     candidates to n_trades=2.
        "event_confirmation_mode": (
            0, 0, "choice:off,positive_beat_30d,beat_30d_only",
        ),
        # Lab-sampled time-stop horizon (post-2026-05-22 enrichment).
        # The legacy hardcoded value was 30 sessions; the PEAD
        # hypothesis explicitly tests 20-session holds. Range
        # [5, 30] lets the sampler explore the band the hypothesis
        # space defines without prejudging the optimum.
        "hold_days": (5, 30, "int"),
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
