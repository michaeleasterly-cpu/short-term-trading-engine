"""Sigma — Plug 1: Setup Detection.

Scans the universe for low-volatility, range-bound names per plan §4.1.

Pipeline per ticker:
    fetch 60 daily bars
        -> ADX(14), BollingerBands(20, 2), BB-width percentile, band proximity, volume trend
        -> Universe filter (price > $10, avg vol > 1M, ADX < 20, width < 30th pctile)
        -> Composite score (channel quality / entry precision / market context)
        -> SetupCandidate iff score >= 50
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import numpy as np
import pandas as pd
import structlog

from tpcore.interfaces.data import Bar, DataProviderInterface
from tpcore.interfaces.engine_plug import BaseEnginePlug

from sigma.models import (
    SCORE_WEAK,
    SIGMA_TEST_UNIVERSE,
    SetupCandidate,
)

logger = structlog.get_logger(__name__)

LOOKBACK_DAYS = 60
ADX_PERIOD = 14
BB_PERIOD = 20
BB_NUM_STD = 2.0

# Universe-filter thresholds from plan §4.1.
MIN_PRICE = Decimal("10")
MIN_AVG_VOLUME = 1_000_000
MAX_ADX = 20.0
MAX_WIDTH_PCTILE = 0.30


def _bars_to_frame(bars: list[Bar]) -> pd.DataFrame:
    """Cast list[Bar] → pandas DataFrame with float columns sorted by ts."""
    if not bars:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    rows = [
        {
            "ts": b.ts,
            "open": float(b.open),
            "high": float(b.high),
            "low": float(b.low),
            "close": float(b.close),
            "volume": int(b.volume),
        }
        for b in bars
    ]
    df = pd.DataFrame(rows).sort_values("ts").set_index("ts")
    return df


def _compute_adx(df: pd.DataFrame, period: int = ADX_PERIOD) -> pd.Series:
    """Wilder's ADX. Returns a Series indexed like df."""
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)

    tr = pd.concat(
        [(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)

    up = high.diff()
    dn = -low.diff()
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)

    atr = pd.Series(tr).rolling(period, min_periods=period).mean()
    plus_di = 100 * pd.Series(plus_dm, index=df.index).rolling(period, min_periods=period).mean() / atr
    minus_di = 100 * pd.Series(minus_dm, index=df.index).rolling(period, min_periods=period).mean() / atr

    denom = (plus_di + minus_di).replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / denom
    adx = dx.rolling(period, min_periods=period).mean()
    return adx


def _compute_bbands(
    df: pd.DataFrame, period: int = BB_PERIOD, num_std: float = BB_NUM_STD
) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    """Returns (sma, upper, lower, width_normalized)."""
    close = df["close"]
    sma = close.rolling(period, min_periods=period).mean()
    sd = close.rolling(period, min_periods=period).std(ddof=0)
    upper = sma + num_std * sd
    lower = sma - num_std * sd
    width = (upper - lower) / sma
    return sma, upper, lower, width


def _band_proximity(close: float, upper: float, lower: float) -> float:
    """0.0 = at lower band, 1.0 = at upper band; outside the band is < 0 or > 1."""
    span = upper - lower
    if span <= 0:
        return 0.5
    return float((close - lower) / span)


def _width_percentile(width: pd.Series) -> float:
    """Where today's BB width sits in the distribution of recent widths.

    Returns ``0`` when the series is degenerate (≤4 values or near-zero variance);
    a uniformly tight channel should not be flagged as "wider than usual".
    """
    series = width.dropna()
    if len(series) < 5:
        return 0.0
    if float(series.std(ddof=0)) < 1e-9:
        return 0.0
    current = float(series.iloc[-1])
    return float((series < current).mean())


def _volume_trend(df: pd.DataFrame, fast: int = 5, slow: int = 20) -> float:
    """Ratio of recent fast-window volume to slow-window volume. Above 1 = picking up."""
    if len(df) < slow:
        return 1.0
    fast_avg = df["volume"].tail(fast).mean()
    slow_avg = df["volume"].tail(slow).mean()
    if slow_avg <= 0:
        return 1.0
    return float(fast_avg / slow_avg)


def _score_channel_quality(adx_now: float, width_pctile: float, width_stability: float) -> float:
    """0–40. Lower ADX, tighter percentile, more stable width → higher score."""
    if np.isnan(adx_now) or np.isnan(width_pctile):
        return 0.0
    # ADX: 0 → full 20, scaling linearly to 0 at ADX=20.
    adx_pts = max(0.0, 20.0 * (1.0 - adx_now / MAX_ADX))
    # Tightness: ≤30th pctile → up to 12 pts; outside → 0.
    tight_pts = max(0.0, 12.0 * (1.0 - width_pctile / MAX_WIDTH_PCTILE))
    # Stability: width_stability ∈ [0, 1] — higher is better.
    stab_pts = max(0.0, min(8.0, 8.0 * width_stability))
    return float(min(40.0, adx_pts + tight_pts + stab_pts))


def _score_entry_precision(band_proximity_val: float) -> float:
    """0–35. 0.0 (at lower band) → 35; 0.5 (mid) → 0; clamp."""
    if band_proximity_val >= 0.5:
        return 0.0
    return float(max(0.0, min(35.0, 35.0 * (1.0 - band_proximity_val / 0.5))))


def _score_market_context(volume_ratio: float) -> float:
    """0–25. Stable-to-rising volume preferred; collapse penalised."""
    if volume_ratio >= 1.5:
        return 25.0
    if volume_ratio >= 1.0:
        return 18.0
    if volume_ratio >= 0.7:
        return 10.0
    return 4.0


class SigmaSetupDetection(BaseEnginePlug):
    """Plug 1 of Sigma — universe scan + scoring."""

    engine_name = "sigma"

    def __init__(
        self,
        data: DataProviderInterface,
        universe: tuple[str, ...] = SIGMA_TEST_UNIVERSE,
    ) -> None:
        self._data = data
        self._universe = universe

    def validate_dependencies(self) -> bool:
        return self._data is not None

    def healthcheck(self) -> dict:
        return {
            "engine": self.engine_name,
            "plug": "setup_detection",
            "ok": self.validate_dependencies(),
            "details": {"universe_size": len(self._universe), "lookback_days": LOOKBACK_DAYS},
        }

    async def scan(self, as_of: date) -> list[SetupCandidate]:
        """Run the scan for ``as_of`` and return all qualifying candidates.

        ``as_of`` is the inclusive end date (NYSE session). Bars are fetched
        from ``as_of - 90`` calendar days back to give 60-ish trading sessions.
        """
        start = as_of - timedelta(days=LOOKBACK_DAYS + 30)
        candidates: list[SetupCandidate] = []
        for symbol in self._universe:
            bars = await self._data.get_daily_bars(symbol, start, as_of)
            try:
                cand = self._evaluate(symbol, as_of, bars)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("sigma.setup.evaluate_failed", symbol=symbol, error=str(exc))
                continue
            if cand is not None:
                candidates.append(cand)
        candidates.sort(key=lambda c: c.sigma_score, reverse=True)
        return candidates

    def _evaluate(self, symbol: str, as_of: date, bars: list[Bar]) -> SetupCandidate | None:
        df = _bars_to_frame(bars)
        if len(df) < BB_PERIOD + 5:
            return None

        last_close = float(df["close"].iloc[-1])
        avg_vol_20 = float(df["volume"].tail(BB_PERIOD).mean())
        if last_close < float(MIN_PRICE) or avg_vol_20 < MIN_AVG_VOLUME:
            return None

        adx_series = _compute_adx(df)
        sma, upper, lower, width = _compute_bbands(df)
        adx_now = float(adx_series.iloc[-1])
        upper_now = float(upper.iloc[-1])
        lower_now = float(lower.iloc[-1])
        if np.isnan(adx_now) or np.isnan(upper_now) or np.isnan(lower_now):
            return None

        # Universe filters.
        if adx_now >= MAX_ADX:
            return None
        width_pctile = _width_percentile(width)
        if width_pctile >= MAX_WIDTH_PCTILE:
            return None

        prox = _band_proximity(last_close, upper_now, lower_now)
        recent_widths = width.dropna().tail(BB_PERIOD)
        # Stability: 1 - normalized stddev of the recent width series.
        if len(recent_widths) >= 2 and recent_widths.mean() > 0:
            stability = max(0.0, 1.0 - float(recent_widths.std(ddof=0) / recent_widths.mean()))
        else:
            stability = 0.0
        vol_ratio = _volume_trend(df)

        cq = _score_channel_quality(adx_now, width_pctile, stability)
        ep = _score_entry_precision(prox)
        mc = _score_market_context(vol_ratio)
        score = cq + ep + mc
        if score < SCORE_WEAK:
            return None

        mid_now = float(sma.iloc[-1])
        return SetupCandidate(
            ticker=symbol,
            as_of=as_of,
            sigma_score=round(score, 2),
            channel_quality=round(cq, 2),
            entry_precision=round(ep, 2),
            market_context=round(mc, 2),
            band_proximity=round(prox, 4),
            bb_width_percentile=round(width_pctile, 4),
            adx=round(adx_now, 2),
            suggested_entry_price=Decimal(str(round(last_close, 2))),
            bb_upper=Decimal(str(round(upper_now, 2))),
            bb_lower=Decimal(str(round(lower_now, 2))),
            bb_mid=Decimal(str(round(mid_now, 2))),
        )
