"""Clockwork guard: every test module that loads scripts/ops.py or
mutates sys.modules['ops'] MUST carry the ``xdist_group("ops_shadow")``
mark, else parallel runs go flaky (the ops/ package-shadow is a
single-process invariant; loadgroup keeps the group on one worker).
Mirrors the gen_engine_manifest manifest-discipline."""
from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_TEST_DIRS = ["tests", "tpcore/tests", "scripts/tests"]
_SHADOW_RE = re.compile(
    r"sys\.modules\[[\"']ops[\"']\]|sys\.modules\.get\([\"']ops"
    r"|spec_from_file_location\([^)]*ops|scripts/ops\.py")

def _has_group_mark(src: str) -> bool:
    return 'xdist_group("ops_shadow")' in src or "xdist_group('ops_shadow')" in src

def test_every_ops_shadow_module_is_grouped() -> None:
    missing = []
    for d in _TEST_DIRS:
        for p in (_REPO / d).rglob("test_*.py"):
            src = p.read_text()
            if p.name == "test_xdist_group_manifest.py":
                continue
            if _SHADOW_RE.search(src) and not _has_group_mark(src):
                missing.append(str(p.relative_to(_REPO)))
    assert not missing, (
        "ops-shadow test modules missing xdist_group('ops_shadow') "
        f"(parallel-flaky): {missing}")
