"""4-component Fear & Greed Index — pure, reusable, no I/O.

Internally-computed from data the platform already has (FRED VIX +
hy_spread + yield_curve, prices_daily SPY). No external API, no CNN
scrape, no Yahoo. Adapted from the tsp-entrepreneur model.

Components & weights:
  * volatility   30% — (vix_50dma − vix)/vix_50dma, clip ±1 → [0,100]
  * credit       30% — 756d rolling z of HY OAS; 50 − z·15, clip[0,100]
  * momentum     25% — (sp500/sp500_125dma − 1)·500 + 50, clip[0,100]
  * safe_haven   15% — (t10y2y + 1)/4 · 100, clip[0,100]

score = 0.30·vol + 0.30·credit + 0.25·mom + 0.15·safe (1 decimal).
label: <25 Extreme Fear, <45 Fear, <55 Neutral, <75 Greed,
       ≥75 Extreme Greed.
direction: vs score 5 trading days ago, ±2.0 dead-band →
           rising / falling / flat.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

VOL_WEIGHT = 0.30
CREDIT_WEIGHT = 0.30
MOMENTUM_WEIGHT = 0.25
SAFE_HAVEN_WEIGHT = 0.15

VIX_MA_WINDOW = 50
CREDIT_Z_WINDOW = 756        # ~3 trading years
SP500_MA_WINDOW = 125
DIRECTION_LOOKBACK = 5       # trading days
DIRECTION_DEAD_BAND = 2.0


def _label(score: float) -> str:
    if score < 25:
        return "Extreme Fear"
    if score < 45:
        return "Fear"
    if score < 55:
        return "Neutral"
    if score < 75:
        return "Greed"
    return "Extreme Greed"


def compute_fear_greed(
    vix: pd.Series,
    hy_oas: pd.Series,
    sp500: pd.Series,
    t10y2y: pd.Series,
) -> pd.DataFrame:
    """Compute the daily Fear & Greed index.

    Each arg is a date-indexed numeric ``pd.Series``. Inputs are
    aligned on the union of dates and forward-filled (macro series
    publish on different calendars); rows lacking enough history for a
    rolling window come back as NaN in the affected component (the
    caller / backfill filters incomplete rows).

    Returns a DataFrame indexed by date with columns: ``score``,
    ``label``, ``direction``, ``score_5d_ago``, and the four component
    columns ``volatility_component`` / ``credit_component`` /
    ``momentum_component`` / ``safe_haven_component``.
    """
    idx = (
        vix.index.union(hy_oas.index)
        .union(sp500.index)
        .union(t10y2y.index)
        .sort_values()
    )
    vix = vix.reindex(idx).ffill()
    hy_oas = hy_oas.reindex(idx).ffill()
    sp500 = sp500.reindex(idx).ffill()
    t10y2y = t10y2y.reindex(idx).ffill()

    # ── volatility (30%): VIX below its 50dma = Greed ────────────────
    vix_ma = vix.rolling(VIX_MA_WINDOW, min_periods=VIX_MA_WINDOW).mean()
    vol_raw = ((vix_ma - vix) / vix_ma).clip(-1.0, 1.0)
    volatility = ((vol_raw + 1.0) / 2.0) * 100.0

    # ── credit (30%): wide/widening HY OAS = Fear ────────────────────
    z_mean = hy_oas.rolling(CREDIT_Z_WINDOW, min_periods=CREDIT_Z_WINDOW).mean()
    z_std = hy_oas.rolling(CREDIT_Z_WINDOW, min_periods=CREDIT_Z_WINDOW).std()
    z = (hy_oas - z_mean) / z_std.replace(0.0, np.nan)
    credit = (50.0 - z * 15.0).clip(0.0, 100.0)

    # ── momentum (25%): S&P below its 125dma = Fear ──────────────────
    sp_ma = sp500.rolling(SP500_MA_WINDOW, min_periods=SP500_MA_WINDOW).mean()
    momentum = (((sp500 / sp_ma) - 1.0) * 500.0 + 50.0).clip(0.0, 100.0)

    # ── safe haven (15%): inverted curve = Fear ──────────────────────
    safe_haven = (((t10y2y + 1.0) / 4.0) * 100.0).clip(0.0, 100.0)

    score = (
        VOL_WEIGHT * volatility
        + CREDIT_WEIGHT * credit
        + MOMENTUM_WEIGHT * momentum
        + SAFE_HAVEN_WEIGHT * safe_haven
    ).round(1)

    out = pd.DataFrame(
        {
            "score": score,
            "volatility_component": volatility.round(2),
            "credit_component": credit.round(2),
            "momentum_component": momentum.round(2),
            "safe_haven_component": safe_haven.round(2),
        },
        index=idx,
    )
    out["score_5d_ago"] = out["score"].shift(DIRECTION_LOOKBACK)

    delta = out["score"] - out["score_5d_ago"]
    out["direction"] = np.select(
        [delta > DIRECTION_DEAD_BAND, delta < -DIRECTION_DEAD_BAND],
        ["rising", "falling"],
        default="flat",
    )
    # direction is meaningless without a 5d-ago anchor
    out.loc[out["score_5d_ago"].isna(), "direction"] = None
    out["label"] = out["score"].map(
        lambda s: _label(float(s)) if pd.notna(s) else None
    )
    return out[
        [
            "score", "label", "direction", "score_5d_ago",
            "volatility_component", "credit_component",
            "momentum_component", "safe_haven_component",
        ]
    ]


__all__ = ["compute_fear_greed", "_label"]
