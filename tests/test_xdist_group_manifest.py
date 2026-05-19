"""Clockwork guard: every test module that loads scripts/ops.py or an
``ops/`` package module, or mutates sys.modules['ops'], MUST carry the
``xdist_group("ops_shadow")`` mark, else parallel runs go flaky (the
ops/ package-shadow is a single-process invariant; loadgroup keeps the
group on one worker). Mirrors the gen_engine_manifest manifest-discipline.

Conservative over-inclusion is the correct live-money choice here:
over-grouping merely co-locates a module on one xdist worker (mild
parallelism loss); under-grouping is the dangerous parallel-flaky case.
So the matcher is a SUPERSET — any test module that (a) mutates/reads
sys.modules['ops'], OR (b) ``spec_from_file_location``-loads (single- OR
multi-line) a path with an ``ops``/``scripts/ops.py`` segment, OR (c)
``importlib``-loads an ``ops/`` module. P1.1 fix: the old
``spec_from_file_location\\([^)]*ops`` stopped at the FIRST ``)`` so a
multi-line call whose path is built like ``Path(...) / "ops" / "x.py"``
on a continuation line evaded the guard AND a human grep."""
from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_TEST_DIRS = ["tests", "tpcore/tests", "scripts/tests"]

# An ``ops``/``scripts/ops.py`` path segment as it appears inside a
# spec_from_file_location / importlib path expression. Covers a quoted
# path component ("ops"), a slash-joined literal (ops/x.py or
# scripts/ops.py), and a pathlib-join component (/ "ops" / ...).
_OPS_PATH_TOKEN = (
    r"""(?:["']ops["']"""              # "ops" path component
    r"""|/\s*["']ops["']"""            # ... / "ops"
    r"""|["']scripts["']\s*/\s*["']ops\.py["']"""  # "scripts" / "ops.py"
    r"""|\bscripts/ops\.py\b"""        # scripts/ops.py literal
    r"""|\bops/[\w/]+\.py\b)"""        # ops/<module>.py literal
)

# DOTALL so the bounded window between the call head and the ops token
# spans continuation lines. Non-greedy + capped so it can't wander into
# an unrelated later call.
_SHADOW_RE = re.compile(
    r"""sys\.modules\[\s*["']ops["']\s*\]"""          # sys.modules['ops']
    r"""|sys\.modules\.get\(\s*["']ops["']"""         # sys.modules.get('ops'
    r"""|(?:spec_from_file_location|importlib\.import_module"""
    r"""|importlib\.util\.spec_from_file_location)"""
    r"""\s*\([\s\S]{0,400}?""" + _OPS_PATH_TOKEN,     # multi-line load
    re.DOTALL,
)


def _has_group_mark(src: str) -> bool:
    return 'xdist_group("ops_shadow")' in src or "xdist_group('ops_shadow')" in src


def test_every_ops_shadow_module_is_grouped() -> None:
    missing = []
    for d in _TEST_DIRS:
        for p in (_REPO / d).rglob("test_*.py"):
            if p.name == "test_xdist_group_manifest.py":
                # Self-exempt: this file's own pattern strings contain
                # the very tokens it scans for.
                continue
            src = p.read_text()
            if _SHADOW_RE.search(src) and not _has_group_mark(src):
                missing.append(str(p.relative_to(_REPO)))
    assert not missing, (
        "ops-shadow test modules missing xdist_group('ops_shadow') "
        f"(parallel-flaky): {missing}")
