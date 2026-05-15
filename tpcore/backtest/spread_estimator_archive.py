"""ARCHIVED — Corwin-Schultz (2012) high-low bid-ask spread estimator.

**RETIRED 2026-05-15.** Do not import from this module in any active
code path. The C-S estimator was found to invert liquidity rankings
for individual stocks: high-volatility mega-caps (AAPL/NVDA/TSLA)
got wide-spread estimates (T3/T4) because their daily HIGH/LOW range
reflects price discovery rather than quote width; illiquid microcaps
(BEBE/FONR/APXT) with narrow ranges got tight-spread estimates (T1)
because nobody trades them enough to widen the daily range. The
active estimator is now Abdi-Ranaldo (2017), in
``tpcore.backtest.spread_estimator``. This file is preserved verbatim
for academic reference and the historical
``source='corwin_schultz'`` rows in ``platform.spread_observations``.

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
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd
import structlog

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

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
    pool: asyncpg.Pool,
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


__all__ = [
    "estimate_spread_corwin_schultz",
    "average_spread_estimate",
    "rank_universe_by_liquidity",
]
