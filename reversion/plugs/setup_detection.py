"""Reversion — Plug 1: Setup Detection.

Per plan §4.2 the engine fades statistical-extreme deviations from the
20-day moving average. Two-sided: oversold names are entered LONG,
overbought names are entered SHORT. (The scheduler can clamp to LONG-only
for paper trading.)

Score components (sum to 0–100):
    * Statistical Extremity (0–45):
        - |Z-score| ≥ 2.0 (20 pts)
        - BB breach 2+ consecutive days (15 pts)
        - RSI < 25 (oversold) or > 75 (overbought) (10 pts)
    * Exhaustion Confirmation (0–30):
        - Volume ≥ 2× the 20-day average on the extreme day (15 pts)
        - Reversal candlestick (hammer for oversold, shooting star for
          overbought) (10 pts)
        - RSI divergence — a fresh price extreme that the latest RSI
          doesn't confirm (5 pts)
    * Market Context (0–25):
        - "Sector" reversion proxy: stock's z-score sign matches SPY's
          z-score sign (both stretched the same way) (15 pts)
        - VIX context: VIX > 25 for oversold fades or VIX < 15 for
          overbought fades (10 pts). Falls back to SPY 20-day realized
          volatility when VIX bars aren't available on the data feed.

Score threshold: ≥ 50 to qualify, sorted descending. Universe filters
(price > $10, avg vol > 1M, ADX(14) ≤ 25) gate before scoring.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import numpy as np
import pandas as pd
import structlog

from reversion.models import (
    MAX_ADX_FOR_REVERSION,
    REVERSION_TEST_UNIVERSE,
    RSI_OVERBOUGHT,
    RSI_OVERSOLD,
    SCORE_WEAK,
    Z_SCORE_THRESHOLD,
    Direction,
    SetupCandidate,
)
from tpcore.interfaces.data import Bar, DataProviderInterface
from tpcore.interfaces.engine_plug import BaseEnginePlug

logger = structlog.get_logger(__name__)

LOOKBACK_DAYS = 60
ADX_PERIOD = 14
RSI_PERIOD = 14
ZSCORE_PERIOD = 20
BB_PERIOD = 20
BB_NUM_STD = 2.0
MA_50_PERIOD = 50
DIVERGENCE_LOOKBACK = 10

# Universe-filter thresholds.
MIN_PRICE = Decimal("10")
MIN_AVG_VOLUME = 1_000_000

# VIX gates — defaulted from common practice. Fallback path uses SPY 20-day
# annualized realized vol scaled to a comparable units.
VIX_OVERSOLD_THRESHOLD = 25.0
VIX_OVERBOUGHT_THRESHOLD = 15.0
VIX_PROXY_SYMBOL = "VIX"
SPY_SYMBOL = "SPY"


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────


def _bars_to_frame(bars: list[Bar]) -> pd.DataFrame:
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
    return pd.DataFrame(rows).sort_values("ts").set_index("ts")


def _compute_adx(df: pd.DataFrame, period: int = ADX_PERIOD) -> pd.Series:
    """Wilder's ADX (same formula as Sigma's)."""
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()], axis=1
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


def _compute_rsi(close: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    """Wilder's RSI."""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100.0 - 100.0 / (1.0 + rs)


def _compute_zscore(close: pd.Series, period: int = ZSCORE_PERIOD) -> pd.Series:
    ma = close.rolling(period, min_periods=period).mean()
    sd = close.rolling(period, min_periods=period).std(ddof=0)
    return (close - ma) / sd.replace(0, np.nan)


def _compute_bbands(
    df: pd.DataFrame, period: int = BB_PERIOD, num_std: float = BB_NUM_STD
) -> tuple[pd.Series, pd.Series, pd.Series]:
    close = df["close"]
    sma = close.rolling(period, min_periods=period).mean()
    sd = close.rolling(period, min_periods=period).std(ddof=0)
    return sma, sma + num_std * sd, sma - num_std * sd


