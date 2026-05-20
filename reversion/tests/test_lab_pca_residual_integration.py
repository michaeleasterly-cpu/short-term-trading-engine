"""Reversion PCA-residual Lab candidate — integration smoke tests.

Hermetic synthetic-universe integration: builds a 30-ticker × 500-
session synthetic universe with a factor-driven return structure, runs
the ``signal_mode="pca_residual"`` backtest end-to-end, and asserts
the result wires through cleanly. No live-data dependency; runs in CI
offline.

Spec §5.3 (I1-I3).
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from reversion import backtest as bt


def _make_factor_panel(
    *, ticker: str, beta_market: float, beta_sector: float,
    start: date, end: date, market_returns: np.ndarray,
    sector_returns: np.ndarray, seed: int,
) -> pd.DataFrame:
    """Synthetic OHLCV panel driven by a market + sector factor + idio
    noise. The factor exposure is per-ticker — the PCA primitive
    should be able to identify and remove these factors."""
    rng = np.random.default_rng(seed)
    n = len(market_returns)
    idio = rng.normal(0.0, 0.008, size=n)
    log_returns = (
        beta_market * market_returns + beta_sector * sector_returns + idio
    )
    log_returns[0] = 0.0
    log_prices = np.cumsum(log_returns)
    closes = 100.0 * np.exp(log_prices)
    highs = closes * (1 + rng.uniform(0, 0.005, size=n))
    lows = closes * (1 - rng.uniform(0, 0.005, size=n))
    opens = closes * (1 + rng.normal(0, 0.002, size=n))
    volumes = rng.integers(2_000_000, 5_000_000, size=n)
    sessions = pd.bdate_range(start, periods=n)
    df = pd.DataFrame(
        {
            "open": opens,
            "high": np.maximum(np.maximum(highs, opens), closes),
            "low": np.minimum(np.minimum(lows, opens), closes),
            "close": closes,
            "volume": volumes,
        },
        index=pd.Index([d.date() for d in sessions], name="date"),
    )
    df["ticker"] = ticker
    return bt._precompute_indicators(df)  # noqa: SLF001 — fixture mirrors precompute


def _make_pca_integration_context() -> bt.ReversionWindowContext:
    """Build a 30-ticker × 500-session synthetic universe with a 2-
    factor structure (market + sector). Seeded for determinism."""
    rng = np.random.default_rng(42)
    n_sessions = 500
    start = date(2022, 1, 3)
    market = rng.normal(0.0003, 0.012, size=n_sessions)
    sector_returns_a = rng.normal(0.0, 0.01, size=n_sessions)
    sector_returns_b = rng.normal(0.0, 0.01, size=n_sessions)

    panels: dict[str, pd.DataFrame] = {}
    for i in range(30):
        beta_m = float(rng.uniform(0.4, 1.4))
        # Half the universe loads on sector A, half on sector B.
        sec_ret = sector_returns_a if i % 2 == 0 else sector_returns_b
        beta_s = float(rng.uniform(-0.5, 1.0))
        panels[f"TST{i:03d}"] = _make_factor_panel(
            ticker=f"TST{i:03d}",
            beta_market=beta_m, beta_sector=beta_s,
            start=start, end=date(2024, 12, 31),
            market_returns=market, sector_returns=sec_ret, seed=500 + i,
        )

    # SPY panel — small market loader.
    spy = _make_factor_panel(
        ticker="SPY", beta_market=1.0, beta_sector=0.0,
        start=start, end=date(2024, 12, 31),
        market_returns=market, sector_returns=sector_returns_a, seed=777,
    )

    # The window inside the synthetic series — the PCA-residual
    # primitive needs ≥ 252 prior bars; pick a start ≥ 1 year in.
    window_start = date(2023, 6, 1)
    window_end = date(2024, 6, 28)
    return bt.ReversionWindowContext(
        panels=panels,
        spy_panel=spy,
        fundamentals={},
        tier_round_trip_costs={},
        funded_tickers=list(panels.keys()),
        start=window_start,
        end=window_end,
        universe=tuple(panels.keys()),
    )


@pytest.fixture(autouse=True)
def _reset_signal_mode_override() -> None:
    bt._SIGNAL_MODE_OVERRIDE = None  # noqa: SLF001
    yield
    bt._SIGNAL_MODE_OVERRIDE = None  # noqa: SLF001


def test_I1_pca_residual_produces_non_empty_trade_set() -> None:
    """The pca_residual branch must wire end-to-end and produce trades
    on a seeded synthetic universe."""
    ctx = _make_pca_integration_context()
    result = bt.run_reversion_with_context(
        ctx, overrides={"signal_mode": "pca_residual"},
    )
    # Wiring proof: the parameters round-trip the signal_mode.
    assert result.parameters["signal_mode"] == "pca_residual"
    # Smoke proof: at least one trade fired. Empty would imply the
    # entire signal path is dead. (We do not assert a specific trade
    # count — the synthetic fixture is not a calibration target.)
    assert result.trades >= 1, (
        f"PCA-residual signal produced zero trades on the seeded "
        f"synthetic universe — branch is wired but signal is silent "
        f"(result={result!r})"
    )


def test_I2_pca_residual_parameters_round_trip() -> None:
    """The pinned config is recorded in the result's parameters block
    so the Lab dossier carries the literature anchors."""
    ctx = _make_pca_integration_context()
    result = bt.run_reversion_with_context(
        ctx, overrides={"signal_mode": "pca_residual"},
    )
    # Pinned constants from spec §2 — every one round-trips.
    params = result.parameters
    assert params["signal_mode"] == "pca_residual"
    assert params["pca_window"] == 252
    assert params["top_k"] == 3
    assert params["ou_half_life_days"] == 30
    assert params["ou_entry_threshold"] == 1.25
    assert params["ou_exit_threshold"] == 0.50
    assert params["pca_group_k"] == 20


def test_I3_pca_residual_rubric_inputs_survivorship_inclusive_false() -> None:
    """Spec §3.2: PCA-residual branch sets
    ``survivorship_inclusive=False`` so the credibility scorer caps
    appropriately. The verdict bar's credibility ≥ 60 floor is the
    operator-side gate; this is the dossier honesty knob.

    We verify by introspecting the call into compute_search_metrics —
    the result's existence + non-crash with the synthetic fixture is
    the wiring proof; the spec assertion lives in the Lab module
    source (the literal True/False kwarg). This test pins that an
    additional rubric_input kwarg flip cannot land silently.
    """
    import inspect

    from reversion import lab_pca_residual as lpc

    # Inspect the source of run_pca_residual_with_context for the
    # binding kwarg — strict source-level check (small, focused).
    src = inspect.getsource(lpc.run_pca_residual_with_context)
    assert "\"survivorship_inclusive\": False" in src, (
        "lab_pca_residual must declare survivorship_inclusive=False "
        "in its rubric_inputs (spec §3.2 — terminal-delisting honesty)"
    )
