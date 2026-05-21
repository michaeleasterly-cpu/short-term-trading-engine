"""Parameter sensitivity sweep — flat surface = robust, spikey = overfit.

Re-runs a backtest at each candidate parameter value and reports the
performance surface plus a single *flatness score* (CV of profit factor
across the sweep). The threshold logic mirrors common quant practice:

    flatness < 0.20  → robust; the strategy isn't sitting on a knife-edge.
    flatness > 0.50  → overfit; the parameter was tuned to a sweet spot.

The caller supplies a thunk that takes one parameter value and returns a
metrics dict. That keeps this module independent of any specific
backtest harness.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

# CV (std/mean) below this is treated as a flat surface — robust strategy.
FLATNESS_ROBUST_THRESHOLD = 0.20


@dataclass(frozen=True)
class ParameterPoint:
    """One sample from the parameter sweep."""

    param_value: object
    metrics: dict[str, float]


@dataclass(frozen=True)
class SensitivityResult:
    """Output of a single-parameter sweep."""

    param_name: str
    points: list[ParameterPoint]
    flatness_score: float = field(metadata={"doc": "CV of profit factor across points"})

    @property
    def is_flat(self) -> bool:
        """True iff the surface is flat enough to consider robust."""
        return self.flatness_score < FLATNESS_ROBUST_THRESHOLD


def sweep_parameter(
    backtest_fn: Callable[[object], dict[str, float]],
    param_name: str,
    param_values: list,
    *,
    flatness_metric: str = "profit_factor",
) -> SensitivityResult:
    """Run ``backtest_fn`` at each value in ``param_values`` and return the surface.

    ``backtest_fn`` must return a dict with keys ``profit_factor``,
    ``sharpe``, ``win_rate``, and ``max_drawdown`` (or whichever metric is
    named in ``flatness_metric``). The flatness score is computed as the
    coefficient of variation (``std / |mean|``) of the chosen metric. A
    strategy whose chosen metric varies wildly across nearby parameter
    values is overfit; one whose metric is stable is robust.
    """
    if not param_values:
        raise ValueError("param_values must be non-empty")

    points: list[ParameterPoint] = []
    metric_series: list[float] = []
    for v in param_values:
        metrics = backtest_fn(v)
        points.append(ParameterPoint(param_value=v, metrics=dict(metrics)))
        metric_series.append(float(metrics[flatness_metric]))

    arr = np.asarray(metric_series, dtype=float)
    mean = float(arr.mean())
    if mean == 0:
        flatness = float("inf")
    else:
        flatness = float(arr.std(ddof=0) / abs(mean))

    return SensitivityResult(
        param_name=param_name,
        points=points,
        flatness_score=flatness,
    )


__all__ = [
    "FLATNESS_ROBUST_THRESHOLD",
    "ParameterPoint",
    "SensitivityResult",
    "sweep_parameter",
]
