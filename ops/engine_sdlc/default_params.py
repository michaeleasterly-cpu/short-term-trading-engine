"""Lazy per-engine default_params() dispatcher (SP3 O1, spec §7.1).

Exact parity with ops.lab.run._runner_for: the engine import is LAZY,
inside the function body, so this module (and anything importing it)
never eager-imports an engine — and there is NEVER a tpcore→engine
import (the dispatcher lives in ops/, legal here, H-S3-10).
"""
from __future__ import annotations

from typing import Any


def default_params(engine: str) -> dict[str, Any]:
    if engine == "reversion":
        from reversion.backtest import default_params as dp
        return dp()
    if engine == "vector":
        from vector.backtest import default_params as dp
        return dp()
    if engine == "momentum":
        from momentum.backtest import default_params as dp
        return dp()
    raise ValueError(f"unknown engine: {engine}")


__all__ = ["default_params"]
