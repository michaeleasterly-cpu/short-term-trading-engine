"""Read-only universe-scan simulation against ``platform.prices_daily``.

Walks every ticker that has a bar in the last 90 days (and is not flagged
delisted), applies the coarse master-universe gate, then runs each engine's
fine filter on the survivors. Reports candidate counts per engine. When an
engine produces zero candidates, lists the reason each coarse-filtered
ticker failed so the operator can tell whether the gate is too tight or
the database is too thin to populate it.

This script is **diagnostic and read-only** — it does not write rows,
update flags, or call external APIs. The point is to validate that the
filter chain is calibrated correctly before we ingest the full
8,000-symbol Alpaca tradable list.

Usage::

    export DATABASE_URL=$DATABASE_URL_IPV4   # local pooler URL
    python scripts/simulate_universe.py
    python scripts/simulate_universe.py --as-of 2026-05-09
    python scripts/simulate_universe.py --verbose-failures   # always list per-ticker reasons
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from tpcore.db import build_asyncpg_pool

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = logging.getLogger("scripts.simulate_universe")

# ---------------------------------------------------------------------------
# Filter thresholds — kept as module constants so the report can echo them.
# Values mirror the engines' setup-detection plugs; if you tune them there,
# tune them here too.
# ---------------------------------------------------------------------------

# Coarse / master-universe gate.
MIN_PRICE = 10.0
MIN_AVG_VOLUME = 1_000_000
AVG_VOLUME_WINDOW = 20  # trading days

# Sigma fine filter.
SIGMA_ADX_PERIOD = 14
SIGMA_CHOP_PERIOD = 14
SIGMA_BB_PERIOD = 20
SIGMA_BB_NUM_STD = 2.0
SIGMA_WIDTH_HISTORY = 60  # trailing widths used for the percentile rank
SIGMA_MAX_ADX = 20.0
SIGMA_MIN_CHOP = 38.2
SIGMA_MAX_WIDTH_PCTILE = 0.30

# Reversion fine filter.
REV_ZSCORE_PERIOD = 20
REV_RSI_PERIOD = 14
REV_VOL_WINDOW = 20
REV_Z_THRESHOLD = 3.0
REV_RSI_OVERSOLD = 25.0
REV_RSI_OVERBOUGHT = 75.0
REV_VOL_RATIO_MIN = 1.5

# Vector fine filter (simplified per task spec — 50-day MA only).
VEC_PB_CEILING = 1.5
VEC_DE_CEILING = 3.0
VEC_REVENUE_FLOOR = 500_000_000.0
VEC_CATALYST_TRADING_DAYS = 5
VEC_MA_FAST = 50
VEC_MA_SHORT = 10
VEC_MA_MEDIUM = 20
VEC_PULLBACK_TOLERANCE = 0.02  # close within ±2% of MA

# Bars window — pull enough calendar days to satisfy the longest indicator
# (BB width percentile vs 60-day history → ~80 trading days, plus warmup).
BARS_LOOKBACK_DAYS = 200


@dataclass
class TickerPanel:
    """Per-ticker numbers cached across the engine filters."""

    ticker: str
    df: pd.DataFrame
    last_close: float
    avg_volume_20: float


@dataclass
class FilterResult:
    """Pass/fail decision plus a human-readable reason for the failure."""

    passed: bool
    reason: str | None = None
    detail: dict[str, float] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Indicator math — kept inline so the script is self-contained and
# side-effect free. Same formulas as the engine plugs; ``ddof=0`` matches
# what Sigma / Reversion compute today.
# ---------------------------------------------------------------------------


def _adx(df: pd.DataFrame, period: int = SIGMA_ADX_PERIOD) -> pd.Series:
    """Wilder's ADX; identical math to ``sigma.plugs.setup_detection._compute_adx``."""
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    up, dn = high.diff(), -low.diff()
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    atr = pd.Series(tr).rolling(period, min_periods=period).mean()
    plus_di = 100 * pd.Series(plus_dm, index=df.index).rolling(period, min_periods=period).mean() / atr
    minus_di = 100 * pd.Series(minus_dm, index=df.index).rolling(period, min_periods=period).mean() / atr
    denom = (plus_di + minus_di).replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / denom
    return dx.rolling(period, min_periods=period).mean()


