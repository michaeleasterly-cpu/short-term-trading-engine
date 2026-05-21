"""ARCHIVED — retired/rejected research-quality estimators.

Two tenants share this archive. Both were proposed, tried, and removed
from the active code path. Preserved verbatim so the rationale and
implementation are easy to revisit if anyone re-opens the question.

1. **Corwin-Schultz (2012) — retired 2026-05-15.**
   High-low bid-ask spread estimator. Found to invert liquidity rankings
   for individual stocks: high-volatility mega-caps (AAPL/NVDA/TSLA)
   got wide-spread estimates (T3/T4) because their daily HIGH/LOW range
   reflects price discovery rather than quote width; illiquid microcaps
   (BEBE/FONR/APXT) with narrow ranges got tight-spread estimates (T1)
   because nobody trades them enough to widen the daily range. The
   active estimator is now Abdi-Ranaldo (2017), in
   ``tpcore.backtest.spread_estimator``. Historical
   ``source='corwin_schultz'`` rows in ``platform.spread_observations``
   are retained for audit only.

2. **Ornstein-Uhlenbeck κ gate — rejected 2026-05-15.**
   Sigma research spike: gate Sigma's setup candidates on whether the
   last 60 closes fit an OU process with κ above a sweep-set
   threshold. 50-trial walk-forward sweep showed the gate degraded
   Sigma's edge — best across-window mean Sharpe fell from +1.073
   (baseline) to +0.898 (κ ≥ 1.46), held-back Sharpe regressed from
   +0.839 to +0.366, and the gate cut *more* trades in the stable
   walk-forward windows (where Sigma's edge actually fires) than in
   the fragile window it was meant to filter. Mechanism: low AR(1)
   coefficient (= high κ) does not correlate with the bounded-channel
   regime Sigma trades. See verdict notes in commit ``dd7a597`` and
   the rejection write-up in conversation log of 2026-05-15.

Reference: "A simple way to estimate bid–ask spreads from daily high
and low prices", Corwin & Schultz, Journal of Finance, 2012.

The estimator uses only daily H/L data — no quote feed required — so
it works against ``platform.prices_daily`` we already have for 7,694
tickers. Phase 2 uses it **only** to rank the universe and pick the
top-200 to subscribe to first via Tradier's streaming WebSocket. All
tier assignments in ``platform.liquidity_tiers`` flow from real
streaming data (``source = 'tradier_streaming'``); the CS rows we
write here land with ``source = 'corwin_schultz'`` and are for
reference / prioritisation only.

Formula
-------
For consecutive trading days t and t+1:

  β_t = (ln(H_t / L_t))^2 + (ln(H_{t+1} / L_{t+1}))^2
  γ_t = (ln(max(H_t, H_{t+1}) / min(L_t, L_{t+1})))^2
  α_t = (√(2β_t) - √β_t) / (3 - 2√2) - √(γ_t / (3 - 2√2))
  S_t = 2 (e^α_t - 1) / (1 + e^α_t)

Negative single-day estimates are clipped to 0 (the paper recommends
either clipping or excluding; we clip so the per-ticker mean is a
stable scalar).
"""
from __future__ import annotations

import math
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd
import structlog

logger = structlog.get_logger(__name__)

# 3 − 2√2 appears in both the α numerator and denominator. Pre-compute.
_K_DENOM = 3 - 2 * math.sqrt(2)

# Default lookback for ``rank_universe_by_liquidity`` — covers ~20 trading days
# of HL pairs once we drop the first row (no t-1 to pair with).
_DEFAULT_LOOKBACK_CALENDAR_DAYS = 35
_MIN_OBSERVATIONS = 5

# Coarse-universe filter mirrors ``simulate_universe.py`` so the same
# 1,400-ish tickers get prioritised first.
_MIN_PRICE = Decimal("10.00")
_MIN_AVG_VOLUME = 1_000_000


