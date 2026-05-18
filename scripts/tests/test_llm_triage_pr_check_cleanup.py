"""Tests for the git-worktree cleanup in scripts/llm_triage_pr_check.py.

CRITICAL ISOLATION INVARIANT (PR #61 lesson): every test that touches
git MUST fabricate a THROWAWAY ``git init`` repo in ``tmp_path`` and
operate exclusively there.  NOTHING here ever runs git against the real
working repo at /Users/michael/short-term-trading-engine.

Two properties verified:
  (A) normal path — after _load_specs_base runs, no stale admin entry
      remains in the tmp repo's .git/worktrees/ directory.
  (B) bite / remove-fails path — if ``git worktree remove`` is made a
      no-op (simulating failure/interruption), the defensive ``git
      worktree prune`` fallback STILL removes the stale admin entry, so
      ``git worktree list`` shows only the main worktree.

Test (B) is non-tautological: it FAILS against the original
finally-block-without-prune (pre-fix code: stale .git/worktrees/base_wt
entry persists) and PASSES with the defensive prune added.

Host-guard: both tests assert that the real host repo has no new
``llm_triage_base_`` worktree entry after the test completes.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Load scripts/llm_triage_pr_check.py under a private, collision-safe name
# so ``import`` order with other test modules does not matter.
# ---------------------------------------------------------------------------
_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "llm_triage_pr_check.py"
_SPEC = importlib.util.spec_from_file_location("_pr_check_under_test", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_mod = importlib.util.module_from_spec(_SPEC)
sys.modules["_pr_check_under_test"] = _mod
_SPEC.loader.exec_module(_mod)

# The real host repo — used by host-guard assertions.
_HOST_REPO = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _setup_tmp_repo(tmp: Path) -> Path:
    """Create a minimal git repo at ``tmp`` with one commit, configure it as
    its own origin (so ``origin/main`` resolves), and return the repo root."""
    repo = tmp / "repo"
    repo.mkdir()

    def g(*args: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=str(repo),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    g("init", "-b", "main")
    g("config", "user.email", "test@example.com")
    g("config", "user.name", "Test")
    (repo / "placeholder.txt").write_text("x")
    g("add", "placeholder.txt")
    g("commit", "-m", "init")
    # Self-origin so ``origin/main`` resolves inside the tmp repo.
    g("remote", "add", "origin", str(repo))
    g("fetch", "origin")
    return repo


def _worktree_list_count(repo: Path) -> int:
    """Number of worktrees reported by ``git worktree list`` in *repo*."""
    out = subprocess.run(
        ["git", "worktree", "list"],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return len([ln for ln in out.splitlines() if ln.strip()])


def _worktree_has_llm_triage_entry(repo: Path) -> bool:
    """True if any worktree line references an llm_triage_base_ path."""
    out = subprocess.run(
        ["git", "worktree", "list"],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return any("llm_triage_base_" in ln for ln in out.splitlines())


def _stale_admin_entry_exists(repo: Path) -> bool:
    """True if .git/worktrees/ has any leftover admin dir."""
    wt_dir = repo / ".git" / "worktrees"
    return wt_dir.exists() and any(wt_dir.iterdir())


# ---------------------------------------------------------------------------
# (A) Normal path: cleanup leaves no stale admin entry.
# ---------------------------------------------------------------------------


def test_load_specs_base_cleanup_normal_path(tmp_path: Path) -> None:
    """After a normal _load_specs_base run, no stale worktree admin entry
    persists in the tmp repo."""
    repo = _setup_tmp_repo(tmp_path)

    with patch.object(_mod, "_REPO_ROOT", repo):
        result = _mod._load_specs_base("main")  # succeeds (tpcore on venv PYTHONPATH)

    # Return-contract: must be a non-empty dict keyed by every _REGISTRY_SNIPPETS
    # name — currently {"selfheal", "auditheal"} — derived from the SoT directly.
    expected_keys = {name for name, _ in _mod._REGISTRY_SNIPPETS}
    assert isinstance(result, dict), "_load_specs_base must return a dict"
    assert result, "_load_specs_base must return a non-empty dict"
    assert set(result.keys()) == expected_keys, (
        f"_load_specs_base keys {set(result.keys())!r} != expected {expected_keys!r}"
    )

    # Post-call: only the main worktree remains.
    assert _worktree_list_count(repo) == 1, (
        "Stale worktree admin entry leaked after _load_specs_base"
    )
    assert not _stale_admin_entry_exists(repo)

    # Host guard: real host repo completely unaffected.
    assert not _worktree_has_llm_triage_entry(_HOST_REPO), (
        "_load_specs_base leaked a worktree entry into the real host repo"
    )


# ---------------------------------------------------------------------------
# (B) Bite test: remove is a no-op → prune fallback cleans the admin entry.
#
# Pre-fix behaviour: ``git worktree remove`` is swallowed so the admin dir
# under .git/worktrees/ survives; the assertion FAILS (stale entry present).
# Post-fix behaviour: the defensive ``git worktree prune`` reclaims it;
# the assertion PASSES.
#
# The ``TemporaryDirectory`` context manager deletes the actual worktree
# directory on the filesystem, which marks the git admin entry as
# "prunable" — exactly the condition ``git worktree prune`` is designed
# to reclaim.
# ---------------------------------------------------------------------------


def test_load_specs_base_prune_fallback_when_remove_fails(tmp_path: Path) -> None:
    """If ``git worktree remove`` is a no-op (simulating failure), the
    defensive ``git worktree prune`` fallback MUST clean the stale admin
    entry so no .git/worktrees/<name> dir remains."""
    repo = _setup_tmp_repo(tmp_path)

    real_subprocess_run = subprocess.run

    def _intercept(args, **kwargs):  # noqa: ANN001
        # Swallow ``git worktree remove`` — simulates an interrupted /
        # partially-failed remove that leaves the admin dir intact.
        if isinstance(args, list) and args[:3] == ["git", "worktree", "remove"]:
            class _FakeOk:
                returncode = 0
                stdout = b""
                stderr = b""

            return _FakeOk()
        return real_subprocess_run(args, **kwargs)

    with patch.object(_mod, "_REPO_ROOT", repo):
        with patch("subprocess.run", side_effect=_intercept):
            _mod._load_specs_base("main")

    # The admin entry MUST be gone (prune reclaimed it even though remove
    # was a no-op).  Pre-fix: this assertion FAILS (entry persists).
    assert not _stale_admin_entry_exists(repo), (
        "Stale .git/worktrees/ admin entry persisted after _load_specs_base "
        "even though the worktree path was deleted by TemporaryDirectory. "
        "The defensive `git worktree prune` fallback is missing (pre-fix)."
    )
    assert _worktree_list_count(repo) == 1

    # Host guard: real host repo completely unaffected.
    assert not _worktree_has_llm_triage_entry(_HOST_REPO), (
        "Test leaked a worktree entry into the real host repo!"
    )
