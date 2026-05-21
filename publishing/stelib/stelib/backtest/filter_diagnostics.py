"""Shared filter-diagnostic counters for engine setup-detection plugs.

Every engine's setup-detection pipeline filters its universe down through
a series of gates (liquidity → fundamentals → catalyst → technical → score
floor, in different orders per engine). When debugging a quiet day — "why
did no signals fire?" — the operator wants a count of *how many tickers
were rejected at each gate*, not just a list of survivors.

:class:`FilterDiagnostics` is the shared shape every engine populates. It
has a small set of common counters (universe_total, coarse_liquidity_blocked,
candidates_passed) plus engine-specific optional counters. An engine only
populates its own fields; the others stay ``None`` and are excluded from
the serialised JSON via ``model_dump(exclude_none=True)``.

Usage in a plug::

    diag = FilterDiagnostics(universe_total=len(universe))
    for ticker in universe:
        if not _coarse_liquidity_ok(...):
            diag.coarse_liquidity_blocked += 1
            continue
        if not _gate1_value_ok(...):
            diag.gate1_value_blocked = (diag.gate1_value_blocked or 0) + 1
            continue
        ...
        diag.candidates_passed += 1
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class FilterDiagnostics(BaseModel):
    """Per-filter pass/block counters for one setup-detection scan.

    All engine-specific counters are ``Optional[int]`` with default
    ``None``. An engine populates *only* the counters that apply to its
    filter stack; the rest stay ``None`` and are dropped from the
    serialised output via ``exclude_none=True``."""

    model_config = ConfigDict(extra="forbid")

    # ─── Common counters (every engine populates these) ─────────────────────
    universe_total: int = Field(
        default=0, description="Total tickers evaluated in the scan",
    )
    coarse_liquidity_blocked: int = Field(
        default=0, description="Blocked by price/volume coarse filter (or missing data)",
    )
    candidates_passed: int = Field(
        default=0, description="Tickers that passed every filter and entered the candidate list",
    )

    # ─── Vector-specific gates ──────────────────────────────────────────────
    gate1_value_blocked: int | None = Field(
        default=None, description="Vector: blocked by P/B, D/E, or Revenue gate",
    )
    gate2_catalyst_blocked: int | None = Field(
        default=None, description="Vector: blocked by missing catalyst event",
    )
    gate3_technical_blocked: int | None = Field(
        default=None, description="Vector: blocked by technical-trigger failure",
    )
    crash_guard_size_reduced: int | None = Field(
        default=None, description="Vector: VIX crash guard reduced position size",
    )

    # ─── Sigma-specific gates ───────────────────────────────────────────────
    adx_blocked: int | None = Field(
        default=None, description="Sigma: blocked by ADX threshold",
    )
    chop_blocked: int | None = Field(
        default=None, description="Sigma: blocked by CHOP threshold",
    )
    bb_width_blocked: int | None = Field(
        default=None, description="Sigma: blocked by Bollinger Band width percentile",
    )
    band_proximity_blocked: int | None = Field(
        default=None, description="Sigma: price not near BB band",
    )
    stochastic_blocked: int | None = Field(
        default=None, description="Sigma: stochastic not in oversold zone",
    )
    volume_declining_blocked: int | None = Field(
        default=None, description="Sigma: volume not declining",
    )

    # ─── Momentum-specific gates ────────────────────────────────────────────
    momentum_history_blocked: int | None = Field(
        default=None, description="Momentum: insufficient bar history for the lookback window",
    )
    momentum_score_blocked: int | None = Field(
        default=None, description="Momentum: 12-1 score could not be computed (NaN / zero prior)",
    )
    momentum_tradability_blocked: int | None = Field(
        default=None, description="Momentum: failed common-stock-only or min-price filter",
    )

    # ─── Reversion-specific gates ───────────────────────────────────────────
    z_score_blocked: int | None = Field(
        default=None, description="Reversion: blocked by Z-score threshold",
    )
    rsi_blocked: int | None = Field(
        default=None, description="Reversion: blocked by RSI threshold",
    )
    volume_climax_blocked: int | None = Field(
        default=None, description="Reversion: blocked by volume climax check",
    )
    earnings_quality_blocked: int | None = Field(
        default=None, description="Reversion: blocked by earnings quality gate",
    )
    adx_trending_blocked: int | None = Field(
        default=None, description="Reversion: blocked by ADX trending shutdown",
    )

    # ─── Sentinel-specific gates ────────────────────────────────────────────
    # Sentinel's "universe" is the six Bear Score sub-scorers. For each daily
    # observation we increment the per-sub-scorer ``_blocked`` counter when
    # that sub-scorer contributes zero points. ``candidates_passed`` is the
    # number of sub-scorers that fired (raw_total > 0).
    sahm_rule_blocked: int | None = Field(
        default=None, description="Sentinel: Sahm Rule below 0.50",
    )
    industrial_production_blocked: int | None = Field(
        default=None, description="Sentinel: Industrial Production not contracting",
    )
    initial_claims_blocked: int | None = Field(
        default=None, description="Sentinel: Initial Claims not rising above threshold",
    )
    yield_curve_blocked: int | None = Field(
        default=None, description="Sentinel: yield curve not in bear-steepener regime",
    )
    credit_spread_blocked: int | None = Field(
        default=None,
        description=(
            "Sentinel: Baa-10Y credit spread did not contribute (below 3% "
            "Watch tier OR tightening at <5% level). Replaced "
            "``hy_spread_blocked`` 2026-05-15 when FRED truncated "
            "BAMLH0A0HYM2 and the sub-scorer switched to BAA10Y."
        ),
    )
    vix_proxy_blocked: int | None = Field(
        default=None, description="Sentinel: VIX proxy not above 25",
    )

    # ─── Catalyst-specific gates ────────────────────────────────────────────
    cluster_size_blocked: int | None = Field(
        default=None,
        description="Catalyst: fewer than MIN_DISTINCT_INSIDERS distinct "
                    "insider BUYers in the cluster window",
    )
    cluster_value_blocked: int | None = Field(
        default=None,
        description="Catalyst: aggregate insider-BUY $ in the cluster window "
                    "below MIN_AGGREGATE_USD",
    )
    catalyst_liquidity_blocked: int | None = Field(
        default=None,
        description="Catalyst: blocked by price/volume liquidity gate or "
                    "insufficient price history for the SMA",
    )
    catalyst_trend_blocked: int | None = Field(
        default=None,
        description="Catalyst: last close at or below the trend-filter SMA",
    )
