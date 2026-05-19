"""Catalyst — Plug 1: Setup Detection (insider-cluster leg only).

Reads recent ``platform.sec_insider_transactions`` rows and per-ticker
``platform.prices_daily`` rows; emits a :class:`SetupCandidate` for any
ticker that

1. has ≥ ``CATALYST_MIN_DISTINCT_INSIDERS`` distinct insiders BUYing
   inside the most recent ``CATALYST_CLUSTER_WINDOW_DAYS`` calendar-day
   window ending at ``as_of`` (strict point-in-time: rows with
   ``filing_date <= as_of`` only — no lookahead);
2. has ≥ ``CATALYST_MIN_AGGREGATE_USD`` aggregate BUY dollar value
   over the same window;
3. clears the universe-liquidity gate (price ≥ ``MIN_PRICE``,
   20-day average volume ≥ ``MIN_AVG_VOLUME``);
4. closes above its 50-day SMA (the basic trend filter — a cheap
   "is this name in an uptrend?" check that matches Vector's pattern).

The plug is pure: callers fetch the SEC + price panels and hand them
in. Returns ``(candidates, FilterDiagnostics)``. The scheduler lifts
the diagnostics onto ``db_log.signal(..., extra_data=...)``.

Lookahead honesty (lab_candidate_readiness §9): the cluster window is
``[as_of − CATALYST_CLUSTER_WINDOW_DAYS, as_of]`` — strictly backward,
no row dated after ``as_of`` enters a score. The SMA is computed from
prices up to and including ``as_of`` only.
"""
from __future__ import annotations

from datetime import date as date_t
from datetime import timedelta
from decimal import Decimal

import pandas as pd
import structlog

from catalyst.models import (
    CATALYST_CLUSTER_WINDOW_DAYS,
    CATALYST_MIN_AGGREGATE_USD,
    CATALYST_MIN_DISTINCT_INSIDERS,
    MIN_AVG_VOLUME,
    MIN_PRICE,
    SMA_TREND_PERIOD,
    InsiderCluster,
    SetupCandidate,
)
from tpcore.backtest.filter_diagnostics import FilterDiagnostics
from tpcore.interfaces.engine_plug import BaseEnginePlug

logger = structlog.get_logger(__name__)


def _density_score(aggregate_usd: Decimal, distinct: int) -> float:
    """Cluster density = aggregate USD × distinct insiders.

    Multiplying (not dividing) by ``distinct`` privileges quorum-driven
    clusters over a single insider's large block — a sentinel-CEO buy
    of $10M scores lower than three executives each buying $400k
    ($10M × 1 = 10M, vs. $1.2M × 3 = 3.6M only when the dollar gap is
    very large; the 3-insider case wins the density race at common
    dollar levels). Pure float for sort stability.
    """
    return float(aggregate_usd) * float(distinct)


def detect_clusters(
    *,
    insider_rows: pd.DataFrame,
    as_of: date_t,
    window_days: int = CATALYST_CLUSTER_WINDOW_DAYS,
) -> dict[str, InsiderCluster]:
    """Pure: aggregate BUY rows over ``[as_of − window_days, as_of]`` by ticker.

    Args:
        insider_rows: dataframe with columns
            ``{ticker, filing_date, insider_name, transaction_type, value}``.
            ``transaction_type`` is one of ``BUY``/``SELL`` (the migration's
            CHECK constraint guarantees this); only BUYs are aggregated.
        as_of: the right edge of the window (inclusive).
        window_days: calendar days back from ``as_of``.

    Returns: ``{ticker: InsiderCluster}``. Tickers with no qualifying
    BUYs are omitted (callers treat absence as "no cluster").
    """
    if insider_rows.empty:
        return {}
    start = as_of - timedelta(days=window_days)
    mask_window = (insider_rows["filing_date"] >= start) & (
        insider_rows["filing_date"] <= as_of)
    mask_buy = insider_rows["transaction_type"] == "BUY"
    window = insider_rows[mask_window & mask_buy]
    out: dict[str, InsiderCluster] = {}
    if window.empty:
        return out
    for ticker, group in window.groupby("ticker"):
        distinct = int(group["insider_name"].nunique())
        agg = Decimal(str(group["value"].sum()))
        out[str(ticker)] = InsiderCluster(
            ticker=str(ticker), as_of=as_of, window_days=window_days,
            distinct_insiders=distinct, aggregate_value_usd=agg,
            n_buy_transactions=int(len(group)),
        )
    return out


