"""SP4 T8 — the docs-match-code gate (H-S4-10).

Asserts the SP4 doc-closure prose against the SHIPPED modules so a
future doc edit claiming a command/state/behavior the code does not
have fails CI. Clauses a–e per the spec hardening register.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
# Evict a non-package ``ops`` (scripts/ops.py) cached by an earlier test in
# full-suite collection order, so ``import ops.*`` resolves the package —
# the scripts/ops.py vs ops/ collision that bit SP2-T9.
for _m in [m for m in list(sys.modules) if m == "ops" or m.startswith("ops.")]:
    if not hasattr(sys.modules[_m], "__path__"):
        del sys.modules[_m]

from tpcore.engine_profile import LifecycleState, roster_for_dispatch  # noqa: E402

CLAUDE = (REPO_ROOT / "CLAUDE.md").read_text()
OPS = (REPO_ROOT / "docs" / "OPERATIONS.md").read_text()
GLOSS = (REPO_ROOT / "docs" / "glossary.md").read_text()


# pytest-xdist: pin this ops-shadow module to one worker so its
# sys.modules['ops'] / scripts/ops.py loading stays single-process
# (the ops/ package-shadow is a single-process invariant). P1.3.
pytestmark = pytest.mark.xdist_group("ops_shadow")


def test_clause_a_entrypoints_import_resolve():
    """(a) python -m ops.engine_sdlc / python -m ops.lab resolve."""
    assert importlib.util.find_spec("ops.engine_sdlc.__main__") is not None
    assert importlib.util.find_spec("ops.lab.__main__") is not None


def test_clause_b_documented_lifecycle_states_match_enum():
    """(b) the documented LAB→PAPER→LIVE→RETIRED == LifecycleState."""
    names = {s.name for s in LifecycleState}
    assert names == {"LAB", "PAPER", "LIVE", "RETIRED"}
    for doc in (CLAUDE, GLOSS):
        assert "LAB → PAPER → LIVE → RETIRED" in doc or \
               "LAB→PAPER→LIVE→RETIRED" in doc, (
            "the documented lifecycle ladder is absent/incorrect")


def test_clause_c_documented_roster_matches_sot():
    """(c) the roster line any doc states == roster_for_dispatch()."""
    sot = " → ".join(roster_for_dispatch())
    assert sot == ("reversion → vector → momentum → sentinel → "
                   "canary → catalyst")
    # CLAUDE.md states the live engines; assert the SoT-derived names
    # all appear in the SDLC entry's engine list.
    for e in roster_for_dispatch():
        assert e in CLAUDE, f"{e} absent from CLAUDE.md SDLC entry"


def test_clause_d_claude_fail_the_gate_honesty_substring():
    """(d) CLAUDE.md states all five engines FAIL the gate (prevents a
    future edit implying a graduation)."""
    assert "all five engines currently FAIL the DSR/credibility gate" in CLAUDE


def test_clause_e_sp3_carry_forwards_provably_unchanged():
    """(e) the recorded SP3 known-limitations still match shipped code:
    _ENGINE_DEFAULT_CONSTS is reversion-only; _validate_modify still
    carries the type(want)(v) coercion line. A future accidental
    fix/regress fails this gate (the known-limitations are provably
    truthful, not aspirational)."""
    import inspect

    from ops.engine_sdlc import planner
    assert set(planner._ENGINE_DEFAULT_CONSTS) == {"reversion"}, (  # noqa: SLF001
        "SP3 carry-forward (a) changed: _ENGINE_DEFAULT_CONSTS is no "
        "longer reversion-only — the docs' known-limitation is now "
        "false (or this is an out-of-scope SP4 fix)")
    vm = inspect.getsource(planner._validate_modify)  # noqa: SLF001
    assert "type(want)(v)" in vm, (
        "SP3 carry-forward (b) changed: the type(want)(v) coercion "
        "line is gone — the docs' known-limitation is now false")


def test_operations_md_re_role_not_delete():
    """H-S4-11: OPERATIONS.md gained the python -m ops.lab canonical
    framing AND still references scripts/search_parameters.py (the
    re-role, NOT a delete)."""
    assert "python -m ops.lab" in OPS
    assert "scripts/search_parameters.py" in OPS
    assert "ops.engine_sdlc" in OPS, (
        "the Engine SDLC section / ECR command is absent from OPERATIONS.md")