def _bb_breach_consecutive(close: pd.Series, upper: pd.Series, lower: pd.Series) -> int:
    """Count trailing consecutive bars closed outside the bands.

    Counts on whichever side is currently breached (last bar's side).
    Returns 0 if the last bar is inside the bands.
    """
    last_close = float(close.iloc[-1])
    last_upper = float(upper.iloc[-1])
    last_lower = float(lower.iloc[-1])
    if last_close > last_upper:
        run = 0
        for c, u in zip(close.iloc[::-1], upper.iloc[::-1], strict=True):
            if pd.isna(c) or pd.isna(u) or c <= u:
                break
            run += 1
        return run
    if last_close < last_lower:
        run = 0
        for c, ll in zip(close.iloc[::-1], lower.iloc[::-1], strict=True):
            if pd.isna(c) or pd.isna(ll) or c >= ll:
                break
            run += 1
        return run
    return 0


def _volume_ratio(volume: pd.Series, period: int = ZSCORE_PERIOD) -> float:
    """Latest volume divided by its trailing N-day mean."""
    if len(volume) < period:
        return 1.0
    avg = float(volume.tail(period).mean())
    if avg <= 0:
        return 1.0
    return float(volume.iloc[-1]) / avg


def _is_hammer(open_: float, high: float, low: float, close: float) -> bool:
    """Long lower wick, small body near top — bullish reversal off oversold."""
    rng = high - low
    if rng <= 0:
        return False
    body = abs(close - open_)
    lower_wick = min(open_, close) - low
    upper_wick = high - max(open_, close)
    return body / rng <= 0.35 and lower_wick / rng >= 0.5 and upper_wick / rng <= 0.20


def _is_shooting_star(open_: float, high: float, low: float, close: float) -> bool:
    """Long upper wick, small body near bottom — bearish reversal off overbought."""
    rng = high - low
    if rng <= 0:
        return False
    body = abs(close - open_)
    upper_wick = high - max(open_, close)
    lower_wick = min(open_, close) - low
    return body / rng <= 0.35 and upper_wick / rng >= 0.5 and lower_wick / rng <= 0.20


def _has_rsi_divergence(close: pd.Series, rsi: pd.Series, direction: Direction) -> bool:
    """Cheap divergence check on the trailing ``DIVERGENCE_LOOKBACK`` window.

    LONG (oversold): price made a new low but the latest RSI is *higher*
    than it was at the prior local low → bullish divergence.
    SHORT (overbought): price made a new high but the latest RSI is *lower*
    than it was at the prior local high → bearish divergence.
    """
    if len(close) < DIVERGENCE_LOOKBACK + 2 or len(rsi) < DIVERGENCE_LOOKBACK + 2:
        return False
    win_close = close.tail(DIVERGENCE_LOOKBACK)
    win_rsi = rsi.tail(DIVERGENCE_LOOKBACK)
    last_close = float(win_close.iloc[-1])
    last_rsi = float(win_rsi.iloc[-1])
    if pd.isna(last_rsi):
        return False
    prior_close = win_close.iloc[:-1]
    prior_rsi = win_rsi.iloc[:-1]
    if direction is Direction.LONG:
        prior_min_idx = prior_close.idxmin()
        if last_close >= float(prior_close.loc[prior_min_idx]):
            return False  # not a new low
        prior_min_rsi = float(prior_rsi.loc[prior_min_idx])
        return last_rsi > prior_min_rsi
    prior_max_idx = prior_close.idxmax()
    if last_close <= float(prior_close.loc[prior_max_idx]):
        return False  # not a new high
    prior_max_rsi = float(prior_rsi.loc[prior_max_idx])
    return last_rsi < prior_max_rsi


def _spy_realized_vol_proxy(spy_close: pd.Series) -> float:
    """Annualized realized vol of SPY's daily log returns over 20 trading days,
    scaled to a comparable VIX-units number (fractional → percent)."""
    if len(spy_close) < 21:
        return float("nan")
    log_ret = np.log(spy_close).diff().tail(20)
    return float(log_ret.std(ddof=0) * np.sqrt(252) * 100.0)


