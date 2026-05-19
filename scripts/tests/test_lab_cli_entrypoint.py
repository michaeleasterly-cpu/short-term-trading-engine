"""SDLC SP2 T10 — ``python -m ops.lab`` CLI entrypoint.

Asserts the canonical entrypoint shape (``__main__`` + argparse + a
``main`` shim) and the two fail-loud invariants that need NO DB:

* a no-DSN invocation returns an explicit non-zero rc (NOT a silent 0 —
  the canary ``-m``-no-op lesson) with an error surfaced;
* a path-traversal ``--candidate`` fails loud (LabCandidate pattern
  ValidationError → rc 1), before any DB work.

No real Lab runs here — there is no DB; every assertion is reachable
purely from the entrypoint / candidate-validation / no-DSN paths. The
DB-dependent walk-forward is never entered (no DSN ⇒ early return; bad
candidate ⇒ even earlier return).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
# Evict a non-package ``ops`` (scripts/ops.py) cached by an earlier test in
# full-suite collection order, so ``import ops.lab.__main__`` resolves the
# package — the scripts/ops.py vs ops/ collision that bit SP2-T9.
for _m in [m for m in list(sys.modules) if m == "ops" or m.startswith("ops.")]:
    if not hasattr(sys.modules[_m], "__path__"):
        del sys.modules[_m]

MAIN_PY = REPO_ROOT / "ops" / "lab" / "__main__.py"


# pytest-xdist: pin this ops-shadow module to one worker so its
# sys.modules['ops'] / scripts/ops.py loading stays single-process
# (the ops/ package-shadow is a single-process invariant). P1.3.
pytestmark = pytest.mark.xdist_group("ops_shadow")


def test_entrypoint_file_has_canonical_cli_shape():
    src = MAIN_PY.read_text()
    assert 'if __name__ == "__main__":' in src
    assert "argparse" in src
    assert "def main()" in src


def test_no_dsn_returns_explicit_nonzero_not_silent_zero(monkeypatch, capsys):
    """env DATABASE_URL unset + no --db-url ⇒ rc == 1 + an error printed.
    The Lab must NEVER silently exit 0 with no DSN."""
    import ops.lab.__main__ as cli

    monkeypatch.delenv("DATABASE_URL", raising=False)
    rc = asyncio.run(cli._amain([
        "--candidate", "exp1",
        "--target-engine", "reversion",
        "--intent", "fold_existing",
    ]))
    assert rc == 1, "no-DSN must return explicit non-zero, not silent 0"
    captured = capsys.readouterr()
    assert "DATABASE_URL" in captured.err


def test_bad_candidate_name_fails_loud(monkeypatch, capsys):
    """A path-traversal --candidate fails loud (LabCandidate pattern
    rejects it) with a non-zero rc, even with a DSN present — the
    candidate is built + validated BEFORE any DB work."""
    import ops.lab.__main__ as cli

    monkeypatch.setenv("DATABASE_URL", "postgres://unused/forbidden")
    rc = asyncio.run(cli._amain([
        "--candidate", "../../etc/x",
        "--target-engine", "reversion",
        "--intent", "fold_existing",
    ]))
    assert rc != 0, "a bad candidate name must fail loud, not pass"
    captured = capsys.readouterr()
    assert "invalid Lab candidate" in captured.err


def test_bad_param_overrides_json_fails_loud(monkeypatch, capsys):
    """Non-dict / unparseable --param-overrides fails loud before DB."""
    import ops.lab.__main__ as cli

    monkeypatch.setenv("DATABASE_URL", "postgres://unused/forbidden")
    rc = asyncio.run(cli._amain([
        "--candidate", "exp1",
        "--target-engine", "reversion",
        "--intent", "fold_existing",
        "--param-overrides", "[1, 2, 3]",
    ]))
    assert rc == 1
    assert "invalid Lab candidate" in capsys.readouterr().err


def test_help_exits_zero():
    """argparse --help works (rc 0 via SystemExit(0))."""
    import ops.lab.__main__ as cli

    with pytest.raises(SystemExit) as ei:
        cli._parse_args(["--help"])
    assert ei.value.code == 0


def test_importing_main_does_not_eager_import_an_engine():
    """H-S2-1: importing the entrypoint must NOT pull in any engine
    package — engine imports stay lazy in ops.lab.run function-locals."""
    import importlib

    for mod in ("reversion", "vector", "momentum"):
        sys.modules.pop(mod, None)
    importlib.import_module("ops.lab.__main__")
    for mod in ("reversion", "vector", "momentum"):
        assert mod not in sys.modules, (
            f"import ops.lab.__main__ eager-imported {mod!r} — "
            "engine imports must stay lazy (H-S2-1)"
        )