def estimate_spread_corwin_schultz(
    high: pd.Series, low: pd.Series
) -> pd.Series:
    """Return the Corwin-Schultz daily spread estimate per the paper.

    Inputs are two pandas Series indexed identically (typically by
    date). Outputs are a Series of the same length whose values are
    daily spread estimates as a fraction of mid-price (e.g. 0.0015 =
    15 bps). The first value is NaN — the formula needs a t-1 bar.

    Negative single-day estimates are clipped to 0. NaN values appear
    where ``γ`` produces a domain error (e.g. constant H = L); callers
    should ``.dropna()`` before reducing.
    """
    if len(high) != len(low):
        raise ValueError(
            f"high/low length mismatch: high={len(high)} low={len(low)}"
        )
    high = high.astype(float)
    low = low.astype(float)
    log_hl = np.log(high / low)
    beta = log_hl.pow(2) + log_hl.shift(-1).pow(2)
    # γ uses the 2-day max-high / min-low envelope. Align t and t+1.
    h2 = np.maximum(high, high.shift(-1))
    l2 = np.minimum(low, low.shift(-1))
    log_h2_l2 = np.log(h2 / l2)
    gamma = log_h2_l2.pow(2)

    with np.errstate(invalid="ignore"):
        alpha = (
            (np.sqrt(2 * beta) - np.sqrt(beta)) / _K_DENOM
            - np.sqrt(gamma / _K_DENOM)
        )
        spread = 2 * (np.exp(alpha) - 1) / (1 + np.exp(alpha))

    # Clip negative single-day estimates to zero (CS 2012 §III).
    spread = spread.clip(lower=0)
    return spread


def average_spread_estimate(
    high: pd.Series, low: pd.Series, *, min_observations: int = _MIN_OBSERVATIONS
) -> float | None:
    """Return the mean CS daily spread for a single ticker.

    Returns ``None`` when fewer than ``min_observations`` finite daily
    estimates are available — the per-ticker mean is too noisy to
    rank otherwise.
    """
    raw = estimate_spread_corwin_schultz(high, low).replace(
        [np.inf, -np.inf], np.nan
    ).dropna()
    if len(raw) < min_observations:
        return None
    return float(raw.mean())


# ── DB-backed ranking + persistence ─────────────────────────────────────


_RANKING_BARS_SQL = """
    WITH active_tickers AS (
        SELECT DISTINCT ticker
        FROM platform.prices_daily
        WHERE delisted = false
          AND date >= CURRENT_DATE - INTERVAL '90 days'
    ),
    windowed AS (
        SELECT pd.ticker, pd.date, pd.high, pd.low, pd.close, pd.volume,
               ROW_NUMBER() OVER (PARTITION BY pd.ticker ORDER BY pd.date DESC) AS rn
        FROM platform.prices_daily pd
        JOIN active_tickers a USING (ticker)
        WHERE pd.date >= CURRENT_DATE - INTERVAL '{lookback} days'
    )
    SELECT ticker, date, high, low, close, volume, rn
    FROM windowed
    ORDER BY ticker, date
"""

_INSERT_OBS_SQL = """
    INSERT INTO platform.spread_observations
        (ticker, spread_pct, observed_at, session, source)
    VALUES ($1, $2, $3, $4, $5)
"""


