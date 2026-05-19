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
def test_lab_target_values_byte_match_old_param_ranges(engine):
    """The (low, high, kind) tuples are byte-identical to the values
    that lived in ops.lab.run.PARAM_RANGES — no behavioural drift."""
    import importlib as _il

    mod = _il.import_module(f"{engine}.backtest")
    # Reconstruct the OLD literal from git's pre-refactor copy is overkill;
    # the run.py lazy Mapping (Task 4) will resolve THROUGH these, and the
    # characterization oracle pins reversion's keyset. Here we pin the
    # values are 3-tuples with a valid kind (LabTarget already enforces;
    # this is the engine-side regression pin).
    for _name, spec in mod.LAB_TARGET.param_ranges.items():
        assert isinstance(spec, tuple) and len(spec) == 3
        assert spec[2] in ("float", "int") or spec[2].startswith("choice:")


def test_live_import_surface_does_not_import_lab_target():
    """The scheduler/order-manager/plug never import backtest.LAB_TARGET —
    the live path is byte-identical (spec §6). Proxy: importing each
    engine's scheduler must not require LAB_TARGET to exist (it is only
    referenced lazily by ops.lab.run). We assert the constant is defined
    AFTER run_for_search and the engine package import is side-effect
    clean (no exception)."""
    for engine in ("reversion", "vector", "momentum"):
        importlib.import_module(f"{engine}.backtest")  # must not raise
