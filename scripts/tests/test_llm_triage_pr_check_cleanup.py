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

import contextlib
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


# ---------------------------------------------------------------------------
# (C) Engine-lane base loader (Epic E Phase 3.2): the SAME #63-hardened
# worktree-add + remove + defensive-prune-fallback path serves the
# engine registries baseline (`ops.engine_ladder.DISPOSITION_POLICIES`
# via `_ENGINE_REGISTRY_SNIPPETS`). It must keep the SAME isolation
# guarantees — no stale admin entry, no real-host-repo worktree leak.
# ---------------------------------------------------------------------------


def test_engine_lane_load_specs_base_cleanup_invariant(
    tmp_path: Path,
) -> None:
    """The engine snippets go through the SAME #63-hardened loader. The
    `ops` package is NOT in the editable-install MAPPING (only `tpcore`
    + engines are — verified), so unlike the data snippet the engine
    snippet resolves `ops.engine_ladder` from the WORKTREE; a bare
    tmp-fixture repo has no `ops/` so the snippet raises. That is
    expected here — this test asserts the load-bearing INVARIANT (the
    #63 hardening): even when the in-worktree subprocess fails, the
    loader STILL leaves no stale admin entry and NEVER touches the real
    host repo. (Real CI runs against origin/main, which DOES contain
    ops/engine_ladder.py and PYTHONPATH=wt_path, so the snippet
    succeeds there — proven by the engine fence unit tests.)"""
    repo = _setup_tmp_repo(tmp_path)

    with patch.object(_mod, "_REPO_ROOT", repo):
        with contextlib.suppress(subprocess.CalledProcessError):
            _mod._load_specs_base("main", _mod._ENGINE_REGISTRY_SNIPPETS)

    # The load-bearing invariant: cleanup happened despite the failure.
    assert _worktree_list_count(repo) == 1, (
        "Engine-lane base load leaked a stale worktree admin entry"
    )
    assert not _stale_admin_entry_exists(repo)
    # Host guard: the real host repo MUST be untouched.
    assert not _worktree_has_llm_triage_entry(_HOST_REPO), (
        "Engine-lane base load leaked a worktree entry into the host repo"
    )


def _setup_tmp_repo_with_ops(tmp: Path) -> Path:
    """Like ``_setup_tmp_repo`` but the initial commit also contains a
    minimal ``ops/engine_ladder.py`` stub (no Pydantic dependency) whose
    ``DISPOSITION_POLICIES`` exactly matches the shape the engine snippet
    emits — so ``PYTHONPATH=wt_path`` resolves the import inside the
    worktree subprocess without touching the real host repo or any remote."""
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

    ops_dir = repo / "ops"
    ops_dir.mkdir()
    (ops_dir / "__init__.py").write_text("")
    # Minimal stub: same public shape as the real engine_ladder but with no
    # Pydantic / StrEnum imports so the snippet works in the venv-less
    # subprocess that only has PYTHONPATH=wt_path.
    (ops_dir / "engine_ladder.py").write_text(
        "class _Disp:\n"
        "    def __init__(self, v): self.value = v\n"
        "class _Pol:\n"
        "    def __init__(self, d): self.default = _Disp(d)\n"
        "DISPOSITION_POLICIES = {\n"
        "    'crashed_startup': _Pol('structural'),\n"
        "    'scheduler_crash': _Pol('structural'),\n"
        "}\n"
    )
    (repo / "placeholder.txt").write_text("x")
    g("add", ".")
    g("commit", "-m", "init")
    # Self-origin so ``origin/main`` resolves inside the tmp repo.
    g("remote", "add", "origin", str(repo))
    g("fetch", "origin")
    return repo