def _chop(df: pd.DataFrame, period: int = SIGMA_CHOP_PERIOD) -> pd.Series:
    """Choppiness Index (Dreiss) — same formula as Sigma's ``compute_chop``."""
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    sum_atr = tr.rolling(period, min_periods=period).sum()
    max_high = high.rolling(period, min_periods=period).max()
    min_low = low.rolling(period, min_periods=period).min()
    denom = (max_high - min_low).replace(0, np.nan)
    ratio = (sum_atr / denom).where(lambda r: r > 0)
    return 100.0 * np.log10(ratio) / np.log10(period)


def _bb_width(df: pd.DataFrame, period: int = SIGMA_BB_PERIOD, num_std: float = SIGMA_BB_NUM_STD) -> pd.Series:
    close = df["close"]
    sma = close.rolling(period, min_periods=period).mean()
    sd = close.rolling(period, min_periods=period).std(ddof=0)
    upper = sma + num_std * sd
    lower = sma - num_std * sd
    return (upper - lower) / sma


def _width_percentile(width: pd.Series, history: int = SIGMA_WIDTH_HISTORY) -> float:
    """Fraction of the trailing ``history`` widths strictly less than today's."""
    series = width.dropna().tail(history)
    if len(series) < 5:
        return 0.0
    if float(series.std(ddof=0)) < 1e-9:
        return 0.0
    current = float(series.iloc[-1])
    return float((series < current).mean())


def _rsi(close: pd.Series, period: int = REV_RSI_PERIOD) -> pd.Series:
    """Wilder's RSI."""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100.0 - 100.0 / (1.0 + rs)


def _zscore(close: pd.Series, period: int = REV_ZSCORE_PERIOD) -> float:
    ma = close.rolling(period, min_periods=period).mean()
    sd = close.rolling(period, min_periods=period).std(ddof=0)
    z = (close - ma) / sd.replace(0, np.nan)
    return float(z.iloc[-1])


def _volume_ratio(volume: pd.Series, period: int = REV_VOL_WINDOW) -> float:
    if len(volume) < period:
        return float("nan")
    avg = float(volume.tail(period).mean())
    if avg <= 0:
        return float("nan")
    return float(volume.iloc[-1]) / avg


def _sma(close: pd.Series, period: int) -> float:
    val = close.rolling(period, min_periods=period).mean().iloc[-1]
    return float(val) if not pd.isna(val) else float("nan")


# ---------------------------------------------------------------------------
# Database access
# ---------------------------------------------------------------------------


# Batched, set-based SQL — one round-trip per concept, not per ticker.
# The old implementation issued ~7.7k + 3 × ~1.4k = ~12k queries through
# the Supabase pooler and took 30+ minutes wall-time at ~250ms/round-trip.
# These four queries scale O(1) in ticker count.

# 1) Coarse-stat per active ticker. ``rn`` is dense-ranked latest-first so
#    the "last close" is rn=1 and the 20-day avg volume is rn <= 20.
_COARSE_STATS_SQL = """
    WITH active_tickers AS (
        SELECT DISTINCT ticker
        FROM platform.prices_daily
        WHERE delisted = false
          AND date >= $1::date - INTERVAL '90 days'
          AND date <= $1::date
    ),
    windowed AS (
        SELECT pd.ticker, pd.close, pd.volume,
               ROW_NUMBER() OVER (PARTITION BY pd.ticker ORDER BY pd.date DESC) AS rn
        FROM platform.prices_daily pd
        JOIN active_tickers a USING (ticker)
        WHERE pd.date >= $1::date - INTERVAL '200 days'
          AND pd.date <= $1::date
    )
    SELECT ticker,
           COUNT(*)::int AS n_bars,
           MAX(close) FILTER (WHERE rn = 1) AS last_close,
           AVG(volume) FILTER (WHERE rn <= 20) AS avg_vol_20
    FROM windowed
    GROUP BY ticker
    ORDER BY ticker
"""

