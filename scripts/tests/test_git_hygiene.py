"""Tests for scripts/git_hygiene.sh — the canonical git-maintenance
wrapper (CLAUDE.md Session Rules / docs/STYLE_GUIDE.md "Git hygiene").

CRITICAL ISOLATION INVARIANT: every test here fabricates a THROWAWAY
git repo entirely inside ``tmp_path`` and drives the real script there
via ``subprocess`` with ``cwd=`` that throwaway. NOTHING in this module
ever runs git against the real working repo (the PR #61 lesson: a test
that ran real git/gh leaked branches into a live daemon's namespace).
The script itself ``cd``s to ``git rev-parse --show-toplevel``, so a
``cwd`` inside the fabricated repo is sufficient + necessary isolation.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "git_hygiene.sh"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _run_script(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=str(cwd),
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def fab_repo(tmp_path: Path) -> Path:
    """A fabricated throwaway repo with:

      * a bare 'origin' remote (so fetch/prune is real but local)
      * branch `main` (checked out at the end)
      * `merged-gone`     : merged into main, upstream pruned => DELETABLE
      * `unmerged-gone`   : has an extra commit, upstream pruned => KEEP
      * current branch is `main`
    """
    bare = tmp_path / "origin.git"
    bare.mkdir()
    _git(bare, "init", "--bare", "-q")

    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "t")
    (work / "f.txt").write_text("base\n")
    _git(work, "add", "f.txt")
    _git(work, "commit", "-q", "-m", "base")
    _git(work, "remote", "add", "origin", str(bare))
    _git(work, "push", "-q", "-u", "origin", "main")

    # merged-gone: a branch fully merged into main, then its remote
    # upstream is created+pushed, then deleted on the remote so it
    # becomes [gone] after a prune.
    _git(work, "switch", "-q", "-c", "merged-gone")
    _git(work, "push", "-q", "-u", "origin", "merged-gone")
    _git(work, "switch", "-q", "main")  # merged-gone == main => merged

    # unmerged-gone: has its OWN commit not in main (unmerged).
    _git(work, "switch", "-q", "-c", "unmerged-gone")
    (work / "g.txt").write_text("extra\n")
    _git(work, "add", "g.txt")
    _git(work, "commit", "-q", "-m", "extra")
    _git(work, "push", "-q", "-u", "origin", "unmerged-gone")
    _git(work, "switch", "-q", "main")

    # Delete both branches on the remote => after a prune both
    # local upstreams are [gone].
    _git(work, "push", "-q", "origin", "--delete", "merged-gone")
    _git(work, "push", "-q", "origin", "--delete", "unmerged-gone")
    return work


# ── --init ──────────────────────────────────────────────────────────


def test_init_sets_both_config_keys(fab_repo: Path) -> None:
    r = _run_script(fab_repo, "--init")
    assert r.returncode == 0, r.stderr
    assert _git(fab_repo, "config", "--local", "--get", "fetch.prune").strip() == "true"
    assert (
        _git(fab_repo, "config", "--local", "--get", "gc.worktreePruneExpire").strip()
        == "3.days.ago"
    )


def test_init_is_idempotent(fab_repo: Path) -> None:
    assert _run_script(fab_repo, "--init").returncode == 0
    r2 = _run_script(fab_repo, "--init")  # second run must not error
    assert r2.returncode == 0, r2.stderr
    assert _git(fab_repo, "config", "--local", "--get", "fetch.prune").strip() == "true"
    assert (
        _git(fab_repo, "config", "--local", "--get", "gc.worktreePruneExpire").strip()
        == "3.days.ago"
    )


# ── --dry-run ───────────────────────────────────────────────────────


def _local_branches(repo: Path) -> set[str]:
    out = _git(repo, "branch", "--format=%(refname:short)")
    return {b.strip() for b in out.splitlines() if b.strip()}


def test_dry_run_changes_nothing(fab_repo: Path) -> None:
    before = _local_branches(fab_repo)
    assert {"main", "merged-gone", "unmerged-gone"} <= before
    r = _run_script(fab_repo, "--dry-run")
    assert r.returncode == 0, r.stderr
    # The deletable branch is still present after a dry-run.
    after = _local_branches(fab_repo)
    assert after == before
    assert "merged-gone" in after  # NOT deleted by dry-run


def test_default_no_args_is_dry_run(fab_repo: Path) -> None:
    before = _local_branches(fab_repo)
    r = _run_script(fab_repo)  # no args => safe default
    assert r.returncode == 0, r.stderr
    assert _local_branches(fab_repo) == before  # nothing deleted


# ── --apply ─────────────────────────────────────────────────────────


def test_apply_deletes_only_merged_gone(fab_repo: Path) -> None:
    r = _run_script(fab_repo, "--apply")
    assert r.returncode == 0, r.stderr
    after = _local_branches(fab_repo)
    # HARD SAFETY proven: only the merged+[gone] branch is gone.
    assert "merged-gone" not in after
    assert "main" in after  # never delete main
    assert "unmerged-gone" in after  # never delete an unmerged branch


def test_apply_with_nothing_to_do_exits_clean(fab_repo: Path) -> None:
    # First apply removes merged-gone; a second apply has nothing to do.
    assert _run_script(fab_repo, "--apply").returncode == 0
    r2 = _run_script(fab_repo, "--apply")
    assert r2.returncode == 0, r2.stderr
    assert "nothing to delete" in r2.stdout


def test_apply_never_deletes_current_branch(fab_repo: Path) -> None:
    # Make `merged-gone` the CURRENT branch — it is merged+[gone] and
    # would otherwise be deletable, but the current branch must survive.
    _git(fab_repo, "switch", "-q", "merged-gone")
    r = _run_script(fab_repo, "--apply")
    assert r.returncode == 0, r.stderr
    assert "merged-gone" in _local_branches(fab_repo)  # current => kept


def test_apply_handles_slash_named_branch(fab_repo: Path) -> None:
    # Regression: the awk extraction in deletable_branches() parses the
    # first field of `git branch -vv` output. A branch name containing
    # '/' (e.g. feat/slash-name) is emitted as a single token by awk, so
    # the extraction must preserve it intact. This test would FAIL (the
    # branch would survive undeletably) if awk split on '/' or the
    # grep -xF anchor failed to match the full name.
    _git(fab_repo, "switch", "-q", "-c", "feat/slash-name")
    _git(fab_repo, "push", "-q", "-u", "origin", "feat/slash-name")
    _git(fab_repo, "switch", "-q", "main")  # feat/slash-name == main => merged
    _git(fab_repo, "push", "-q", "origin", "--delete", "feat/slash-name")
    # Upstream is now [gone]; branch is merged into main => deletable.
    r = _run_script(fab_repo, "--apply")
    assert r.returncode == 0, r.stderr
    after = _local_branches(fab_repo)
    assert "feat/slash-name" not in after  # slash name deleted correctly
    assert "main" in after  # main untouched
    assert "merged-gone" not in after  # merged-gone also cleaned up
    assert "unmerged-gone" in after  # unmerged branch kept
