"""Sentinel — Plug 1: Setup Detection (Bear Score computation).

Translates the latest macro indicator readings + a SPY-based VIX proxy
into the Bear Score that drives lifecycle transitions. Pure functions
where possible; one DB-facing entry point (:meth:`SentinelSetupDetection.
compute_for_date`) for callers that need everything in one shot.

The score is the sum of six contributors (max 85) rescaled to 0-100
(spec §4.6). The 0-100 score is what the activation gate consumes.

The Bear Score is *daily* — macro indicators that publish less often
(monthly Sahm Rule, weekly initial-claims, monthly industrial production)
are forward-filled to the most recent observation on or before the
as-of date. Forward-filling is the standard treatment for stale
macro readings; the alternative ("indicator unavailable → 0 points")
would over-weight publish-day jolts.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date as date_t
from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import pandas as pd
import structlog

from sentinel.models import (
    CREDIT_SPREAD_RECESSION_POINTS,
    CREDIT_SPREAD_RECESSION_THRESHOLD,
    CREDIT_SPREAD_WARNING_POINTS,
    CREDIT_SPREAD_WARNING_THRESHOLD,
    CREDIT_SPREAD_WATCH_POINTS,
    CREDIT_SPREAD_WATCH_THRESHOLD,
    INDUSTRIAL_PRODUCTION_HARD_POINTS,
    INDUSTRIAL_PRODUCTION_HARD_THRESHOLD,
    INDUSTRIAL_PRODUCTION_SOFT_HIGH,
    INDUSTRIAL_PRODUCTION_SOFT_LOW,
    INDUSTRIAL_PRODUCTION_SOFT_POINTS,
    INITIAL_CLAIMS_POINTS,
    INITIAL_CLAIMS_THRESHOLD,
    RAW_SCORE_MAX,
    SAHM_RULE_POINTS,
    SAHM_RULE_THRESHOLD,
    SCALED_SCORE_MAX,
    TRADING_DAYS_PER_YEAR,
    VIX_PROXY_HIGH_ONLY_POINTS,
    VIX_PROXY_HIGH_PLUS_MA_POINTS,
    VIX_PROXY_HIGH_THRESHOLD,
    VIX_PROXY_LOOKBACK_DAYS,
    YIELD_CURVE_BEAR_STEEPENER_POINTS,
    BearScoreBreakdown,
)
from tpcore.backtest.filter_diagnostics import FilterDiagnostics
from tpcore.data.repositories import MacroRepo
from tpcore.interfaces.engine_plug import BaseEnginePlug

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = structlog.get_logger(__name__)

# Indicators we consume from platform.macro_indicators.
_INDICATORS_NEEDED: tuple[str, ...] = (
    "sahm_rule",
    "industrial_production",
    "initial_claims",
    "yield_curve",
    "credit_spread",
)


# ─── Sub-scorers (pure functions) ───────────────────────────────────────


def score_sahm_rule(value: Decimal | None) -> int:
    """≥ 0.50 → 25 pts, else 0. Missing → 0."""
    if value is None:
        return 0
    return SAHM_RULE_POINTS if value >= SAHM_RULE_THRESHOLD else 0


def score_industrial_production(value: Decimal | None) -> int:
    """< 45 → 15 pts; 45–47 → 10 pts; else 0. Missing → 0.

    The threshold is the ISM-Manufacturing-style print where < 50 is
    contractionary; < 45 is hard recession territory.
    """
    if value is None:
        return 0
    if value < INDUSTRIAL_PRODUCTION_HARD_THRESHOLD:
        return INDUSTRIAL_PRODUCTION_HARD_POINTS
    if INDUSTRIAL_PRODUCTION_SOFT_LOW <= value < INDUSTRIAL_PRODUCTION_SOFT_HIGH:
        return INDUSTRIAL_PRODUCTION_SOFT_POINTS
    return 0


def score_initial_claims(
    latest: Decimal | None,
    previous: Decimal | None,
    two_back: Decimal | None,
) -> int:
    """> 260K AND rising 2 consecutive weeks → 10 pts. Else 0.

    "Rising 2 consecutive weeks" = ``latest > previous > two_back``.
    Returns 0 if any of the three inputs is missing.
    """
    if latest is None or previous is None or two_back is None:
        return 0
    if latest <= INITIAL_CLAIMS_THRESHOLD:
        return 0
    if latest > previous > two_back:
        return INITIAL_CLAIMS_POINTS
    return 0


def score_yield_curve(
    latest: Decimal | None,
    prior_value_at_inversion: Decimal | None,
) -> int:
    """Bear steepener detector — inverted AND re-steepening → 15 pts.

    "Inverted AND re-steepening" is interpreted as:
      * The curve has recently been inverted (``prior_value_at_inversion < 0``)
      * AND the current value is *less inverted* than the prior
        (``latest > prior_value_at_inversion``).

    The prior value comes from the LifecycleAnalysis cache of the
    minimum yield_curve reading over the prior 90 days. Returns 0 if
    either input is missing or the curve never inverted in the lookback.
    """
    if latest is None or prior_value_at_inversion is None:
        return 0
    if prior_value_at_inversion >= 0:
        return 0  # never inverted recently → bear steepener N/A
    # Re-steepening: today is closer to / above zero than the prior trough.
    if latest > prior_value_at_inversion:
        return YIELD_CURVE_BEAR_STEEPENER_POINTS
    return 0


def score_credit_spread(latest: Decimal | None, prior: Decimal | None) -> int:
    """Graduated credit-stress scorer for Moody's Baa - 10Y Treasury.

    Values are in *percent* (FRED ``BAA10Y``). Historical anchors: GFC
    peak ~6% (600 bp), COVID peak ~4.9%, calm periods 200-250 bp.

    Tiers (preserves the 5-pt budget from the prior HY OAS scorer so
    ``RAW_SCORE_MAX`` is unchanged):

    * **Recession** — ``latest > 5.00%`` (>500 bp): 5 pts. Fires at this
      level regardless of direction — sustained ≥500 bp credit stress
      is bad whether widening or stable.
    * **Warning** — ``latest > 4.00%`` AND widening (``latest > prior``):
      3 pts. Direction filter avoids paying for a tightening spread
      that's still elevated but recovering.
    * **Watch** — ``latest > 3.00%`` AND widening: 2 pts.
    * Otherwise (below 3% OR tightening at <5% level): 0 pts.

    Missing ``latest`` → 0. Missing ``prior`` is treated as "no direction
    info" — the Recession tier still fires on level alone; Watch/Warning
    require a non-None ``prior``.
    """
    if latest is None:
        return 0
    # Recession tier — level alone, no direction filter.
    if latest > CREDIT_SPREAD_RECESSION_THRESHOLD:
        return CREDIT_SPREAD_RECESSION_POINTS
    if prior is None:
        return 0
    # Watch / Warning tiers require widening.
    if latest <= prior:
        return 0
    if latest > CREDIT_SPREAD_WARNING_THRESHOLD:
        return CREDIT_SPREAD_WARNING_POINTS
    if latest > CREDIT_SPREAD_WATCH_THRESHOLD:
        return CREDIT_SPREAD_WATCH_POINTS
    return 0


def score_vix_proxy(vix_now: Decimal | None, vix_200d_ma: Decimal | None) -> int:
    """> 25 AND above 200-day MA → 15 pts; > 25 alone → 10 pts. Missing → 0."""
    if vix_now is None:
        return 0
    if vix_now <= VIX_PROXY_HIGH_THRESHOLD:
        return 0
    if vix_200d_ma is not None and vix_now > vix_200d_ma:
        return VIX_PROXY_HIGH_PLUS_MA_POINTS
    return VIX_PROXY_HIGH_ONLY_POINTS


def scale_raw_to_100(raw: int) -> int:
    """Linearly rescale raw 0-85 score to 0-100, rounded to int."""
    if raw <= 0:
        return 0
    if raw >= RAW_SCORE_MAX:
        return SCALED_SCORE_MAX
    return int(round(raw * SCALED_SCORE_MAX / RAW_SCORE_MAX))


# ─── VIX proxy — SPY 20-day realized vol annualized ─────────────────────


def compute_vix_proxy_series(spy_close: pd.Series) -> pd.Series:
    """Annualized 20-day realized vol of SPY log returns, in percent.

    Returns a Series indexed identically to ``spy_close`` (NaN for the
    first ``VIX_PROXY_LOOKBACK_DAYS`` rows). Caller is expected to align
    on the as-of date.
    """
    import numpy as np

    if len(spy_close) == 0:
        return spy_close.copy()
    log_returns = np.log(spy_close / spy_close.shift(1))
    rv = log_returns.rolling(VIX_PROXY_LOOKBACK_DAYS, min_periods=VIX_PROXY_LOOKBACK_DAYS).std()
    return rv * np.sqrt(TRADING_DAYS_PER_YEAR) * 100.0


def compute_spy_rally_pct(spy_close: pd.Series, window_end: date_t, window_days: int) -> Decimal:
    """Largest peak-to-trough rally in SPY closes within the trailing window.

    Used by the activation gate's "no counter-trend rally > 5%" rule.
    Computes max((close[t] - min(close[s..t])) / min(close[s..t])) for
    every t in the trailing window. Returns 0 if the window has fewer
    than 2 bars.
    """
    end_ts = pd.Timestamp(window_end)
    start_ts = end_ts - pd.Timedelta(days=window_days * 2)
    sub = spy_close.loc[(spy_close.index >= start_ts) & (spy_close.index <= end_ts)]
    sub = sub.tail(window_days + 1)
    if len(sub) < 2:
        return Decimal("0")
    running_min = sub.cummin()
    pct = sub / running_min - 1.0
    max_pct = float(pct.max())
    if max_pct <= 0:
        return Decimal("0")
    return Decimal(str(round(max_pct, 6)))


# ─── DB-backed pull + score for a date range ────────────────────────────


async def fetch_macro_indicator_panel(
    pool: asyncpg.Pool,
    *,
    start: date_t,
    end: date_t,
) -> pd.DataFrame:
    """Return a wide-format DataFrame indexed by date, columns by indicator.

    Forward-filled to daily — rows on non-publish dates carry the most
    recent observation. The DataFrame includes every NYSE trading day
    from ``start - 365 days`` to ``end`` so the activation gate can look
    back across weekends/holidays without missing observations.

    Backed by ``MacroRepo.get_window_batch`` against
    ``platform.macro_data`` (Task #18 P7 consolidation). Reads are
    FRED-sourced — sentinel's _INDICATORS_NEEDED are all FRED series.
    """
    repo = MacroRepo(pool)
    # Pad the start so forward-filling on monthly indicators has data.
    pad_start = start - timedelta(days=365)
    by_series = await repo.get_window_batch(
        _INDICATORS_NEEDED,
        pad_start,
        end,
        source="fred",
    )
    if not by_series:
        return pd.DataFrame(columns=list(_INDICATORS_NEEDED))
    df = pd.DataFrame(
        [
            {"date": obs.observed_date, "indicator": series_id, "value": float(obs.value_num)}
            for series_id, observations in by_series.items()
            for obs in observations
            if obs.value_num is not None
        ]
    )
    wide = df.pivot(index="date", columns="indicator", values="value").sort_index()
    for ind in _INDICATORS_NEEDED:
        if ind not in wide.columns:
            wide[ind] = float("nan")
    daily_idx = pd.date_range(pad_start, end, freq="D").date
    wide = wide.reindex(daily_idx).ffill()
    wide.index.name = "date"
    return wide[list(_INDICATORS_NEEDED)]


async def fetch_spy_close(
    pool: asyncpg.Pool,
    *,
    start: date_t,
    end: date_t,
) -> pd.Series:
    """SPY close prices indexed by date, sorted ascending."""
    sql = """
        SELECT date, close
        FROM platform.prices_daily
        WHERE ticker = 'SPY' AND date BETWEEN $1 AND $2
        ORDER BY date
    """
    # Pad start so the 200-day MA + 20-day VIX-proxy lookback have data.
    pad_start = start - timedelta(days=365)
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, pad_start, end)
    if not rows:
        return pd.Series(dtype=float, name="SPY")
    s = pd.Series(
        {pd.Timestamp(r["date"]): float(r["close"]) for r in rows},
        name="SPY",
    ).sort_index()
    return s


# ─── Headline class ─────────────────────────────────────────────────────


class SentinelSetupDetection(BaseEnginePlug):
    """Plug 1 — compute the Bear Score breakdown for a given date.

    Use :meth:`compute_for_range` to pre-compute a panel of breakdowns
    across the backtest window in one query, then walk by date for the
    activation gate. :meth:`compute_for_date` is a thin shim around it.
    """

    engine_name = "sentinel"

    def validate_dependencies(self) -> bool:
        return True

    def healthcheck(self) -> dict:
        return {
            "engine": self.engine_name,
            "plug": "setup_detection",
            "ok": True,
            "details": {},
        }

    async def compute_for_range(
        self,
        pool: asyncpg.Pool,
        *,
        start: date_t,
        end: date_t,
    ) -> dict[date_t, BearScoreBreakdown]:
        """Compute Bear Score breakdowns for every NYSE trading day in
        ``[start, end]``.

        Trading days are intersected with the dates that have SPY price
        data (VIX proxy depends on SPY) — that keeps the panel aligned
        with the price grid the backtest walks.
        """
        macro = await fetch_macro_indicator_panel(pool, start=start, end=end)
        spy = await fetch_spy_close(pool, start=start, end=end)
        return self._build_breakdowns(macro, spy, start=start, end=end)

    def _build_breakdowns(
        self,
        macro: pd.DataFrame,
        spy: pd.Series,
        *,
        start: date_t,
        end: date_t,
    ) -> dict[date_t, BearScoreBreakdown]:
        out: dict[date_t, BearScoreBreakdown] = {}
        if len(spy) == 0:
            return out
        vix_proxy_series = compute_vix_proxy_series(spy)
        vix_200d_ma_series = vix_proxy_series.rolling(200, min_periods=50).mean()

        spy_dates: Iterable[date_t] = [d.date() if hasattr(d, "date") else d for d in spy.index]
        for trade_date in spy_dates:
            if not (start <= trade_date <= end):
                continue
            breakdown = self._score_one_date(trade_date, macro, vix_proxy_series, vix_200d_ma_series)
            out[trade_date] = breakdown
        return out

    def _score_one_date(
        self,
        as_of: date_t,
        macro: pd.DataFrame,
        vix_proxy: pd.Series,
        vix_200d_ma: pd.Series,
    ) -> BearScoreBreakdown:
        missing: list[str] = []

        def _get(indicator: str, *, days_back: int = 0) -> Decimal | None:
            try:
                rows = macro.loc[macro.index <= as_of, indicator]
            except KeyError:
                missing.append(indicator)
                return None
            if days_back > 0:
                rows = rows.iloc[:-days_back] if len(rows) > days_back else pd.Series(dtype=float)
            if len(rows) == 0 or pd.isna(rows.iloc[-1]):
                missing.append(indicator)
                return None
            return Decimal(str(rows.iloc[-1]))

        sahm_v = _get("sahm_rule")
        ip_v = _get("industrial_production")
        ic_now = _get("initial_claims")
        # initial_claims publishes weekly; "rising 2 consecutive weeks" → step back 7 + 14 days.
        ic_prev = _get_lagged(macro, "initial_claims", as_of=as_of, days=7, missing=missing)
        ic_two = _get_lagged(macro, "initial_claims", as_of=as_of, days=14, missing=missing)

        yc_latest = _get("yield_curve")
        # Re-steepener detector — use the 90-day trailing min as the inversion floor.
        yc_floor = _trailing_min(macro, "yield_curve", as_of=as_of, days=90)

        cs_latest = _get("credit_spread")
        cs_prior = _get_lagged(macro, "credit_spread", as_of=as_of, days=5, missing=missing)

        vix_now = _series_value_at_or_before(vix_proxy, as_of)
        vix_ma = _series_value_at_or_before(vix_200d_ma, as_of)
        vix_now_dec = Decimal(str(round(vix_now, 6))) if vix_now is not None else None
        vix_ma_dec = Decimal(str(round(vix_ma, 6))) if vix_ma is not None else None

        sahm_p = score_sahm_rule(sahm_v)
        ip_p = score_industrial_production(ip_v)
        ic_p = score_initial_claims(ic_now, ic_prev, ic_two)
        yc_p = score_yield_curve(yc_latest, yc_floor)
        cs_p = score_credit_spread(cs_latest, cs_prior)
        vix_p = score_vix_proxy(vix_now_dec, vix_ma_dec)

        raw = sahm_p + ip_p + ic_p + yc_p + cs_p + vix_p
        # FilterDiagnostics — one ``passed`` for each sub-scorer that fired.
        diag = FilterDiagnostics(
            universe_total=6,  # six sub-scorers evaluated per day
            candidates_passed=sum(1 for p in (sahm_p, ip_p, ic_p, yc_p, cs_p, vix_p) if p > 0),
            sahm_rule_blocked=0 if sahm_p > 0 else 1,
            industrial_production_blocked=0 if ip_p > 0 else 1,
            initial_claims_blocked=0 if ic_p > 0 else 1,
            yield_curve_blocked=0 if yc_p > 0 else 1,
            credit_spread_blocked=0 if cs_p > 0 else 1,
            vix_proxy_blocked=0 if vix_p > 0 else 1,
        )
        return BearScoreBreakdown(
            as_of=as_of,
            sahm_pts=sahm_p,
            industrial_production_pts=ip_p,
            initial_claims_pts=ic_p,
            yield_curve_pts=yc_p,
            credit_spread_pts=cs_p,
            vix_pts=vix_p,
            raw_total=raw,
            score=scale_raw_to_100(raw),
            indicators_missing=tuple(sorted(set(missing))),
            filter_diagnostics=diag,
        )


# ─── Helpers (module-local; used by SentinelSetupDetection._score_one_date) ─


def _get_lagged(
    macro: pd.DataFrame,
    indicator: str,
    *,
    as_of: date_t,
    days: int,
    missing: list[str],
) -> Decimal | None:
    """Most recent observation at or before ``as_of - days``. None if missing."""
    if indicator not in macro.columns:
        return None
    target = as_of - timedelta(days=days)
    sub = macro.loc[macro.index <= target, indicator]
    if len(sub) == 0 or pd.isna(sub.iloc[-1]):
        return None
    return Decimal(str(sub.iloc[-1]))


def _trailing_min(
    macro: pd.DataFrame,
    indicator: str,
    *,
    as_of: date_t,
    days: int,
) -> Decimal | None:
    """Min observation in (as_of - days, as_of]. None if no data in window."""
    if indicator not in macro.columns:
        return None
    lo = as_of - timedelta(days=days)
    sub = macro.loc[(macro.index > lo) & (macro.index <= as_of), indicator]
    if len(sub) == 0 or sub.isna().all():
        return None
    return Decimal(str(float(sub.min())))


def _series_value_at_or_before(series: pd.Series, as_of: date_t) -> float | None:
    """Most recent non-NaN value at or before ``as_of``."""
    if len(series) == 0:
        return None
    sub = series.loc[series.index <= pd.Timestamp(as_of)]
    sub = sub.dropna()
    if len(sub) == 0:
        return None
    return float(sub.iloc[-1])


__all__ = [
    "SentinelSetupDetection",
    "compute_vix_proxy_series",
    "compute_spy_rally_pct",
    "fetch_macro_indicator_panel",
    "fetch_spy_close",
    "score_sahm_rule",
    "score_industrial_production",
    "score_initial_claims",
    "score_yield_curve",
    "score_credit_spread",
    "score_vix_proxy",
    "scale_raw_to_100",
]
