"""SP-D §2.1 — the engine-free LabPrimaryMetric vocabulary + the
defaulted LabTarget.primary_metric field. tpcore stays engine-free
(only `enum` added vs today)."""
from __future__ import annotations

import pytest
from pydantic import ValidationError


def _callables():
    async def _afn(*a, **k):
        return None

    def _sfn(*a, **k):
        return None

    def _dp() -> dict:
        return {}

    return _afn, _sfn, _dp


def test_enum_members_and_str_values():
    from tpcore.lab.target import LabPrimaryMetric

    assert LabPrimaryMetric.SHARPE == "sharpe"
    assert LabPrimaryMetric.MAXDD_REDUCTION == "maxdd_reduction"
    assert LabPrimaryMetric.ULCER == "ulcer"
    assert LabPrimaryMetric.INVERSE_ETF_HOLD == "inverse_etf_hold"
    # StrEnum -> serializes as a plain string for the dossier JSON.
    assert isinstance(LabPrimaryMetric.SHARPE.value, str)


def test_vocabulary_is_exactly_pinned():
    """Persisted-value contract: ``LabPrimaryMetric`` member NAMES and
    string VALUES are written into ``LabResult`` JSON sidecars and
    compared in the make-or-break. This asserts the enum is EXACTLY
    these four (name, value) pairs — no more, no fewer, none renamed.
    Any add/rename/remove is a deliberate persisted-state migration and
    MUST be a conscious edit to this set, never an incidental enum
    change; this test reds the build until that edit is made.
    """
    from tpcore.lab.target import LabPrimaryMetric

    assert {(m.name, m.value) for m in LabPrimaryMetric} == {
        ("SHARPE", "sharpe"),
        ("MAXDD_REDUCTION", "maxdd_reduction"),
        ("ULCER", "ulcer"),
        ("INVERSE_ETF_HOLD", "inverse_etf_hold"),
    }


def test_labtarget_primary_metric_defaults_to_sharpe():
    from tpcore.lab.target import LabPrimaryMetric, LabTarget

    afn, sfn, dp = _callables()
    t = LabTarget(param_ranges={"z": (2.0, 4.0, "float")},
                  run_for_search=afn, load_window_context=afn,
                  run_with_context=sfn, default_params=dp)
    assert t.primary_metric == LabPrimaryMetric.SHARPE


def test_labtarget_accepts_explicit_metric():
    from tpcore.lab.target import LabPrimaryMetric, LabTarget

    afn, sfn, dp = _callables()
    t = LabTarget(param_ranges={"z": (2.0, 4.0, "float")},
                  run_for_search=afn, load_window_context=afn,
                  run_with_context=sfn, default_params=dp,
                  primary_metric=LabPrimaryMetric.MAXDD_REDUCTION)
    assert t.primary_metric == LabPrimaryMetric.MAXDD_REDUCTION


def test_labtarget_rejects_unknown_metric_string():
    """extra='forbid' + closed StrEnum -> a misspelled metric is a
    pydantic ValidationError at declaration (§8-A8, fail-loud, never a
    silent Sharpe fallback)."""
    from tpcore.lab.target import LabTarget

    afn, sfn, dp = _callables()
    with pytest.raises(ValidationError):
        LabTarget(param_ranges={"z": (2.0, 4.0, "float")},
                  run_for_search=afn, load_window_context=afn,
                  run_with_context=sfn, default_params=dp,
                  primary_metric="shrpe")


def test_target_module_still_engine_free():
    """tpcore/lab/target.py imports only pydantic + stdlib (now incl.
    `enum`) — no engine, no ops edge."""
    import ast
    from pathlib import Path

    tree = ast.parse(Path("tpcore/lab/target.py").read_text())
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module.split(".")[0])
    for bad in ("reversion", "vector", "momentum", "sentinel",
                "canary", "ops"):
        assert bad not in mods
    assert mods <= {"__future__", "collections", "typing", "pydantic",
                    "enum"}