# ────────────────────────────────────────────────────────────────────────────
# Score components
# ────────────────────────────────────────────────────────────────────────────


def _score_statistical_extremity(z: float, bb_breach_days: int, rsi: float) -> float:
    score = 0.0
    if abs(z) >= Z_SCORE_THRESHOLD:
        score += 20.0
    if bb_breach_days >= 2:
        score += 15.0
    if rsi < RSI_OVERSOLD or rsi > RSI_OVERBOUGHT:
        score += 10.0
    return score


def _score_exhaustion(volume_ratio: float, has_reversal: bool, has_divergence: bool) -> float:
    score = 0.0
    if volume_ratio >= 2.0:
        score += 15.0
    if has_reversal:
        score += 10.0
    if has_divergence:
        score += 5.0
    return score


def _score_market_context(
    *, z_score: float, spy_z_score: float, vix_value: float, direction: Direction
) -> float:
    score = 0.0
    # Sector-ish reversion proxy: stock z-score and SPY z-score share the
    # same sign — i.e. the broader market is stretched the same way the
    # candidate is, so the fade has tailwind from a market-wide reset.
    if not np.isnan(spy_z_score) and not np.isnan(z_score):
        if (z_score < 0 and spy_z_score < 0) or (z_score > 0 and spy_z_score > 0):
            score += 15.0
    # VIX context — relevant gate depends on direction.
    if not np.isnan(vix_value):
        if direction is Direction.LONG and vix_value > VIX_OVERSOLD_THRESHOLD:
            score += 10.0
        elif direction is Direction.SHORT and vix_value < VIX_OVERBOUGHT_THRESHOLD:
            score += 10.0
    return score


# ────────────────────────────────────────────────────────────────────────────
# Plug
# ────────────────────────────────────────────────────────────────────────────