def test_engine_lane_base_loader_succeeds_tmp_isolated(tmp_path: Path) -> None:
    """Positive: the engine-lane base loader returns a normalised
    ``DISPOSITION_POLICIES`` baseline when the worktree contains a valid
    ``ops/engine_ladder.py`` — proving the snippet and loader contract are
    correct.

    Fully tmp-isolated: uses a throwaway repo in ``tmp_path`` whose initial
    commit contains a minimal ``ops/engine_ladder.py`` stub; a self-origin
    remote makes ``origin/main`` resolve LOCALLY inside the tmp repo.
    ``_REPO_ROOT`` is patched so every ``git worktree`` command targets the
    tmp repo — the real host repo and ``origin/main`` of the real repo are
    never accessed.  Mirrors the ``_setup_tmp_repo`` pattern from the
    data-lane #63 tests (git-hygiene rule 3).

    Replaces ``test_engine_lane_base_loader_succeeds_against_real_repo``
    which ran `git worktree add ... origin/main` against the real repo and
    failed in GitHub Actions because the CI checkout does not have
    ``origin/main`` as a resolvable local ref."""
    repo = _setup_tmp_repo_with_ops(tmp_path)

    with patch.object(_mod, "_REPO_ROOT", repo):
        result = _mod._load_specs_base("main", _mod._ENGINE_REGISTRY_SNIPPETS)

    expected_keys = {name for name, _ in _mod._ENGINE_REGISTRY_SNIPPETS}
    assert isinstance(result, dict) and result, (
        "_load_specs_base must return a non-empty dict"
    )
    assert set(result.keys()) == expected_keys, (
        f"keys {set(result.keys())!r} != expected {expected_keys!r}"
    )
    pols = result["disposition_policies"]
    assert "crashed_startup" in pols, "baseline must contain 'crashed_startup'"
    assert pols["crashed_startup"]["stage"] == "structural"
    assert pols["crashed_startup"]["act"] is True

    # Cleanup invariant: no stale admin entry, only the main worktree remains.
    assert _worktree_list_count(repo) == 1, (
        "Engine-lane base load leaked a stale worktree admin entry"
    )
    assert not _stale_admin_entry_exists(repo)

    # Host guard: the real host repo MUST be untouched.
    assert not _worktree_has_llm_triage_entry(_HOST_REPO), (
        "Engine-lane base load leaked a worktree entry into the real host repo"
    )


def test_engine_lane_prune_fallback_when_remove_fails(
    tmp_path: Path,
) -> None:
    """Engine lane: with ``git worktree remove`` swallowed, the
    defensive prune fallback STILL reclaims the stale admin entry — the
    #63 hardening is preserved on the engine path (no real-repo leak)."""
    repo = _setup_tmp_repo(tmp_path)
    real_subprocess_run = subprocess.run

    def _intercept(args, **kwargs):  # noqa: ANN001
        if isinstance(args, list) and args[:3] == [
            "git", "worktree", "remove"
        ]:
            class _FakeOk:
                returncode = 0
                stdout = b""
                stderr = b""

            return _FakeOk()
        return real_subprocess_run(args, **kwargs)

    with patch.object(_mod, "_REPO_ROOT", repo):
        with patch("subprocess.run", side_effect=_intercept):
            # The in-worktree `ops.engine_ladder` import raises in a bare
            # tmp repo (no ops/ — `ops` is not in the editable MAPPING);
            # that is the fixture artifact. The point under test is that
            # the #63 `finally:`-prune fallback STILL reclaims the stale
            # admin entry even on that failure path.
            with contextlib.suppress(subprocess.CalledProcessError):
                _mod._load_specs_base(
                    "main", _mod._ENGINE_REGISTRY_SNIPPETS
                )

    assert not _stale_admin_entry_exists(repo), (
        "Engine-lane: stale .git/worktrees/ admin entry persisted — "
        "the defensive `git worktree prune` fallback was lost on the "
        "engine path."
    )
    assert _worktree_list_count(repo) == 1
    assert not _worktree_has_llm_triage_entry(_HOST_REPO), (
        "Engine-lane test leaked a worktree entry into the real host repo!"
    )
