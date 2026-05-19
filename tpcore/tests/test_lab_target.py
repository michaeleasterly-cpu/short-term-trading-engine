"""SP-B — LabTarget engine-free contract: declaration-time fail-loud
validation of the (low, high, kind) tuple/kind contract.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError


def _callables():
    async def _afn(*a, **k):  # run_for_search / load_window_context
        return None

    def _sfn(*a, **k):  # run_with_context
        return None

    def _dp() -> dict:  # default_params
        return {}

    return _afn, _sfn, _dp


def test_labtarget_accepts_valid_declaration():
    from tpcore.lab.target import LabTarget

    afn, sfn, dp = _callables()
    t = LabTarget(
        param_ranges={"z": (2.0, 4.0, "float"), "n": (3, 12, "int"),
                      "m": (0, 1, "choice:a,b")},
        run_for_search=afn,
        load_window_context=afn,
        run_with_context=sfn,
        default_params=dp,
    )
    assert t.param_ranges["z"] == (2.0, 4.0, "float")
    assert callable(t.run_for_search)
    assert callable(t.default_params)


def test_labtarget_is_frozen_and_extra_forbid():
    from tpcore.lab.target import LabTarget

    afn, sfn, dp = _callables()
    t = LabTarget(param_ranges={"z": (2.0, 4.0, "float")},
                  run_for_search=afn, load_window_context=afn,
                  run_with_context=sfn, default_params=dp)
    with pytest.raises(ValidationError):  # pydantic frozen instance
        t.param_ranges = {}
    with pytest.raises(ValidationError):  # extra="forbid"
        LabTarget(param_ranges={}, run_for_search=afn,
                  load_window_context=afn, run_with_context=sfn,
                  default_params=dp, bogus=1)


@pytest.mark.parametrize("bad", [
    {"z": (2.0, 4.0)},                       # 2-tuple, not 3
    {"z": (2.0, 4.0, "floar")},              # typo kind
    {"z": (2.0, 4.0, "choice")},             # choice w/o ":"
    {"z": (2.0, 4.0, 7)},                    # kind not str
    {"z": (0, 1, "choice:")},                # empty CSV → [''] silent corruption
    {"z": (0, 1, "choice:,")},               # all-empty members
])
def test_labtarget_rejects_malformed_param_ranges_at_construction(bad):
    """Fail-loud at DECLARATION time (model_post_init), not at sample
    time on a live-money-adjacent path (spec §2.2). pydantic v2 wraps the
    ``model_post_init`` ``ValueError`` in ``pydantic.ValidationError``."""
    from tpcore.lab.target import LabTarget

    afn, sfn, dp = _callables()
    with pytest.raises(ValidationError):
        LabTarget(param_ranges=bad, run_for_search=afn,
                  load_window_context=afn, run_with_context=sfn,
                  default_params=dp)


def test_labtarget_module_is_engine_free():
    """tpcore/lab/target.py imports only pydantic + stdlib — no engine,
    no tpcore→engine edge (check_imports stays green)."""
    import ast
    from pathlib import Path

    src = Path("tpcore/lab/target.py").read_text()
    tree = ast.parse(src)
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module.split(".")[0])
    for engine in ("reversion", "vector", "momentum", "sentinel", "canary"):
        assert engine not in mods, f"target.py must not import {engine}"