class ReversionSetupDetection(BaseEnginePlug):
    """Plug 1 of Reversion — universe scan + scoring."""

    engine_name = "reversion"

    def __init__(
        self,
        data: DataProviderInterface,
        universe: tuple[str, ...] = REVERSION_TEST_UNIVERSE,
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
            "details": {
                "universe_size": len(self._universe),
                "lookback_days": LOOKBACK_DAYS,
                "vix_thresholds": [VIX_OVERSOLD_THRESHOLD, VIX_OVERBOUGHT_THRESHOLD],
            },
        }

    async def scan(self, as_of: date) -> list[SetupCandidate]:
        """Return all qualifying SetupCandidates for ``as_of``, sorted by score."""
        start = as_of - timedelta(days=LOOKBACK_DAYS + 30)
        spy_z, vix = await self._market_context(start, as_of)

        candidates: list[SetupCandidate] = []
        for symbol in self._universe:
            bars = await self._data.get_daily_bars(symbol, start, as_of)
            try:
                cand = self._evaluate(symbol, as_of, bars, spy_z=spy_z, vix=vix)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("reversion.setup.evaluate_failed", symbol=symbol, error=str(exc))
                continue
            if cand is not None:
                candidates.append(cand)
        candidates.sort(key=lambda c: c.fade_score, reverse=True)
        return candidates

    async def _market_context(self, start: date, as_of: date) -> tuple[float, float]:
        """Compute SPY z-score and the VIX value (or its realized-vol proxy)."""
        try:
            spy_bars = await self._data.get_daily_bars(SPY_SYMBOL, start, as_of)
        except Exception as exc:
            logger.warning("reversion.setup.spy_fetch_failed", error=str(exc))
            return float("nan"), float("nan")
        spy_df = _bars_to_frame(spy_bars)
        if len(spy_df) < ZSCORE_PERIOD + 5:
            return float("nan"), float("nan")
        spy_z = float(_compute_zscore(spy_df["close"]).iloc[-1])

        # Try real VIX first, fall back to SPY realized-vol proxy.
        try:
            vix_bars = await self._data.get_daily_bars(VIX_PROXY_SYMBOL, start, as_of)
            vix_df = _bars_to_frame(vix_bars)
            if len(vix_df) > 0:
                vix = float(vix_df["close"].iloc[-1])
                logger.debug("reversion.setup.vix_real", value=vix)
                return spy_z, vix
        except Exception as exc:
            logger.debug("reversion.setup.vix_fetch_failed", error=str(exc))
        vix_proxy = _spy_realized_vol_proxy(spy_df["close"])
        logger.debug("reversion.setup.vix_proxy", value=vix_proxy)
        return spy_z, vix_proxy

    def _evaluate(
        self,
        symbol: str,
        as_of: date,
        bars: list[Bar],
        *,
        spy_z: float,
        vix: float,
    ) -> SetupCandidate | None:
        df = _bars_to_frame(bars)
        if len(df) < MA_50_PERIOD + 5:
            return None

        last_close = float(df["close"].iloc[-1])
        avg_vol_20 = float(df["volume"].tail(ZSCORE_PERIOD).mean())
        if last_close < float(MIN_PRICE) or avg_vol_20 < MIN_AVG_VOLUME:
            return None

        adx_series = _compute_adx(df)
        adx_now = float(adx_series.iloc[-1])
        if np.isnan(adx_now) or adx_now > MAX_ADX_FOR_REVERSION:
            return None  # plan §4.2 — engine disabled in trending markets.

        z_series = _compute_zscore(df["close"])
        z_now = float(z_series.iloc[-1])
        if np.isnan(z_now) or abs(z_now) < Z_SCORE_THRESHOLD:
            return None  # need a real statistical extreme to fade.
        direction = Direction.LONG if z_now < 0 else Direction.SHORT

        rsi_now = float(_compute_rsi(df["close"]).iloc[-1])
        sma, upper, lower = _compute_bbands(df)
        bb_breach = _bb_breach_consecutive(df["close"], upper, lower)
        vol_ratio = _volume_ratio(df["volume"])

        last = df.iloc[-1]
        reversal = (
            _is_hammer(float(last["open"]), float(last["high"]), float(last["low"]), last_close)
            if direction is Direction.LONG
            else _is_shooting_star(
                float(last["open"]), float(last["high"]), float(last["low"]), last_close
            )
        )
        rsi_series = _compute_rsi(df["close"])
        divergence = _has_rsi_divergence(df["close"], rsi_series, direction)

        ma_50 = float(df["close"].rolling(MA_50_PERIOD, min_periods=MA_50_PERIOD).mean().iloc[-1])
        if np.isnan(ma_50):
            return None
        ma_20 = float(sma.iloc[-1])

        se = _score_statistical_extremity(z_now, bb_breach, rsi_now)
        ec = _score_exhaustion(vol_ratio, reversal, divergence)
        mc = _score_market_context(z_score=z_now, spy_z_score=spy_z, vix_value=vix, direction=direction)
        score = se + ec + mc
        if score < SCORE_WEAK:
            return None

        return SetupCandidate(
            ticker=symbol,
            as_of=as_of,
            direction=direction,
            fade_score=round(score, 2),
            statistical_extremity=round(se, 2),
            exhaustion_confirmation=round(ec, 2),
            market_context=round(mc, 2),
            z_score=round(z_now, 4),
            rsi_14=round(rsi_now, 2),
            bb_breach_consecutive_days=bb_breach,
            volume_ratio=round(vol_ratio, 3),
            adx_14=round(adx_now, 2),
            has_reversal_candle=reversal,
            has_rsi_divergence=divergence,
            suggested_entry_price=Decimal(str(round(last_close, 2))),
            target_20ma=Decimal(str(round(ma_20, 2))),
            target_50ma=Decimal(str(round(ma_50, 2))),
            notes=f"z={z_now:+.2f} rsi={rsi_now:.1f} bb_breach={bb_breach} vol_x={vol_ratio:.2f}",
        )
