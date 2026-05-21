"""Abdi-Ranaldo (2017) bid-ask spread estimator — active implementation.

Reference: "A Simple Estimation of Bid-Ask Spreads from Daily Close,
High, and Low Prices", Abdi & Ranaldo, Review of Financial Studies,
2017. https://doi.org/10.1093/rfs/hhx084

Replaces the Corwin-Schultz (2012) estimator that was retired
2026-05-15 after it was found to systematically invert liquidity
rankings for individual stocks (volatility-driven HIGH/LOW ranges
were being interpreted as wide spreads on the most liquid names).

The Abdi-Ranaldo estimator addresses C-S's failure mode by anchoring
spread inference to the **close** price (an observable point estimate
of mid) rather than the HIGH/LOW range (a volatility-confounded
estimate). High-volatility liquid stocks no longer get penalized;
narrow daily ranges no longer get rewarded for illiquidity.

Formula
-------

For each trading day t with daily bars (H_t, L_t, C_t) and the next
day's (H_{t+1}, L_{t+1}, C_{t+1}):

    η_t   = (log(H_t) + log(L_t)) / 2          # mid-range log-price
    s²_t  = max(0, 4 * (log(C_t) - η_t) * (log(C_t) - η_{t+1}))
    S_t   = √(s²_t)                             # daily spread in log space

The per-ticker spread estimate is the mean of valid daily S_t values.
We then exponentiate-and-subtract-one to convert log-space to a
fraction-of-mid: ``spread_pct ≈ S_t`` for small S (first-order
approximation; the linear regime holds up to ~5% spreads, far
beyond anything tradeable).

Negative single-day estimates are clipped to 0 per the paper —
they're noise from the L_t > C_t edge case. NaN values appear where
the next-day bar is missing; callers ``.dropna()`` before reducing.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd
import structlog

logger = structlog.get_logger(__name__)

# Source tag persisted to ``platform.spread_observations``. Keeps the
# audit trail honest about which estimator wrote each row.
SOURCE_TAG = "abdi_ranaldo"

# Default lookback. AR is more sample-efficient than C-S because each
# observation uses 4 OHLC numbers vs C-S's 2 HL pairs, but a longer
# window still smooths microstructure noise. 35 calendar days ~ 22
# trading-day pairs after the trailing-day drop.
_DEFAULT_LOOKBACK_CALENDAR_DAYS = 365
_MIN_OBSERVATIONS = 5

# Coarse-universe filter — mirrors ``simulate_universe.py`` so the same
# tickers get ranked first. Engines need to see liquid names, so we
# pre-filter to a sensible price + volume floor before the estimator
# does any work.
_MIN_PRICE = Decimal("10.00")
_MIN_AVG_VOLUME = 1_000_000


def estimate_spread_abdi_ranaldo(
    high: pd.Series, low: pd.Series, close: pd.Series
) -> pd.Series:
    """Return AR's per-day **product term** (unclipped, can be negative).

    Inputs are three pandas Series indexed identically (typically by
    date). Output is a Series of the same length whose values are the
    per-day cross-product ``4 * (log(C_t) - η_t) * (log(C_t) - η_{t+1})``.
    The last value is NaN — the formula needs the t+1 mid-range.

    **Important**: these are NOT per-day spread estimates. Per-day
    values are noisy and frequently negative; the spread emerges only
    after averaging across many days. ``average_spread_estimate``
    aggregates these correctly (mean → clip-at-zero → square root).
    Callers that want a single spread for a ticker should use
    ``average_spread_estimate`` directly.
    """
    if not (len(high) == len(low) == len(close)):
        raise ValueError(
            f"high/low/close length mismatch: "
            f"high={len(high)} low={len(low)} close={len(close)}"
        )
    h = high.astype(float)
    l_ = low.astype(float)
    c = close.astype(float)

    # Guard against rows with non-positive prices — log() NaNs.
    valid = (h > 0) & (l_ > 0) & (c > 0)
    h = h.where(valid)
    l_ = l_.where(valid)
    c = c.where(valid)

    log_h = np.log(h)
    log_l = np.log(l_)
    log_c = np.log(c)
    eta = (log_h + log_l) / 2.0
    # Per-day product term. Negative values are NOT clipped here —
    # the AR estimator's correctness depends on positive AND negative
    # per-day products averaging out toward zero for stocks with no
    # bid-ask bounce. Clipping at the per-day stage inflates the
    # estimate for liquid stocks (the Corwin-Schultz failure mode
    # this estimator was meant to replace).
    product = 4.0 * (log_c - eta) * (log_c - eta.shift(-1))
    return product


def average_spread_estimate(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    *,
    min_observations: int = _MIN_OBSERVATIONS,
) -> float | None:
    """Single AR spread estimate for a ticker — fraction of mid.

    Aggregates the per-day product terms correctly:

        S = √(max(0, mean(per_day_products)))

    The clip-at-zero step happens on the MEAN, not on each per-day
    value — see ``estimate_spread_abdi_ranaldo``'s docstring. The
    mean is taken over finite values only (NaN dropped). Returns
    ``None`` when fewer than ``min_observations`` finite per-day
    values are available.

    Result is a fraction (e.g. ``0.00015`` = 1.5 bp), suitable for
    direct comparison against ``TIER_BOUNDS`` in
    ``scripts/assign_liquidity_tiers.py``.
    """
    raw = (
        estimate_spread_abdi_ranaldo(high, low, close)
        .replace([np.inf, -np.inf], np.nan)
        .dropna()
    )
    if len(raw) < min_observations:
        return None
    mean_product = float(raw.mean())
    if mean_product <= 0:
        return 0.0
    return float(np.sqrt(mean_product))


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
    """Compute average AR spread per active ticker, sorted ascending.

    Returns ``[(ticker, avg_spread), ...]`` — narrowest spread first.

    When ``persist=True`` (default), each non-NaN per-ticker estimate
    is written to ``platform.spread_observations`` with
    ``source='abdi_ranaldo'`` so the audit trail records what the
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
        avg = average_spread_estimate(group["high"], group["low"], group["close"])
        if avg is None:
            continue
        estimates.append((ticker, avg))
        bootstrap_observations.append((
            ticker,
            Decimal(str(round(avg, 6))),
            now,
            "regular",
            SOURCE_TAG,
        ))

    estimates.sort(key=lambda x: x[1])

    if persist and bootstrap_observations:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.executemany(_INSERT_OBS_SQL, bootstrap_observations)
        logger.info(
            "tpcore.backtest.spread_estimator.bootstrap_persisted",
            n=len(bootstrap_observations),
            source=SOURCE_TAG,
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


__all__ = [
    "SOURCE_TAG",
    "average_spread_estimate",
    "estimate_spread_abdi_ranaldo",
    "rank_universe_by_liquidity",
]
