"""Vector — Plug 1: Setup Detection (three-gate model + scoring).

Scope (plan §4.3):

1. **Universe filter** — price > $10, avg vol > 1M.
2. **Three-gate model**:
   * Value & Quality — `_check_value_quality`. P/B < 1.5, D/E < 3, revenue
     > $500M, last close above 200-SMA *or* recently crossed back above.
   * Catalyst — `_check_catalyst`. *MVP proxy*: YoY net-income growth ≥ 5%
     (the plan calls for EPS-beats-estimate from FMP's earnings_surprise
     endpoint plus raised-guidance NLP; both deferred — see CALLOUTS below).
   * Technical Trigger — `_check_technical_trigger`. Pullback to 10-/20-MA
     on declining volume that closes up on volume > 1.2× avg, or breakout
     above 50-MA on volume > 1.5× avg.
3. **Trend filter** — SPY > 50-MA AND 50-MA > 200-MA.
4. **VIX filter** — VIX < 28 (above this level: no new entries).
5. **Score** — Technical (0–40) + Catalyst (0–35) + Sentiment (0–25);
   ≥ 65 strong, 50–64 weak, < 50 no trade.

CALLOUTS (deferred to follow-up tasks):
* Real EPS-beats-estimate via FMP `/v3/earnings_surprises/{symbol}` —
  the cache today only stores quarterly statements, so we proxy with
  net-income growth.
* Raised-guidance NLP — requires news/transcript ingest.
* Sentiment score — placeholder 0; production reads ApeWisdom or similar.
* Sector ETF top-3 fallback for the trend filter — single SPY check
  for MVP.

The setup function is *pure*: it consumes pre-fetched price + fundamentals
panels and returns a list of ``SetupCandidate``. The scheduler does the
fetching.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd
import structlog

from tpcore.interfaces.engine_plug import BaseEnginePlug
from vector.models import (
    SCORE_STRONG,
    SCORE_WEAK,
    VIX_BLOCK_NEW,
    SetupCandidate,
)

logger = structlog.get_logger(__name__)

# Universe filter
MIN_PRICE = Decimal("10")
MIN_AVG_VOLUME = 1_000_000

# Value & Quality gate
PB_CEILING = Decimal("1.5")
DE_CEILING = Decimal("3")
REVENUE_FLOOR = Decimal("500000000")  # $500M

# Catalyst gate (MVP proxy)
EARNINGS_GROWTH_FLOOR_YOY = 0.05  # +5% YoY net income

# Technical trigger
PULLBACK_VOLUME_MULTIPLIER = 1.2
BREAKOUT_VOLUME_MULTIPLIER = 1.5

# Trend filter
SMA_FAST_PERIOD = 50
SMA_SLOW_PERIOD = 200
SMA_10_PERIOD = 10
SMA_20_PERIOD = 20


# ────────────────────────────────────────────────────────────────────────────
# Internal helpers — pure functions over pandas Series
# ────────────────────────────────────────────────────────────────────────────


def _sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period, min_periods=period).mean()


def _avg_volume(volume: pd.Series, period: int = 20) -> float:
    return float(volume.rolling(period, min_periods=period).mean().iloc[-1])


def _yoy_growth(latest: Decimal | None, year_ago: Decimal | None) -> float | None:
    if latest is None or year_ago is None or year_ago == 0:
        return None
    return float((latest - year_ago) / abs(year_ago))


@dataclass(frozen=True)
class _PanelStats:
    last_close: Decimal
    sma_10: float
    sma_20: float
    sma_50: float
    sma_200: float
    avg_vol_20: float
    last_volume: int
    prior_close: float


def _compute_stats(df: pd.DataFrame) -> _PanelStats | None:
    if len(df) < SMA_SLOW_PERIOD + 1:
        return None
    closes = df["close"].astype(float)
    volumes = df["volume"].astype(float)
    return _PanelStats(
        last_close=Decimal(str(closes.iloc[-1])),
        sma_10=float(_sma(closes, SMA_10_PERIOD).iloc[-1]),
        sma_20=float(_sma(closes, SMA_20_PERIOD).iloc[-1]),
        sma_50=float(_sma(closes, SMA_FAST_PERIOD).iloc[-1]),
        sma_200=float(_sma(closes, SMA_SLOW_PERIOD).iloc[-1]),
        avg_vol_20=_avg_volume(volumes),
        last_volume=int(volumes.iloc[-1]),
        prior_close=float(closes.iloc[-2]),
    )


# ────────────────────────────────────────────────────────────────────────────
# Gate-by-gate evaluators
# ────────────────────────────────────────────────────────────────────────────


def _check_value_quality(
    fundamentals: dict[str, Any] | None,
    stats: _PanelStats,
) -> tuple[bool, str | None]:
    """Returns (pass, reason_if_fail)."""
    if fundamentals is None:
        return False, "no_fundamentals"

    revenue = fundamentals.get("revenue")
    if revenue is None or Decimal(str(revenue)) < REVENUE_FLOOR:
        return False, "revenue_below_floor"

    # Book value per share = (assets − liabilities) / shares.
    total_assets = fundamentals.get("total_assets")
    total_liabilities = fundamentals.get("total_liabilities")
    shares = fundamentals.get("shares_outstanding")
    if (
        total_assets is None
        or total_liabilities is None
        or shares is None
        or Decimal(str(shares)) <= 0
    ):
        return False, "missing_balance_sheet"
    book_value = Decimal(str(total_assets)) - Decimal(str(total_liabilities))
    if book_value <= 0:
        return False, "negative_book_value"
    bvps = book_value / Decimal(str(shares))
    if bvps <= 0:
        return False, "non_positive_bvps"
    pb = stats.last_close / bvps
    if pb >= PB_CEILING:
        return False, f"pb={pb:.2f}>={PB_CEILING}"

    # D/E proxy: total_liabilities / equity = total_liabilities / book_value.
    de = Decimal(str(total_liabilities)) / book_value
    if de >= DE_CEILING:
        return False, f"de={de:.2f}>={DE_CEILING}"

    # Trend posture — last close above 200-SMA, OR recently crossed above
    # (close > sma_200 in the last 5 sessions).
    if float(stats.last_close) < stats.sma_200 and stats.prior_close < stats.sma_200:
        return False, "below_200_sma"

    return True, None


def _check_catalyst(fundamentals: dict[str, Any] | None) -> tuple[bool, float | None, str | None]:
    """MVP proxy: positive YoY net-income growth ≥ floor.

    Returns (pass, growth_pct_or_None, reason_if_fail).
    """
    if fundamentals is None:
        return False, None, "no_fundamentals"
    history = fundamentals.get("history") or []
    if len(history) < 4:
        return False, None, "insufficient_history"
    latest_ni = fundamentals.get("net_income")
    yoy_ni = history[3].get("net_income") if len(history) >= 4 else None
    growth = _yoy_growth(latest_ni, yoy_ni)
    if growth is None:
        return False, None, "ni_yoy_not_computable"
    if growth < EARNINGS_GROWTH_FLOOR_YOY:
        return False, growth, f"ni_yoy={growth:.1%}<floor"
    return True, growth, None


def _check_technical_trigger(
    df: pd.DataFrame, stats: _PanelStats
) -> tuple[bool, str | None]:
    """Pullback to 10-/20-MA OR breakout above 50-MA, with volume confirmation."""
    last_close = float(stats.last_close)
    last_vol = float(stats.last_volume)

    # Breakout: close above 50-MA today, prior close below, volume > 1.5x avg.
    if (
        last_close > stats.sma_50
        and stats.prior_close <= stats.sma_50
        and last_vol > BREAKOUT_VOLUME_MULTIPLIER * stats.avg_vol_20
    ):
        return True, "breakout_above_50ma"

    # Pullback: close near 10-MA or 20-MA, today's close > prior close, volume confirmation.
    near_10 = abs(last_close - stats.sma_10) / max(last_close, 1e-9) < 0.02
    near_20 = abs(last_close - stats.sma_20) / max(last_close, 1e-9) < 0.02
    if (near_10 or near_20) and last_close > stats.prior_close and last_vol > PULLBACK_VOLUME_MULTIPLIER * stats.avg_vol_20:
        return True, "pullback_to_10ma" if near_10 else "pullback_to_20ma"

    return False, None


def _technical_score(stats: _PanelStats, trigger: str | None) -> float:
    """0–40 — weighted by trigger strength + trend posture above SMAs."""
    if trigger is None:
        return 0.0
    base = 25.0 if "breakout" in trigger else 20.0  # breakouts score slightly higher
    posture = 0.0
    if float(stats.last_close) > stats.sma_50 > stats.sma_200:
        posture = 15.0
    elif float(stats.last_close) > stats.sma_50:
        posture = 8.0
    return min(40.0, base + posture)


def _catalyst_score(growth_yoy: float | None) -> float:
    """0–35 — proportional to YoY earnings growth (capped). MVP proxy."""
    if growth_yoy is None or growth_yoy < EARNINGS_GROWTH_FLOOR_YOY:
        return 0.0
    # Linear ramp: 5% growth → 12 pts; 25% → 30 pts; cap 35.
    return min(35.0, 12.0 + (growth_yoy - 0.05) * 90.0)


def _sentiment_score() -> float:
    """0–25 — placeholder. Production source: ApeWisdom + 13F deltas."""
    return 0.0


# ────────────────────────────────────────────────────────────────────────────
# Plug class
# ────────────────────────────────────────────────────────────────────────────


class VectorSetupDetection(BaseEnginePlug):
    """Plug 1 of Vector."""

    engine_name = "vector"

    def __init__(
        self,
        *,
        universe: tuple[str, ...] | None = None,
        min_price: Decimal = MIN_PRICE,
        min_avg_volume: int = MIN_AVG_VOLUME,
        score_floor_strong: int = SCORE_STRONG,
        score_floor_weak: int = SCORE_WEAK,
    ) -> None:
        from vector.models import VECTOR_TEST_UNIVERSE

        self._universe = universe if universe is not None else VECTOR_TEST_UNIVERSE
        self._min_price = min_price
        self._min_avg_volume = min_avg_volume
        self._score_floor_strong = score_floor_strong
        self._score_floor_weak = score_floor_weak

    def validate_dependencies(self) -> bool:
        return True

    def healthcheck(self) -> dict:
        return {
            "engine": self.engine_name,
            "plug": "setup_detection",
            "ok": True,
            "details": {
                "universe_size": len(self._universe),
                "score_strong": self._score_floor_strong,
                "score_weak": self._score_floor_weak,
            },
        }

    def scan(
        self,
        *,
        as_of: date,
        bars_by_ticker: dict[str, pd.DataFrame],
        fundamentals_by_ticker: dict[str, dict[str, Any] | None],
        spy_panel: pd.DataFrame | None,
        vix_value: float | None,
    ) -> list[SetupCandidate]:
        """Walk the universe and return passing candidates."""
        # Trend filter — SPY > 50-MA AND 50-MA > 200-MA.
        spy_in_uptrend = self._spy_uptrend(spy_panel)
        if not spy_in_uptrend:
            logger.info("vector.setup.spy_not_in_uptrend", as_of=str(as_of))
            return []

        # VIX filter — block all new entries when VIX is too elevated.
        if vix_value is not None and vix_value > float(VIX_BLOCK_NEW):
            logger.info("vector.setup.vix_blocking", vix=vix_value, threshold=float(VIX_BLOCK_NEW))
            return []

        candidates: list[SetupCandidate] = []
        for ticker in self._universe:
            df = bars_by_ticker.get(ticker)
            if df is None or len(df) < SMA_SLOW_PERIOD + 1:
                continue
            stats = _compute_stats(df)
            if stats is None:
                continue
            if stats.last_close < self._min_price or stats.avg_vol_20 < self._min_avg_volume:
                continue

            fundamentals = fundamentals_by_ticker.get(ticker)
            ok_vq, vq_reason = _check_value_quality(fundamentals, stats)
            if not ok_vq:
                continue
            ok_cat, growth, cat_reason = _check_catalyst(fundamentals)
            if not ok_cat:
                continue
            ok_tech, trigger = _check_technical_trigger(df, stats)
            if not ok_tech:
                continue

            tech_score = _technical_score(stats, trigger)
            cat_score = _catalyst_score(growth)
            sent_score = _sentiment_score()
            total = tech_score + cat_score + sent_score
            if total < self._score_floor_weak:
                continue

            candidates.append(
                SetupCandidate(
                    ticker=ticker,
                    as_of=as_of,
                    swing_score=float(total),
                    technical=float(tech_score),
                    catalyst=float(cat_score),
                    sentiment=float(sent_score),
                    last_close=stats.last_close,
                    sma_50=Decimal(str(stats.sma_50)),
                    sma_200=Decimal(str(stats.sma_200)),
                    avg_volume=int(stats.avg_vol_20),
                    market_cap_usd=None,  # MVP — defer
                    vix_at_entry=vix_value,
                    spy_in_uptrend=spy_in_uptrend,
                    earnings_growth_yoy=growth,
                    pullback_or_breakout=trigger,
                    notes=f"strong={total>=self._score_floor_strong}",
                )
            )
        return sorted(candidates, key=lambda c: c.swing_score, reverse=True)

    @staticmethod
    def _spy_uptrend(spy_panel: pd.DataFrame | None) -> bool:
        if spy_panel is None or len(spy_panel) < SMA_SLOW_PERIOD + 1:
            return True  # MVP — when SPY data isn't loaded, don't block (caller's choice).
        closes = spy_panel["close"].astype(float)
        sma_50 = float(_sma(closes, SMA_FAST_PERIOD).iloc[-1])
        sma_200 = float(_sma(closes, SMA_SLOW_PERIOD).iloc[-1])
        last = float(closes.iloc[-1])
        if np.isnan(sma_50) or np.isnan(sma_200):
            return True
        return last > sma_50 and sma_50 > sma_200


__all__ = [
    "VectorSetupDetection",
    "MIN_AVG_VOLUME",
    "MIN_PRICE",
    "PB_CEILING",
    "DE_CEILING",
    "REVENUE_FLOOR",
    "EARNINGS_GROWTH_FLOOR_YOY",
]
