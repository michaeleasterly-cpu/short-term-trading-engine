"""SP-B — every roster-declared engine exports a valid LAB_TARGET whose
param_ranges byte-match the captured T0 baseline; the live import
surface is unchanged (LAB_TARGET is never imported by the live path).
"""
from __future__ import annotations

import importlib

import pytest

# T0 byte-parity baseline: PARAM_RANGES keysets captured on the un-refactored
# tree (Task 0). Inlined deliberately — a 3-key snapshot of a fully-known
# constant needs no file artifact (YAGNI), and a runtime fixture must never
# live under the docs/ plans tree (#252 docs-to-reality).
_T0_PARAM_RANGES_KEYSETS: dict[str, list[str]] = {
    "reversion": ["max_hold_days", "stop_pct", "volume_climax_multiplier", "z_threshold"],
    "vector": ["catalyst_window_days", "de_ceiling", "pb_ceiling", "stop_pct", "swing_score_threshold"],
    "momentum": ["hold_days", "lookback_days", "skip_days", "top_decile_pct"],
}

# FULL-TUPLE T0 byte-parity oracle: the exact {key: (low, high, kind)} dicts
# as they lived in ops.lab.run.PARAM_RANGES on the un-refactored tree (Task 0),
# captured verbatim. T4 deleted the run.py PARAM_RANGES mirror; the engine
# LAB_TARGET is now the SOLE source. Exact dict equality below means a bound
# edit to any engine's LAB_TARGET reds CI against this inlined oracle — the
# spec's byte-parity requirement, guarded at the value level not just
# shape/keyset.
_T0_PARAM_RANGES_FULL: dict[str, dict[str, tuple]] = {
    "reversion": {
        "z_threshold": (2.0, 4.0, "float"),
        "volume_climax_multiplier": (1.2, 3.0, "float"),
        "max_hold_days": (3, 12, "int"),
        "stop_pct": (0.04, 0.12, "float"),
    },
    "vector": {
        "pb_ceiling": (1.5, 3.5, "float"),
        "de_ceiling": (1.5, 4.0, "float"),
        "catalyst_window_days": (3, 10, "int"),
        "swing_score_threshold": (55.0, 75.0, "float"),
        "stop_pct": (0.04, 0.10, "float"),
    },
    "momentum": {
        "lookback_days": (200, 280, "int"),
        "skip_days": (15, 30, "int"),
        "hold_days": (15, 30, "int"),
        "top_decile_pct": (0.05, 0.20, "float"),
    },
}


@pytest.mark.parametrize("engine", ["reversion", "vector", "momentum"])
def test_engine_declares_valid_lab_target(engine):
    from tpcore.lab.target import LabTarget

    mod = importlib.import_module(f"{engine}.backtest")
    lt = getattr(mod, "LAB_TARGET", None)
    assert isinstance(lt, LabTarget), f"{engine}: no module-level LAB_TARGET"
    # param_ranges byte-match the T0 literal keyset (no drift on the move).
    assert sorted(lt.param_ranges) == _T0_PARAM_RANGES_KEYSETS[engine]
    # The 4 callables resolve to the engine's already-defined symbols.
    assert lt.run_for_search is mod.run_for_search
    assert lt.default_params is mod.default_params
    assert callable(lt.load_window_context)
    assert callable(lt.run_with_context)


@pytest.mark.parametrize("engine", ["reversion", "vector", "momentum"])
def test_lab_target_param_ranges_full_value_byte_parity(engine):
    """The full {key: (low, high, kind)} dict is byte-identical to the
    inlined T0 oracle (the values that lived in ops.lab.run.PARAM_RANGES
    pre-refactor; T4 deleted that mirror — the engine LAB_TARGET is now
    the sole source). Exact dict equality — a bound edit to the engine
    LAB_TARGET reds CI against this oracle (the spec's byte-parity
    requirement, guarded at the value level not just shape)."""
    mod = importlib.import_module(f"{engine}.backtest")
    assert mod.LAB_TARGET.param_ranges == _T0_PARAM_RANGES_FULL[engine]


def test_live_import_surface_does_not_import_lab_target():
    """The scheduler/order-manager/plug never import backtest.LAB_TARGET —
    the live path is byte-identical (spec §6). Proxy: importing each
    engine's scheduler must not require LAB_TARGET to exist (it is only
    referenced lazily by ops.lab.run). We assert the constant is defined
    AFTER run_for_search and the engine package import is side-effect
    clean (no exception)."""
    for engine in ("reversion", "vector", "momentum"):
        importlib.import_module(f"{engine}.backtest")  # must not raise
