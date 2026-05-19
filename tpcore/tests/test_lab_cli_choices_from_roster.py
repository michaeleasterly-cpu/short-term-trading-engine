"""SP-B — both argparse choices sites are GENERATED from
lab_targetable_engines() (not a literal copy) and importing
ops.lab.__main__ still eager-imports NO engine (spec §2.5, §4.8).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

pytestmark = pytest.mark.xdist_group("ops_shadow")


def test_run_py_engine_choices_are_the_accessor():
    import ops.lab.run as run
    from tpcore.engine_profile import lab_targetable_engines

    a = run._parse_args(["--engine", "reversion"])
    assert a.engine == "reversion"
    # A non-targetable choice is rejected by argparse (SystemExit) ⇒ the
    # choices ARE the accessor, not a stale literal.
    for bad in ("canary", "sigma", "lab"):
        with pytest.raises(SystemExit):
            run._parse_args(["--engine", bad])
    # And every accessor member is accepted by argparse — including the
    # eligible-but-undeclared "sentinel" (argparse accepts; the resolver
    # rejects it later). Accepting "sentinel" is the discriminator that
    # proves the choices ARE the accessor, not the stale literal triple.
    assert "sentinel" in lab_targetable_engines()
    for good in lab_targetable_engines():
        run._parse_args(["--engine", good])


def test_main_py_target_engine_choices_are_the_accessor():
    import ops.lab.__main__ as m
    from tpcore.engine_profile import lab_targetable_engines

    ns = m._parse_args(["--candidate", "c", "--target-engine", "reversion",
                        "--intent", "promote_new"])
    assert ns.target_engine == "reversion"
    for bad in ("canary", "sigma", "lab"):
        with pytest.raises(SystemExit):
            m._parse_args(["--candidate", "c", "--target-engine", bad,
                           "--intent", "promote_new"])
    # Accepting the eligible-but-undeclared "sentinel" is the discriminator
    # that proves the choices ARE the accessor, not the stale literal.
    assert "sentinel" in lab_targetable_engines()
    for good in lab_targetable_engines():
        m._parse_args(["--candidate", "c", "--target-engine", good,
                       "--intent", "promote_new"])


def test_import_ops_lab_main_eager_imports_no_engine():
    """__main__.py:18-20 invariant: import ops.lab.__main__ pulls in NO
    engine package (the choices accessor is engine-free; resolution is
    lazy). Pristine subprocess (zero collection-order pollution)."""
    probe = (
        f"import sys; sys.path.insert(0, {str(REPO_ROOT)!r})\n"
        "import ops.lab.__main__\n"
        "bad=[m for m in sys.modules if m.split('.')[0] in "
        "('reversion','vector','momentum','sentinel','canary')]\n"
        "print(bad)\n"
    )
    out = subprocess.run([sys.executable, "-c", probe],
                         capture_output=True, text=True, cwd=REPO_ROOT)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]", (
        f"eager engine import leaked: {out.stdout!r}")