async def rank_universe_by_liquidity(
    pool: Any,
    *,
    lookback_days: int = _DEFAULT_LOOKBACK_CALENDAR_DAYS,
    persist: bool = True,
    coarse_filter: bool = True,
) -> list[tuple[str, float]]:
    """Compute average CS spread per active ticker, sorted ascending.

    Returns ``[(ticker, avg_spread), ...]`` — narrowest spread first. The
    caller hands the top N tickers to the Tradier streaming subscriber.

    When ``persist=True`` (default), each non-NaN per-ticker estimate
    is written to ``platform.spread_observations`` with
    ``source='corwin_schultz'`` so we have an audit trail of what the
    bootstrap recommended on a given day.
    """
    sql = _RANKING_BARS_SQL.format(lookback=lookback_days)
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql)
    if not rows:
        return []

    df = pd.DataFrame(
        [
            {
                "ticker": r["ticker"],
                "date": r["date"],
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "volume": int(r["volume"]),
            }
            for r in rows
        ]
    )

    estimates: list[tuple[str, float]] = []
    bootstrap_observations: list[tuple] = []
    now = datetime.now(UTC)
    for ticker, group in df.groupby("ticker", sort=False):
        group = group.sort_values("date")
        if coarse_filter and not _passes_coarse(group):
            continue
        avg = average_spread_estimate(group["high"], group["low"])
        if avg is None:
            continue
        estimates.append((ticker, avg))
        bootstrap_observations.append((
            ticker,
            Decimal(str(round(avg, 6))),
            now,
            "regular",
            "corwin_schultz",
        ))

    estimates.sort(key=lambda x: x[1])

    if persist and bootstrap_observations:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.executemany(_INSERT_OBS_SQL, bootstrap_observations)
        logger.info(
            "tpcore.backtest.spread_estimator.bootstrap_persisted",
            n=len(bootstrap_observations),
        )

    return estimates


def _passes_coarse(group: pd.DataFrame) -> bool:
    """Mirror simulate_universe.py: last close > $10 AND avg 20-day volume > 1M."""
    if len(group) < 20:
        return False
    last_close = Decimal(str(group["close"].iloc[-1]))
    avg_vol = group["volume"].tail(20).mean()
    if last_close <= _MIN_PRICE:
        return False
    return avg_vol > _MIN_AVG_VOLUME


# ── Ornstein-Uhlenbeck mean-reversion gate (Sigma spike, 2026-05-15) ────
#
# OU SDE: dX_t = κ(θ − X_t) dt + σ dW_t
# Discrete MLE via AR(1) on the log-close series: regress x_t on x_{t-1},
# the AR(1) coefficient b = exp(−κ · dt). When 0 < b < 1 the series is
# mean-reverting and κ = −log(b) / dt; otherwise κ = 0 (random walk /
# trend / insufficient observations / non-positive prices).
#
# Lived briefly in ``sigma/plugs/setup_detection.py`` as
# ``estimate_ou_kappa``; removed from the live engine 2026-05-15 after
# the sweep verdict. The implementation is correct — the *hypothesis*
# (that filtering Sigma candidates by κ tightens DSR) was wrong.


def estimate_ou_kappa(close_prices, dt: float = 1.0 / 252.0) -> float:
    """Estimate the Ornstein-Uhlenbeck mean-reversion speed κ.

    Args:
        close_prices: 1-D array-like of close prices (typically the last
            ~60 trading days). Log-transformed before fitting.
        dt: time step in years. Default 1/252 (one trading day).

    Returns:
        κ in trading-year units. ``0.0`` when the series is not mean-
        reverting (AR(1) coefficient ≥ 1 — random walk or trend), when
        fewer than 10 observations are available, or when any close
        price is non-positive (log undefined).

    Half-life in trading days is ``log(2) / κ / dt`` when κ > 0; e.g.
    κ=2.0 → ~87 trading days, κ=5.0 → ~35 trading days.
    """
    arr = np.asarray(close_prices, dtype=float).ravel()
    if arr.size < 10:
        return 0.0
    if not np.all(arr > 0):
        return 0.0
    x = np.log(arr)
    x_prev = x[:-1]
    x_next = x[1:]
    x_prev_mean = x_prev.mean()
    x_next_mean = x_next.mean()
    cov = ((x_prev - x_prev_mean) * (x_next - x_next_mean)).mean()
    var_prev = ((x_prev - x_prev_mean) ** 2).mean()
    if var_prev <= 0:
        return 0.0
    b = cov / var_prev
    if not (0.0 < b < 1.0):
        return 0.0
    return float(-np.log(b) / dt)


__all__ = [
    "estimate_spread_corwin_schultz",
    "average_spread_estimate",
    "rank_universe_by_liquidity",
    "estimate_ou_kappa",
]
