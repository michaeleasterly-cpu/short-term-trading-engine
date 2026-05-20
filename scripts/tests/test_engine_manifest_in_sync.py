"""SP4 T3 — the --check CI-divergence gate (H-S4-5/9).

Invokes scripts/gen_engine_manifest.py --check as a SUBPROCESS (the
faithful CI shape; a fresh interpreter side-steps the scripts/ops.py↔ops
sys.modules collision entirely). An in-fence hand-edit fails --check;
an out-of-fence hand-edit passes.
"""
from __future__ import annotations

import shutil
import subprocess
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

GEN = "scripts/gen_engine_manifest.py"


# pytest-xdist: pin this ops-shadow module to one worker so its
# sys.modules['ops'] / scripts/ops.py loading stays single-process
# (the ops/ package-shadow is a single-process invariant). P1.3.
pytestmark = pytest.mark.xdist_group("ops_shadow")


def _check(cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603
        [sys.executable, GEN, "--check"],
        cwd=str(cwd), capture_output=True, text=True)


def test_check_clean_on_committed_tree():
    res = _check(REPO_ROOT)
    assert res.returncode == 0, (
        f"--check RED on the committed tree (the shadows must already "
        f"be in sync):\n{res.stdout}\n{res.stderr}")


def _staged_copy(tmp_path: Path) -> Path:
    staged = tmp_path / "tree"
    shutil.copytree(
        REPO_ROOT, staged,
        ignore=shutil.ignore_patterns(
            ".git", ".venv", "__pycache__", "backtests"))
    return staged


def test_hand_edit_in_fence_fails_check(tmp_path):
    staged = _staged_copy(tmp_path)
    smoke = staged / "scripts" / "run_smoke_test.sh"
    txt = smoke.read_text()
    smoke.write_text(txt.replace(
        "for engine in reversion vector momentum sentinel canary catalyst; do",
        "for engine in reversion vector momentum; do"))  # in-fence drift
    res = _check(staged)
    assert res.returncode != 0, "an in-fence hand-edit must fail --check"
    assert "run_smoke_test.sh" in (res.stdout + res.stderr), (
        "the unified diff must name the drifted file")


def test_hand_edit_out_of_fence_passes_check(tmp_path):
    staged = _staged_copy(tmp_path)
    smoke = staged / "scripts" / "run_smoke_test.sh"
    txt = smoke.read_text()
    # mutate a line that is NOT inside any fenced region (the shebang
    # comment block at the very top).
    smoke.write_text(txt.replace(
        "# Platform-wide smoke test — covers every engine + shared services.",
        "# Platform-wide smoke test — covers every engine + services (edited)."))
    res = _check(staged)
    assert res.returncode == 0, (
        f"an out-of-fence edit must NOT fail --check:\n{res.stdout}\n"
        f"{res.stderr}")


def test_collision_preemption_stanza_present():
    """H-S4-9: every SP4 scripts/tests file *that imports ops/the
    generator* carries the proven sys.modules eviction loop verbatim.

    ``test_sp4_scope_confined.py`` is DELIBERATELY excluded: like its
    proven SP3 sibling ``scripts/tests/test_sp3_scope_confined.py`` it
    imports ONLY stdlib + pytest and shells read-only ``git`` — it never
    resolves ``ops.*`` and so does not need (and must not carry) the
    stanza. Per H-S4-9 the stanza is for files that import ops/the
    generator; carrying it in the pure scope-gate would needlessly
    mutate global ``sys.modules`` at collection time and perturb the
    locked SP2 oracle (the §13.2(d) ``_FakePool`` collection-order
    fragility). Its own scope-confinement is asserted by
    ``test_sp4_scope_confined.py`` itself."""
    needle = ('for _m in [m for m in list(sys.modules) if m == "ops" '
              'or m.startswith("ops.")]:')
    for fn in ("test_engine_manifest_in_sync.py",
               "test_gen_engine_manifest_render.py",
               "test_sdlc_docs_match_code.py"):
        p = REPO_ROOT / "scripts" / "tests" / fn
        if not p.is_file():
            continue  # lands in its own task; asserted there too
        assert needle in p.read_text(), (
            f"{fn}: missing the H-S4-9 collision-eviction stanza")