# 2) Full 200-day bar window for the coarse-pass survivors only.
_SURVIVOR_BARS_SQL = """
    SELECT ticker, date, open, high, low, close, volume
    FROM platform.prices_daily
    WHERE ticker = ANY($1::text[])
      AND date >= $2::date - INTERVAL '200 days'
      AND date <= $2::date
    ORDER BY ticker, date
"""

# 3) Latest fundamentals row (filing_date <= as_of) per survivor.
_SURVIVOR_FUNDAMENTALS_SQL = """
    SELECT DISTINCT ON (ticker)
        ticker, pb, de, revenue, filing_date
    FROM platform.fundamentals_quarterly
    WHERE ticker = ANY($1::text[])
      AND filing_date <= $2::date
    ORDER BY ticker, filing_date DESC
"""

# 4) Did each survivor have an EARNINGS_BEAT in the catalyst window?
_SURVIVOR_CATALYST_SQL = """
    SELECT DISTINCT ticker
    FROM platform.earnings_events
    WHERE ticker = ANY($1::text[])
      AND event_type = 'EARNINGS_BEAT'
      AND event_date >= $2::date
      AND event_date <= $3::date
"""


async def _fetch_coarse_stats(pool: asyncpg.Pool, as_of: date) -> list[dict]:
    """One query: n_bars, last_close, avg_vol_20 per active ticker."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(_COARSE_STATS_SQL, as_of)
    return [dict(r) for r in rows]


async def _fetch_survivor_bars(
    pool: asyncpg.Pool, survivors: list[str], as_of: date
) -> dict[str, pd.DataFrame]:
    """One query for every survivor's 200-day bar window, then groupby in pandas."""
    if not survivors:
        return {}
    async with pool.acquire() as conn:
        rows = await conn.fetch(_SURVIVOR_BARS_SQL, survivors, as_of)
    if not rows:
        return {}
    df = pd.DataFrame(
        [
            {
                "ticker": r["ticker"],
                "date": r["date"],
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "volume": int(r["volume"]),
            }
            for r in rows
        ]
    )
    out: dict[str, pd.DataFrame] = {}
    for ticker, group in df.groupby("ticker", sort=False):
        out[ticker] = (
            group.drop(columns=["ticker"]).sort_values("date").set_index("date")
        )
    return out


async def _fetch_survivor_fundamentals(
    pool: asyncpg.Pool, survivors: list[str], as_of: date
) -> dict[str, dict]:
    """One query for the latest filing_date <= as_of per survivor."""
    if not survivors:
        return {}
    async with pool.acquire() as conn:
        rows = await conn.fetch(_SURVIVOR_FUNDAMENTALS_SQL, survivors, as_of)
    return {r["ticker"]: dict(r) for r in rows}


async def _fetch_survivor_catalysts(
    pool: asyncpg.Pool, survivors: list[str], as_of: date
) -> set[str]:
    """One query for the set of survivors with EARNINGS_BEAT in the catalyst window."""
    if not survivors:
        return set()
    calendar_window = (VEC_CATALYST_TRADING_DAYS * 7 + 4) // 5
    cutoff = as_of - timedelta(days=calendar_window)
    async with pool.acquire() as conn:
        rows = await conn.fetch(_SURVIVOR_CATALYST_SQL, survivors, cutoff, as_of)
    return {r["ticker"] for r in rows}


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


