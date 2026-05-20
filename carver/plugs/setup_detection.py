"""Carver — Plug 1: Setup Detection (three forecasts + FDM combine).

For each instrument in the panel universe at ``as_of``, compute three
orthogonal forecasts (EWMAC trend / 12m value proxy / 20d Bollinger Z),
scale each so the rolling 24-month abs-mean is approximately
``FORECAST_TARGET_ABS=10``, cap each at +/- ``FORECAST_CAP_ABS=20``,
then equal-weight combine and multiply by the Forecast Diversification
Multiplier (FDM, bounded [IDM_FLOOR, IDM_CAP]).

Output: ``(list[CarverAssessment], FilterDiagnostics)``. The scheduler
lifts the diagnostics onto every SIGNAL event (compliance grep #2).

Pure math — no DB calls. The scheduler/backtest are responsible for
preparing the per-ticker close-price ``pandas.DataFrame`` panel.

See ``docs/superpowers/specs/2026-05-20-carver-design.md`` Section 4.1.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import numpy as np
import pandas as pd
import structlog

from carver.models import (
    FORECAST_CAP_ABS,
    FORECAST_TARGET_ABS,
    IDM_CAP,
    IDM_FLOOR,
    CarverAssessment,
    CarverForecast,
)
from tpcore.backtest.filter_diagnostics import FilterDiagnostics
from tpcore.interfaces.engine_plug import BaseEnginePlug

logger = structlog.get_logger(__name__)

# Window constants used by the per-rule computations.
_VOL_WINDOW_DAYS = 504  # 24 months of trading days, approx
_MIN_HISTORY_DAYS = 100  # need at least this many bars to compute trend + meanrev


# ── Per-rule raw forecasts (pure functions) ─────────────────────────────


def _ewmac_forecast(prices: pd.Series, fast: int, slow: int, vol_24m: float) -> float:
    """Carver EWMAC: (EWMA_fast - EWMA_slow) / sigma_24m. Returns the raw forecast."""
    if vol_24m <= 0 or len(prices) < slow + 50:
        return float("nan")
    f = prices.ewm(span=fast, adjust=False).mean().iloc[-1]
    s = prices.ewm(span=slow, adjust=False).mean().iloc[-1]
    return float((f - s) / vol_24m)


def _value_proxy_forecast(prices: pd.Series, lookback_months: int) -> float:
    """Equity-substrate value proxy: -(12-1 month total return) / sigma_24m so that
    'cheap relative to its own 12-month mean' is a POSITIVE forecast."""
    lookback_days = lookback_months * 21
    if len(prices) < lookback_days + 21:
        return float("nan")
    p_now = float(prices.iloc[-21])  # skip last month
    p_then = float(prices.iloc[-lookback_days - 21])
    if p_then <= 0:
        return float("nan")
    raw_return = (p_now / p_then) - 1.0
    vol_24m = float(prices.pct_change().tail(_VOL_WINDOW_DAYS).std(ddof=1))
    if vol_24m <= 0 or np.isnan(vol_24m):
        return float("nan")
    return float(-raw_return / vol_24m)  # sign-flipped: cheap -> positive


def _bollinger_z_forecast(prices: pd.Series, window: int) -> float:
    """Mean-reversion Z-score: (EMA_window - close) / sigma_window.
    Positive when price below its EMA (cheap)."""
    if len(prices) < window + 1:
        return float("nan")
    ema = float(prices.ewm(span=window, adjust=False).mean().iloc[-1])
    sig = float(prices.tail(window).std(ddof=1))
    if sig <= 0 or np.isnan(sig):
        return float("nan")
    return float((ema - float(prices.iloc[-1])) / sig)


def _cap(value: float) -> float:
    if np.isnan(value):
        return float("nan")
    return float(max(-FORECAST_CAP_ABS, min(FORECAST_CAP_ABS, value)))


def _compute_idm(scaled_forecasts: list[float], correlation_matrix: np.ndarray | None) -> Decimal:
    """Forecast Diversification Multiplier (Carver chapter 8).

    FDM = sqrt(N) / sqrt(1^T rho 1 / N^2), bounded [IDM_FLOOR, IDM_CAP].
    Falls back to IDM_FLOOR on cold-start (correlation_matrix is None).
    """
    if correlation_matrix is None:
        return IDM_FLOOR
    n = len(scaled_forecasts)
    if n == 0:
        return IDM_FLOOR
    ones = np.ones(n)
    quad = float(ones @ correlation_matrix @ ones) / (n * n)
    if quad <= 0:
        return IDM_CAP  # uncorrelated/anti-correlated → diversification ceiling
    fdm = float(np.sqrt(n) / np.sqrt(quad))
    fdm_dec = Decimal(str(fdm))
    if fdm_dec < IDM_FLOOR:
        return IDM_FLOOR
    if fdm_dec > IDM_CAP:
        return IDM_CAP
    return fdm_dec


# ── The plug ────────────────────────────────────────────────────────────


class CarverSetupDetection(BaseEnginePlug):
    """Plug 1 of Carver — three-forecast scan + FDM combine.

    Returns ``(candidates, FilterDiagnostics)``. The scheduler lifts the
    diagnostics onto every ``db_log.signal(..., extra_data=...)`` event.
    """

    engine_name = "carver"

    DEFAULT_TREND_SCALING_CONST: float = 10.0   # EWMAC (8, 32)
    DEFAULT_VALUE_SCALING_CONST: float = 10.0   # 12-1 value proxy
    DEFAULT_MEANREV_SCALING_CONST: float = 8.0  # 20d Bollinger Z

    def __init__(
        self,
        *,
        trend_fast: int = 8,
        trend_slow: int = 32,
        value_lookback_months: int = 12,
        meanrev_window: int = 20,
        idm_cap: Decimal = IDM_CAP,
        trend_scaling_const: float | None = None,
        value_scaling_const: float | None = None,
        meanrev_scaling_const: float | None = None,
    ) -> None:
        self._trend_fast = trend_fast
        self._trend_slow = trend_slow
        self._value_lookback_months = value_lookback_months
        self._meanrev_window = meanrev_window
        self._idm_cap = idm_cap
        self._trend_scaling_const = (
            trend_scaling_const
            if trend_scaling_const is not None
            else self.DEFAULT_TREND_SCALING_CONST
        )
        self._value_scaling_const = (
            value_scaling_const
            if value_scaling_const is not None
            else self.DEFAULT_VALUE_SCALING_CONST
        )
        self._meanrev_scaling_const = (
            meanrev_scaling_const
            if meanrev_scaling_const is not None
            else self.DEFAULT_MEANREV_SCALING_CONST
        )

    def validate_dependencies(self) -> bool:
        # Pure-math plug — no external dependency to verify.
        return True

    def healthcheck(self) -> dict:
        return {
            "engine": self.engine_name,
            "plug": "setup_detection",
            "ok": True,
            "details": {
                "trend_fast": self._trend_fast,
                "trend_slow": self._trend_slow,
                "value_lookback_months": self._value_lookback_months,
                "meanrev_window": self._meanrev_window,
                "idm_cap": str(self._idm_cap),
            },
        }

    def detect(
        self,
        panels: dict[str, pd.DataFrame],
        as_of: date,
    ) -> tuple[list[CarverAssessment], FilterDiagnostics]:
        """Scan ``panels`` (one closing-price column per ticker) and return
        the candidate set + a populated FilterDiagnostics. Pure: no DB."""
        diag = FilterDiagnostics(universe_total=len(panels))
        del as_of  # not used in pure-math scan; carried for symmetry/logging

        # First pass: per-ticker raw + scaled + capped per-rule forecasts.
        per_ticker_rows: list[
            tuple[str, list[CarverForecast], float, float, Decimal]
        ] = []  # (ticker, forecasts, instrument_daily_vol, last_price, _)
        for ticker, panel in panels.items():
            if "close" not in panel.columns or len(panel) < _MIN_HISTORY_DAYS:
                diag.coarse_liquidity_blocked += 1
                continue
            prices = panel["close"]
            vol_24m = float(prices.pct_change().tail(_VOL_WINDOW_DAYS).std(ddof=1))
            if not np.isfinite(vol_24m) or vol_24m <= 0:
                diag.coarse_liquidity_blocked += 1
                continue
            raw_trend = _ewmac_forecast(prices, self._trend_fast, self._trend_slow, vol_24m)
            raw_value = _value_proxy_forecast(prices, self._value_lookback_months)
            raw_meanrev = _bollinger_z_forecast(prices, self._meanrev_window)
            if not (np.isfinite(raw_trend) and np.isfinite(raw_value) and np.isfinite(raw_meanrev)):
                diag.coarse_liquidity_blocked += 1
                continue
            scaled_trend = raw_trend * self._trend_scaling_const
            scaled_value = raw_value * self._value_scaling_const
            scaled_meanrev = raw_meanrev * self._meanrev_scaling_const
            forecasts = [
                CarverForecast(
                    rule="trend", raw=raw_trend, scaled=scaled_trend, capped=_cap(scaled_trend),
                ),
                CarverForecast(
                    rule="value", raw=raw_value, scaled=scaled_value, capped=_cap(scaled_value),
                ),
                CarverForecast(
                    rule="meanrev",
                    raw=raw_meanrev,
                    scaled=scaled_meanrev,
                    capped=_cap(scaled_meanrev),
                ),
            ]
            last_price = float(prices.iloc[-1])
            per_ticker_rows.append(
                (ticker, forecasts, vol_24m * last_price, last_price, Decimal("0"))
            )

        # Cross-sectional IDM: only computed if we have a full 24-month return
        # window for at least two tickers; otherwise IDM_FLOOR (cold-start).
        idm_value = self._compute_universe_idm(panels)

        # Second pass: combine per-ticker forecasts -> CarverAssessment.
        candidates: list[CarverAssessment] = []
        target = FORECAST_TARGET_ABS  # unused but pinned per spec for clarity
        del target
        for ticker, forecasts, instr_cash_vol, last_price, _ in per_ticker_rows:
            capped_values = [
                f.capped if f.capped is not None else 0.0 for f in forecasts
            ]
            combined = float(np.mean(capped_values)) * float(idm_value)
            instr_daily_vol_pct = (instr_cash_vol / last_price) if last_price > 0 else 0.0
            candidates.append(
                CarverAssessment(
                    ticker=ticker,
                    forecasts=forecasts,
                    idm=idm_value,
                    combined_forecast=combined,
                    instrument_daily_vol_pct=instr_daily_vol_pct,
                    instrument_price_usd=Decimal(str(last_price)),
                )
            )

        diag.candidates_passed = len(candidates)
        logger.info(
            "carver.setup.ranked",
            n_universe=len(panels),
            n_candidates=len(candidates),
            idm=str(idm_value),
        )
        return candidates, diag

    def _compute_universe_idm(self, panels: dict[str, pd.DataFrame]) -> Decimal:
        """Cross-sectional IDM from per-ticker return-series correlation.

        Requires at least two tickers with >=``_VOL_WINDOW_DAYS`` rows;
        otherwise falls back to IDM_FLOOR (cold-start)."""
        eligible: list[pd.Series] = []
        for panel in panels.values():
            if "close" not in panel.columns or len(panel) < _VOL_WINDOW_DAYS:
                continue
            returns = panel["close"].pct_change().tail(_VOL_WINDOW_DAYS)
            if returns.notna().sum() < _VOL_WINDOW_DAYS // 2:
                continue
            eligible.append(returns)
        if len(eligible) < 2:
            return IDM_FLOOR
        df = pd.concat(eligible, axis=1).dropna()
        if df.shape[0] < 30 or df.shape[1] < 2:
            return IDM_FLOOR
        rho = df.corr().to_numpy()
        if not np.all(np.isfinite(rho)):
            return IDM_FLOOR
        return _compute_idm([1.0] * df.shape[1], rho)


__all__ = [
    "CarverSetupDetection",
    "_bollinger_z_forecast",
    "_compute_idm",
    "_ewmac_forecast",
    "_value_proxy_forecast",
]
