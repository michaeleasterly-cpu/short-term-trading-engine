"""SP-B — both argparse choices sites are GENERATED from
lab_targetable_engines() (not a literal copy) and importing
ops.lab.__main__ still eager-imports NO engine (spec §2.5, §4.8).
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

pytestmark = pytest.mark.xdist_group("ops_shadow")


def _capture_built_parser(parse_args_fn, argv: list[str]):
    """Call ``parse_args_fn(argv)`` and return the argparse parser object it
    actually built. ``ops.lab.run._parse_args`` / ``ops.lab.__main__.
    _parse_args`` construct the parser internally and only return the parsed
    Namespace, so we transiently monkeypatch ``ArgumentParser.parse_args`` to
    record ``self`` (the genuine SHIPPED parser instance, with the real
    ``choices=lab_targetable_engines()`` add_argument call) before delegating
    to the original. This pins the shipped parser itself — a SUPERSET drift
    (stale literal re-added alongside the accessor, or ``lab_targetable_
    engines() + extra``) is invisible to accept/reject probing but reds the
    exact-tuple assertion below."""
    captured: dict[str, argparse.ArgumentParser] = {}
    orig = argparse.ArgumentParser.parse_args

    def _spy(self, *a, **kw):
        captured["parser"] = self
        return orig(self, *a, **kw)

    argparse.ArgumentParser.parse_args = _spy
    try:
        parse_args_fn(argv)
    finally:
        argparse.ArgumentParser.parse_args = orig
    return captured["parser"]


def _choices_of(parser: argparse.ArgumentParser, option: str) -> tuple:
    """Return the ``choices`` (as an order-sensitive tuple) of the built
    parser's action whose ``option_strings`` contains ``option``."""
    for act in parser._actions:
        if option in act.option_strings:
            return tuple(act.choices)
    raise AssertionError(f"no argparse action with option {option!r}")


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
    # And every accessor member is accepted by argparse — including
    # "sentinel" (post-SP-E it is both targetable AND declared, and was
    # never in the stale literal triple). Accepting "sentinel" is the
    # discriminator that proves the choices ARE the accessor, not the
    # stale literal triple.
    assert "sentinel" in lab_targetable_engines()
    for good in lab_targetable_engines():
        run._parse_args(["--engine", good])
    # EXACT-equality pin (SP-B T5 code-quality Important #1): introspect the
    # SHIPPED parser run._parse_args built and assert its --engine choices
    # are EXACTLY tuple(lab_targetable_engines()) — order-sensitive tuple
    # equality. Accept/reject alone cannot catch a SUPERSET drift; this can.
    parser = _capture_built_parser(run._parse_args, ["--engine", "reversion"])
    assert _choices_of(parser, "--engine") == tuple(lab_targetable_engines())


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
    # Accepting "sentinel" (post-SP-E targetable AND declared; never in
    # the stale literal) is the discriminator that proves the choices
    # ARE the accessor, not the stale literal.
    assert "sentinel" in lab_targetable_engines()
    for good in lab_targetable_engines():
        m._parse_args(["--candidate", "c", "--target-engine", good,
                       "--intent", "promote_new"])
    # EXACT-equality pin (SP-B T5 code-quality Important #1): introspect the
    # SHIPPED parser m._parse_args built and assert its --target-engine
    # choices are EXACTLY tuple(lab_targetable_engines()) — order-sensitive.
    parser = _capture_built_parser(
        m._parse_args,
        ["--candidate", "c", "--target-engine", "reversion",
         "--intent", "promote_new"],
    )
    assert (
        _choices_of(parser, "--target-engine")
        == tuple(lab_targetable_engines())
    )


def test_import_ops_lab_main_eager_imports_no_engine():
    """__main__.py:18-20 invariant: importing ops.lab.__main__ AND building
    BOTH CLI parsers (the path SP-B actually changed — the
    lab_targetable_engines() call happens at parser build, NOT at import)
    pulls in NO engine package (the choices accessor is engine-free;
    engine resolution stays lazy). Pristine subprocess (zero collection-
    order pollution); the parser-build path is exercised in-subprocess so
    the no-eager-import guarantee actually covers the introduced path."""
    probe = (
        f"import sys; sys.path.insert(0, {str(REPO_ROOT)!r})\n"
        "import ops.lab.__main__ as m\n"
        "import ops.lab.run as run\n"
        # Build BOTH shipped parsers (the SP-B-changed path) with minimal
        # valid argument vectors BEFORE the engine-leak scan.
        "m._parse_args(['--candidate','c','--target-engine','reversion',"
        "'--intent','promote_new'])\n"
        "run._parse_args(['--engine','reversion'])\n"
        "bad=[mod for mod in sys.modules if mod.split('.')[0] in "
        "('reversion','vector','momentum','sentinel','canary')]\n"
        "print(bad)\n"
    )
    out = subprocess.run([sys.executable, "-c", probe],
                         capture_output=True, text=True, cwd=REPO_ROOT)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]", (
        f"eager engine import leaked through parser build: {out.stdout!r}")