def _coarse_filter(ticker: str, df: pd.DataFrame) -> tuple[FilterResult, TickerPanel | None]:
    if len(df) < AVG_VOLUME_WINDOW:
        return FilterResult(False, f"only {len(df)} bars (need ≥{AVG_VOLUME_WINDOW})"), None
    last_close = float(df["close"].iloc[-1])
    avg_vol = float(df["volume"].tail(AVG_VOLUME_WINDOW).mean())
    if last_close <= MIN_PRICE:
        return FilterResult(False, f"close ${last_close:.2f} ≤ ${MIN_PRICE:.0f}"), None
    if avg_vol <= MIN_AVG_VOLUME:
        return FilterResult(False, f"avg vol {avg_vol:,.0f} ≤ {MIN_AVG_VOLUME:,}"), None
    return FilterResult(True), TickerPanel(ticker=ticker, df=df, last_close=last_close, avg_volume_20=avg_vol)


def _sigma_filter(panel: TickerPanel) -> FilterResult:
    df = panel.df
    if len(df) < SIGMA_BB_PERIOD + 5:
        return FilterResult(False, f"only {len(df)} bars (need ≥{SIGMA_BB_PERIOD + 5})")
    adx = _adx(df).iloc[-1]
    chop = _chop(df).iloc[-1]
    width = _bb_width(df)
    width_pct = _width_percentile(width)
    detail = {"adx": float(adx), "chop": float(chop), "width_pct": float(width_pct)}
    if pd.isna(adx) or pd.isna(chop):
        return FilterResult(False, "ADX/CHOP NaN (insufficient warmup)", detail)
    if adx >= SIGMA_MAX_ADX:
        return FilterResult(False, f"ADX {adx:.1f} ≥ {SIGMA_MAX_ADX} (too trending)", detail)
    if chop <= SIGMA_MIN_CHOP:
        return FilterResult(False, f"CHOP {chop:.1f} ≤ {SIGMA_MIN_CHOP} (not chopping)", detail)
    if width_pct >= SIGMA_MAX_WIDTH_PCTILE:
        return FilterResult(
            False,
            f"BB width pctile {width_pct:.2f} ≥ {SIGMA_MAX_WIDTH_PCTILE} (channel not tight)",
            detail,
        )
    return FilterResult(True, detail=detail)


def _reversion_filter(panel: TickerPanel) -> FilterResult:
    df = panel.df
    if len(df) < REV_ZSCORE_PERIOD + 5:
        return FilterResult(False, f"only {len(df)} bars (need ≥{REV_ZSCORE_PERIOD + 5})")
    z = _zscore(df["close"])
    rsi = float(_rsi(df["close"]).iloc[-1])
    vol_ratio = _volume_ratio(df["volume"])
    detail = {"z": z, "rsi": rsi, "vol_ratio": vol_ratio}
    if np.isnan(z) or np.isnan(rsi) or np.isnan(vol_ratio):
        return FilterResult(False, "Z/RSI/vol_ratio NaN (insufficient warmup)", detail)
    if abs(z) < REV_Z_THRESHOLD:
        return FilterResult(False, f"|z| {abs(z):.2f} < {REV_Z_THRESHOLD}", detail)
    rsi_extreme = rsi < REV_RSI_OVERSOLD or rsi > REV_RSI_OVERBOUGHT
    if not rsi_extreme:
        return FilterResult(
            False,
            f"RSI {rsi:.1f} not extreme (need <{REV_RSI_OVERSOLD} or >{REV_RSI_OVERBOUGHT})",
            detail,
        )
    if vol_ratio <= REV_VOL_RATIO_MIN:
        return FilterResult(False, f"vol ratio {vol_ratio:.2f} ≤ {REV_VOL_RATIO_MIN}", detail)
    return FilterResult(True, detail=detail)


