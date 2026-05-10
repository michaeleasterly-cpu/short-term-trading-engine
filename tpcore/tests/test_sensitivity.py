"""Tests for `tpcore.backtest.sensitivity`."""
from __future__ import annotations

import pytest

from tpcore.backtest.sensitivity import (
    ParameterPoint,
    sweep_parameter,
)


def _flat_backtest(metric_value: float) -> callable:
    """Return a backtest_fn that produces the same metrics regardless of param."""
    def fn(param_value):  # type: ignore[no-untyped-def]
        return {"profit_factor": metric_value, "sharpe": 0.5, "win_rate": 0.55, "max_drawdown": -0.1}
    return fn


def _spikey_backtest() -> callable:
    """Backtest where profit_factor varies with the param — looks overfit."""
    def fn(param_value: float) -> dict:
        # PF spikes near param=0.5, low elsewhere.
        pf = 3.0 if abs(param_value - 0.5) < 0.05 else 0.5
        return {"profit_factor": pf, "sharpe": 0.0, "win_rate": 0.5, "max_drawdown": -0.1}
    return fn


def test_sweep_runs_backtest_for_each_param_value() -> None:
    fn = _flat_backtest(1.5)
    result = sweep_parameter(fn, "z_threshold", [2.0, 2.5, 3.0])
    assert result.param_name == "z_threshold"
    assert len(result.points) == 3
    assert all(isinstance(p, ParameterPoint) for p in result.points)
    assert result.points[0].param_value == 2.0
    assert result.points[2].param_value == 3.0


def test_flat_surface_has_low_flatness_score() -> None:
    """Constant profit factor → flatness ~ 0; surface is robust."""
    result = sweep_parameter(_flat_backtest(1.5), "z", [2.0, 2.5, 3.0, 3.5])
    assert result.flatness_score < 0.05  # near zero (no variation)
    assert result.is_flat


def test_spikey_surface_has_high_flatness_score() -> None:
    """A surface with one big spike → high flatness score, not robust."""
    result = sweep_parameter(_spikey_backtest(), "z", [0.1, 0.3, 0.5, 0.7, 0.9])
    assert result.flatness_score > 0.5
    assert not result.is_flat


def test_metrics_preserved_per_point() -> None:
    fn = _flat_backtest(2.0)
    result = sweep_parameter(fn, "x", [1, 2, 3])
    for p in result.points:
        assert p.metrics["profit_factor"] == 2.0
        assert "sharpe" in p.metrics
        assert "win_rate" in p.metrics
        assert "max_drawdown" in p.metrics


def test_empty_param_values_raises() -> None:
    with pytest.raises(ValueError):
        sweep_parameter(_flat_backtest(1.0), "x", [])


def test_zero_mean_pf_returns_inf_flatness() -> None:
    """Pathological case: mean PF == 0 → flatness undefined; we report inf."""
    def fn(p):  # type: ignore[no-untyped-def]
        return {"profit_factor": 0.0, "sharpe": 0.0, "win_rate": 0.0, "max_drawdown": 0.0}
    result = sweep_parameter(fn, "x", [1, 2, 3])
    assert result.flatness_score == float("inf")
    assert not result.is_flat
