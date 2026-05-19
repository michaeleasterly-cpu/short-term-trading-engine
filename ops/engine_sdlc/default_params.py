"""Lazy per-engine default_params() dispatcher (SP3 O1, spec §7.1).

SP-B: the if-engine== ladder is replaced by a thin delegate to
ops.lab.run._lab_target_for (the single roster-SoT resolver). The engine
import stays LAZY (ops→ops, no eager engine import, no tpcore→engine).
"""
from __future__ import annotations

from typing import Any


def default_params(engine: str) -> dict[str, Any]:
    from ops.lab.run import _lab_target_for  # lazy, ops→ops, legal

    return _lab_target_for(engine).default_params()


__all__ = ["default_params"]