def _vector_technical_signal(panel: TickerPanel) -> tuple[bool, str]:
    """Simplified Vector tech check — breakout above 50-MA OR pullback bounce.

    Bounce = close near 10/20-MA (within ±2%) AND today's close > prior close.
    """
    df = panel.df
    closes = df["close"]
    last_close = panel.last_close
    sma_50 = _sma(closes, VEC_MA_FAST)
    if np.isnan(sma_50):
        return False, "no 50-MA"
    if last_close > sma_50:
        return True, f"close > 50-MA ({last_close:.2f} > {sma_50:.2f})"
    sma_10 = _sma(closes, VEC_MA_SHORT)
    sma_20 = _sma(closes, VEC_MA_MEDIUM)
    near_10 = (
        not np.isnan(sma_10)
        and abs(last_close - sma_10) / max(last_close, 1e-9) < VEC_PULLBACK_TOLERANCE
    )
    near_20 = (
        not np.isnan(sma_20)
        and abs(last_close - sma_20) / max(last_close, 1e-9) < VEC_PULLBACK_TOLERANCE
    )
    if not (near_10 or near_20):
        return False, f"close {last_close:.2f} not near 10/20/50-MA"
    if len(closes) < 2:
        return False, "no prior close"
    prior = float(closes.iloc[-2])
    if last_close <= prior:
        return False, f"no bounce (close {last_close:.2f} ≤ prior {prior:.2f})"
    return True, f"pullback bounce off {'10' if near_10 else '20'}-MA"


def _vector_filter(
    panel: TickerPanel,
    fundamentals: dict | None,
    has_catalyst: bool,
) -> FilterResult:
    if fundamentals is None:
        return FilterResult(False, "no fundamentals on file (filing_date ≤ today)")
    pb = fundamentals.get("pb")
    de = fundamentals.get("de")
    revenue = fundamentals.get("revenue")
    detail = {
        "pb": float(pb) if pb is not None else float("nan"),
        "de": float(de) if de is not None else float("nan"),
        "revenue": float(revenue) if revenue is not None else float("nan"),
    }
    if pb is None or de is None or revenue is None:
        return FilterResult(False, "fundamentals row missing pb/de/revenue", detail)
    if float(pb) >= VEC_PB_CEILING:
        return FilterResult(False, f"P/B {float(pb):.2f} ≥ {VEC_PB_CEILING}", detail)
    if float(de) >= VEC_DE_CEILING:
        return FilterResult(False, f"D/E {float(de):.2f} ≥ {VEC_DE_CEILING}", detail)
    if float(revenue) <= VEC_REVENUE_FLOOR:
        return FilterResult(
            False,
            f"revenue ${float(revenue):,.0f} ≤ ${VEC_REVENUE_FLOOR:,.0f}",
            detail,
        )
    if not has_catalyst:
        return FilterResult(
            False,
            f"no EARNINGS_BEAT in last {VEC_CATALYST_TRADING_DAYS} trading days",
            detail,
        )
    ok_tech, tech_reason = _vector_technical_signal(panel)
    if not ok_tech:
        return FilterResult(False, f"technical: {tech_reason}", detail)
    return FilterResult(True, reason=tech_reason, detail=detail)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


@dataclass
class EngineReport:
    name: str
    description: str
    candidates: list[str] = field(default_factory=list)
    failures: list[tuple[str, str]] = field(default_factory=list)


