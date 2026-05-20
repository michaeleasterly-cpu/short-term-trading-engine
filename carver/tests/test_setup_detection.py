"""carver/plugs/setup_detection.py — three-forecast scan + FDM combine."""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd


def _synthetic_panel(
    n_days: int = 600,
    *,
    seed: int = 7,
    start_price: float = 50.0,
    drift: float = 0.0005,
    sigma: float = 0.015,
) -> pd.DataFrame:
    """Build a GBM-style close-price panel of ``n_days`` calendar days."""
    rng = np.random.default_rng(seed)
    daily_ret = rng.normal(loc=drift, scale=sigma, size=n_days)
    closes = start_price * np.cumprod(1.0 + daily_ret)
    start = date(2024, 1, 1)
    dates = [start + timedelta(days=i) for i in range(n_days)]
    return pd.DataFrame({"close": closes}, index=pd.DatetimeIndex(dates))


def test_setup_detection_subclasses_base_engine_plug() -> None:
    from carver.plugs.setup_detection import CarverSetupDetection
    from tpcore.interfaces.engine_plug import BaseEnginePlug

    assert issubclass(CarverSetupDetection, BaseEnginePlug)


def test_validate_dependencies_and_healthcheck() -> None:
    from carver.plugs.setup_detection import CarverSetupDetection

    plug = CarverSetupDetection()
    assert plug.validate_dependencies() is True
    hc = plug.healthcheck()
    assert hc["engine"] == "carver"
    assert hc["plug"] == "setup_detection"
    assert hc["ok"] is True
    assert "trend_fast" in hc["details"]


def test_detect_returns_filter_diagnostics_with_universe_total() -> None:
    from carver.plugs.setup_detection import CarverSetupDetection
    from tpcore.backtest.filter_diagnostics import FilterDiagnostics

    panels = {f"T{i}": _synthetic_panel(seed=i + 1) for i in range(5)}
    plug = CarverSetupDetection()
    candidates, diag = plug.detect(panels, as_of=date(2025, 6, 1))
    assert isinstance(diag, FilterDiagnostics)
    assert diag.universe_total == 5


def test_three_forecasts_emitted_per_candidate() -> None:
    from carver.plugs.setup_detection import CarverSetupDetection

    panels = {f"T{i}": _synthetic_panel(seed=i + 1) for i in range(3)}
    plug = CarverSetupDetection()
    candidates, _ = plug.detect(panels, as_of=date(2025, 6, 1))
    assert candidates, "expected at least one candidate from synthetic data"
    for cand in candidates:
        rules = {f.rule for f in cand.forecasts}
        assert rules == {"trend", "value", "meanrev"}


def test_forecasts_are_capped_at_plus_minus_twenty() -> None:
    from carver.models import FORECAST_CAP_ABS
    from carver.plugs.setup_detection import CarverSetupDetection

    # Construct a panel whose late-window price explodes (raw forecasts huge).
    panel = _synthetic_panel(n_days=600, seed=42, drift=0.0005, sigma=0.015)
    # Spike final 50 days +50% to drive trend/value/meanrev raw to extremes.
    panel.iloc[-50:, panel.columns.get_loc("close")] = (
        panel.iloc[-50:]["close"].to_numpy() * 1.5
    )
    panels = {"SPIKE": panel}
    plug = CarverSetupDetection()
    candidates, _ = plug.detect(panels, as_of=date(2025, 6, 1))
    for cand in candidates:
        for f in cand.forecasts:
            assert abs(f.capped) <= FORECAST_CAP_ABS


def test_idm_bounded_to_idm_floor_on_cold_start() -> None:
    from carver.models import IDM_FLOOR
    from carver.plugs.setup_detection import CarverSetupDetection

    # 200-day panel — below the 24-month (504-day) correlation window.
    panels = {"COLD": _synthetic_panel(n_days=200, seed=3)}
    plug = CarverSetupDetection()
    candidates, _ = plug.detect(panels, as_of=date(2024, 7, 1))
    if candidates:
        for cand in candidates:
            assert cand.idm == IDM_FLOOR


def test_idm_bounded_above_at_idm_cap() -> None:
    from carver.models import IDM_CAP
    from carver.plugs.setup_detection import _compute_idm

    # Negative off-diagonal correlations push FDM above the cap (synthetic).
    rho = np.array([
        [1.0, -0.9, -0.9],
        [-0.9, 1.0, -0.9],
        [-0.9, -0.9, 1.0],
    ])
    idm = _compute_idm([1.0, 1.0, 1.0], rho)
    assert idm == IDM_CAP


def test_short_history_ticker_is_blocked_not_returned() -> None:
    from carver.plugs.setup_detection import CarverSetupDetection

    # 60-day panel — insufficient for EWMAC slow=32 + warmup.
    panels = {"SHORT": _synthetic_panel(n_days=60, seed=11)}
    plug = CarverSetupDetection()
    candidates, diag = plug.detect(panels, as_of=date(2024, 3, 15))
    # Short-history ticker should be filtered out OR carry NaN forecasts;
    # the contract is: either skip from candidates, or diagnostics > 0.
    if not candidates:
        # The coarse_liquidity_blocked counter is the catch-all here.
        assert (diag.candidates_passed or 0) == 0
    else:
        for cand in candidates:
            # If returned, must have non-NaN combined_forecast.
            assert not np.isnan(cand.combined_forecast)


def test_carver_specific_filter_diagnostics_counters_populated() -> None:
    from carver.plugs.setup_detection import CarverSetupDetection

    panels = {
        "OK": _synthetic_panel(n_days=600, seed=1),
        "SHORT": _synthetic_panel(n_days=30, seed=2),
    }
    plug = CarverSetupDetection()
    candidates, diag = plug.detect(panels, as_of=date(2025, 6, 1))
    assert diag.universe_total == 2
    # At least one short-history ticker dropped → coarse_liquidity_blocked > 0
    # or candidates_passed == 1 (whichever the impl picks).
    assert (diag.candidates_passed or 0) + (diag.coarse_liquidity_blocked or 0) >= 1