class CatalystSetupDetection(BaseEnginePlug):
    """Plug 1 — insider-cluster setup detection."""

    engine_name = "catalyst"

    def validate_dependencies(self) -> bool:
        """Module-level constants must be coherent (no zero windows / floors)."""
        return (
            CATALYST_CLUSTER_WINDOW_DAYS > 0
            and CATALYST_MIN_DISTINCT_INSIDERS >= 1
            and CATALYST_MIN_AGGREGATE_USD >= Decimal("0")
            and SMA_TREND_PERIOD >= 1
        )

    def healthcheck(self) -> dict:
        return {
            "engine": self.engine_name,
            "plug": "setup_detection",
            "ok": True,
            "details": {
                "window_days": CATALYST_CLUSTER_WINDOW_DAYS,
                "min_distinct_insiders": CATALYST_MIN_DISTINCT_INSIDERS,
                "min_aggregate_usd": str(CATALYST_MIN_AGGREGATE_USD),
            },
        }

    def detect(
        self,
        *,
        as_of: date_t,
        universe: tuple[str, ...],
        insider_rows: pd.DataFrame,
        prices_by_ticker: dict[str, pd.DataFrame],
    ) -> tuple[list[SetupCandidate], FilterDiagnostics]:
        """Scan ``universe`` for insider-cluster setups as of ``as_of``.

        ``prices_by_ticker[t]`` is a dataframe indexed by date with at
        minimum a ``close`` and a ``volume`` column, strictly up to and
        including ``as_of`` (the caller enforces the point-in-time cut).
        """
        diag = FilterDiagnostics(universe_total=len(universe))
        clusters = detect_clusters(
            insider_rows=insider_rows, as_of=as_of,
            window_days=CATALYST_CLUSTER_WINDOW_DAYS,
        )
        candidates: list[SetupCandidate] = []
        for ticker in universe:
            cl = clusters.get(ticker)
            if cl is None or cl.distinct_insiders < CATALYST_MIN_DISTINCT_INSIDERS:
                diag.cluster_size_blocked = (diag.cluster_size_blocked or 0) + 1
                continue
            if cl.aggregate_value_usd < CATALYST_MIN_AGGREGATE_USD:
                diag.cluster_value_blocked = (diag.cluster_value_blocked or 0) + 1
                continue
            prices = prices_by_ticker.get(ticker)
            if prices is None or prices.empty:
                diag.catalyst_liquidity_blocked = (
                    diag.catalyst_liquidity_blocked or 0) + 1
                continue
            # Strict point-in-time cut; the caller MUST already have
            # constrained to as_of, but guard so a bad caller can never
            # smuggle a lookahead row through.
            cut = prices[prices.index <= pd.Timestamp(as_of)].dropna(
                subset=["close"])
            if len(cut) < SMA_TREND_PERIOD:
                diag.catalyst_liquidity_blocked = (
                    diag.catalyst_liquidity_blocked or 0) + 1
                continue
            last_close = Decimal(str(round(float(cut["close"].iloc[-1]), 4)))
            if last_close < MIN_PRICE:
                diag.catalyst_liquidity_blocked = (
                    diag.catalyst_liquidity_blocked or 0) + 1
                continue
            avg_vol_raw = cut["volume"].rolling(20, min_periods=20).mean().iloc[-1]
            if pd.isna(avg_vol_raw):
                diag.catalyst_liquidity_blocked = (
                    diag.catalyst_liquidity_blocked or 0) + 1
                continue
            avg_vol = int(avg_vol_raw)
            if avg_vol < MIN_AVG_VOLUME:
                diag.catalyst_liquidity_blocked = (
                    diag.catalyst_liquidity_blocked or 0) + 1
                continue
            sma = Decimal(str(round(
                float(cut["close"].rolling(SMA_TREND_PERIOD,
                                           min_periods=SMA_TREND_PERIOD)
                      .mean().iloc[-1]), 4)))
            if last_close <= sma:
                diag.catalyst_trend_blocked = (
                    diag.catalyst_trend_blocked or 0) + 1
                continue
            density = _density_score(cl.aggregate_value_usd, cl.distinct_insiders)
            candidates.append(SetupCandidate(
                ticker=ticker, as_of=as_of, cluster=cl,
                cluster_density=density, last_close=last_close, sma_50=sma,
                avg_volume=avg_vol, filter_diagnostics=diag,
            ))
            diag.candidates_passed += 1
        return candidates, diag


__all__ = [
    "CatalystSetupDetection",
    "_density_score",
    "detect_clusters",
]