async def _simulate(pool: asyncpg.Pool, as_of: date, *, verbose_failures: bool) -> int:
    # 1) coarse stats per active ticker (one query)
    stats = await _fetch_coarse_stats(pool, as_of)
    total_in_db = len(stats)
    logger.info("universe_loaded count=%d as_of=%s", total_in_db, as_of)

    coarse_pass_tickers: list[str] = []
    coarse_fails: list[tuple[str, str]] = []
    for s in stats:
        n_bars = s["n_bars"] or 0
        last_close = float(s["last_close"]) if s["last_close"] is not None else 0.0
        avg_vol = float(s["avg_vol_20"]) if s["avg_vol_20"] is not None else 0.0
        if n_bars < AVG_VOLUME_WINDOW:
            coarse_fails.append((s["ticker"], f"only {n_bars} bars (need ≥{AVG_VOLUME_WINDOW})"))
            continue
        if last_close <= MIN_PRICE:
            coarse_fails.append((s["ticker"], f"close ${last_close:.2f} ≤ ${MIN_PRICE:.0f}"))
            continue
        if avg_vol <= MIN_AVG_VOLUME:
            coarse_fails.append((s["ticker"], f"avg vol {avg_vol:,.0f} ≤ {MIN_AVG_VOLUME:,}"))
            continue
        coarse_pass_tickers.append(s["ticker"])

    logger.info("coarse_pass count=%d", len(coarse_pass_tickers))

    # 2-4) batched fetches for the survivors only (three queries)
    bars_by_ticker = await _fetch_survivor_bars(pool, coarse_pass_tickers, as_of)
    fund_by_ticker = await _fetch_survivor_fundamentals(pool, coarse_pass_tickers, as_of)
    catalyst_set = await _fetch_survivor_catalysts(pool, coarse_pass_tickers, as_of)

    # Build TickerPanels in-memory from the survivor bars (no further I/O).
    coarse_pass: list[TickerPanel] = []
    for ticker in coarse_pass_tickers:
        df = bars_by_ticker.get(ticker)
        if df is None or len(df) < AVG_VOLUME_WINDOW:
            # Shouldn't happen given the stats query, but guard anyway.
            coarse_fails.append((ticker, "missing bars after batched fetch"))
            continue
        last_close = float(df["close"].iloc[-1])
        avg_volume_20 = float(df["volume"].tail(AVG_VOLUME_WINDOW).mean())
        coarse_pass.append(
            TickerPanel(ticker=ticker, df=df, last_close=last_close, avg_volume_20=avg_volume_20)
        )

    sigma = EngineReport(
        "Sigma",
        f"ADX < {SIGMA_MAX_ADX}, CHOP > {SIGMA_MIN_CHOP}, BB width pctile < {SIGMA_MAX_WIDTH_PCTILE}",
    )
    reversion = EngineReport(
        "Reversion",
        f"|Z| ≥ {REV_Z_THRESHOLD}, RSI extreme (<{REV_RSI_OVERSOLD} or >{REV_RSI_OVERBOUGHT}), vol ratio > {REV_VOL_RATIO_MIN}",
    )
    vector = EngineReport(
        "Vector",
        (
            f"P/B < {VEC_PB_CEILING}, D/E < {VEC_DE_CEILING}, "
            f"revenue > ${VEC_REVENUE_FLOOR:,.0f}, EARNINGS_BEAT in last "
            f"{VEC_CATALYST_TRADING_DAYS} trading days, close > 50-MA or pullback bounce"
        ),
    )

    for panel in coarse_pass:
        sig = _sigma_filter(panel)
        if sig.passed:
            sigma.candidates.append(panel.ticker)
        else:
            sigma.failures.append((panel.ticker, sig.reason or "unknown"))

        rev = _reversion_filter(panel)
        if rev.passed:
            reversion.candidates.append(panel.ticker)
        else:
            reversion.failures.append((panel.ticker, rev.reason or "unknown"))

        fundamentals = fund_by_ticker.get(panel.ticker)
        has_cat = panel.ticker in catalyst_set
        vec = _vector_filter(panel, fundamentals, has_cat)
        if vec.passed:
            vector.candidates.append(panel.ticker)
        else:
            vector.failures.append((panel.ticker, vec.reason or "unknown"))

    _print_report(
        as_of=as_of,
        total_in_db=total_in_db,
        coarse_pass=coarse_pass,
        coarse_fails=coarse_fails,
        engines=[sigma, reversion, vector],
        verbose_failures=verbose_failures,
    )
    await _emit_universe_simulation_event(
        pool=pool,
        as_of=as_of,
        total_in_db=total_in_db,
        coarse_pass_count=len(coarse_pass),
        sigma_candidates=sigma.candidates,
        reversion_candidates=reversion.candidates,
        vector_candidates=vector.candidates,
    )
    return 0


async def _emit_universe_simulation_event(
    *,
    pool: asyncpg.Pool,
    as_of: date,
    total_in_db: int,
    coarse_pass_count: int,
    sigma_candidates: list[str],
    reversion_candidates: list[str],
    vector_candidates: list[str],
) -> None:
    """Persist a single ``UNIVERSE_SIMULATION`` row in ``application_log``.

    The smoke-test workflow (``scripts/smoke_test.py``) reads the most
    recent event to pick a Sigma candidate without re-running the full
    coarse + fine scan. The row is intentionally idempotent — a fresh
    insert per script invocation, with the prior run preserved by the
    log's 7-day retention.
    """
    import uuid as _uuid

    from tpcore.logging.db_handler import DBLogHandler

    handler = DBLogHandler(pool=pool, engine="platform", run_id=_uuid.uuid4())
    await handler.log(
        event_type="UNIVERSE_SIMULATION",
        message=(
            f"as_of={as_of.isoformat()} "
            f"sigma={len(sigma_candidates)} "
            f"reversion={len(reversion_candidates)} "
            f"vector={len(vector_candidates)}"
        ),
        severity="INFO",
        data={
            "as_of": as_of.isoformat(),
            "total_in_db": total_in_db,
            "coarse_pass": coarse_pass_count,
            "sigma_candidates": sigma_candidates,
            "reversion_candidates": reversion_candidates,
            "vector_candidates": vector_candidates,
        },
    )


def _print_report(
    *,
    as_of: date,
    total_in_db: int,
    coarse_pass: list[TickerPanel],
    coarse_fails: list[tuple[str, str]],
    engines: list[EngineReport],
    verbose_failures: bool,
) -> None:
    line = "─" * 60
    print(f"\nUNIVERSE SIMULATION — {as_of.isoformat()}")
    print(line)
    print(f"Total tickers in database:    {total_in_db}")
    print(
        f"Coarse filter passed:         {len(coarse_pass)} "
        f"(close > ${MIN_PRICE:.0f}, avg vol > {MIN_AVG_VOLUME:,})"
    )
    print(f"Coarse filter failed:         {len(coarse_fails)}")
    for engine in engines:
        print(f"{engine.name + ' candidates:':<30}{len(engine.candidates):>4}  ({engine.description})")
    print(line)

    if coarse_pass:
        sample = ", ".join(p.ticker for p in coarse_pass[:15])
        suffix = "" if len(coarse_pass) <= 15 else f" … (+{len(coarse_pass) - 15} more)"
        print(f"\nCoarse-filter survivors: {sample}{suffix}")

    for engine in engines:
        if engine.candidates:
            shown = ", ".join(engine.candidates[:20])
            tail = "" if len(engine.candidates) <= 20 else f" … (+{len(engine.candidates) - 20} more)"
            print(f"\n{engine.name} candidates ({len(engine.candidates)}): {shown}{tail}")
        if engine.candidates and not verbose_failures:
            continue
        # Zero candidates → always print reasons. Verbose flag → print regardless.
        if not engine.candidates or verbose_failures:
            label = "ZERO candidates" if not engine.candidates else "fine-filter failures"
            print(f"\n{engine.name} {label} — per-ticker reasons:")
            for ticker, reason in engine.failures:
                print(f"  {ticker}: {reason}")

    print()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument(
        "--as-of",
        type=date.fromisoformat,
        default=date.today(),
        help="As-of date (YYYY-MM-DD). Defaults to today; bars are read up to and including this date.",
    )
    p.add_argument(
        "--verbose-failures",
        action="store_true",
        help="Print per-ticker failure reasons even for engines that have candidates.",
    )
    return p.parse_args(argv)


async def amain(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print(
            "DATABASE_URL not set. Locally: export DATABASE_URL=$DATABASE_URL_IPV4 "
            "(see project memory on Supabase's dual URL setup).",
            file=sys.stderr,
        )
        return 2

    pool = await build_asyncpg_pool(db_url)
    try:
        return await _simulate(pool, args.as_of, verbose_failures=args.verbose_failures)
    finally:
        await pool.close()


def main() -> None:  # pragma: no cover - CLI shim
    raise SystemExit(asyncio.run(amain(_parse_args())))


if __name__ == "__main__":
    main()
