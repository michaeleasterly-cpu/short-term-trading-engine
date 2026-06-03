"""Behavioral sentinel for ``.claude/hooks/swv-advisory.sh``.

Controls-audit §13 #3 (PR #469): the hook fires the advisory only when
BOTH conditions are true:

  1. The user prompt contains a fix / patch / repair / backfill /
     cleanup verb (case-insensitive, word-boundary).
  2. The working diff vs HEAD touches a `discovery-first`-scoped path
     (validators / ingestion / auditheal / selfheal / migrations /
     scripts/ops.py).

This sentinel exercises the hook against an isolated ``tmp_path`` git
repo (so we honor `.claude/rules/tests-and-ci.md`: "Tests/code MUST
NEVER run real `git`/`gh` against the working repo"). It pins:

  - Exit 0 in all cases (advisory; never blocks).
  - Verb hit + path hit → advisory text on stdout.
  - Verb miss → silent (no stdout).
  - Path miss → silent (no stdout).
  - Empty prompt → silent.
  - Kill switch (STE_SWV_ADVISORY_DISABLE=1) → silent.

Hermetic — no real git/gh against the working repo, no DB, no
network. Each test sets up its own throwaway repo under tmp_path.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_HOOK = _REPO / ".claude" / "hooks" / "swv-advisory.sh"
_ADVISORY_SUBSTRING = "SWV gate applies"


def _make_throwaway_repo_with_diff(tmp_path: Path, changed_files: list[str]) -> Path:
    """Initialize a tmp git repo with HEAD at an empty commit, then
    create / modify the named files so `git diff HEAD --name-only`
    reports them. Returns the repo root path."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-q", "--initial-branch=main", str(repo)],
        check=True,
    )
    # Configure identity for the empty commit (git refuses without it).
    for key, val in (
        ("user.email", "test@example.com"),
        ("user.name", "Test"),
    ):
        subprocess.run(
            ["git", "-C", str(repo), "config", key, val], check=True,
        )
    # Initial empty commit so HEAD exists.
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "--allow-empty",
         "-m", "init"], check=True,
    )
    # Create the changed files.
    for rel in changed_files:
        f = repo / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("# scaffold for hook test\n", encoding="utf-8")
    return repo


def _run_hook(prompt: str, repo: Path, extra_env: dict | None = None) -> tuple[int, str, str]:
    """Invoke the hook with the synthesized UserPromptSubmit JSON."""
    payload = json.dumps({"prompt": prompt})
    env = {
        **os.environ,
        "CLAUDE_PROJECT_DIR": str(repo),
        # Ensure jq is on PATH (hook falls back to no-op without it).
        "PATH": os.environ.get("PATH", ""),
    }
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(
        [str(_HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=10,
    )
    return result.returncode, result.stdout, result.stderr


@pytest.fixture(scope="module")
def _have_jq():
    """Hook degrades to no-op without jq; the behavioral tests assume
    it's present. Skip the suite gracefully if not."""
    if shutil.which("jq") is None:
        pytest.skip("jq not installed; hook degrades to no-op without it")


# ---------------------------------------------------------------------------
# Verb hit + path hit → advisory fires
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("verb", ["fix", "patch", "repair", "backfill", "cleanup"])
def test_verb_hit_plus_path_hit_emits_advisory(_have_jq, tmp_path, verb) -> None:
    repo = _make_throwaway_repo_with_diff(
        tmp_path, ["tpcore/quality/validation/foo.py"],
    )
    rc, stdout, _ = _run_hook(f"please {verb} the validator", repo)
    assert rc == 0, "hook must never block"
    assert _ADVISORY_SUBSTRING in stdout, (
        f"verb={verb!r} + scoped path → advisory must fire; "
        f"stdout={stdout!r}"
    )


@pytest.mark.parametrize(
    "path",
    [
        "tpcore/quality/validation/foo.py",
        "tpcore/ingestion/handler.py",
        "tpcore/auditheal/spec.py",
        "tpcore/selfheal/registry.py",
        "platform/migrations/versions/20260605_test.py",
        "scripts/ops.py",
    ],
)
def test_each_swv_path_fires(_have_jq, tmp_path, path) -> None:
    """Every path in the discovery-first scope must trigger the hook."""
    repo = _make_throwaway_repo_with_diff(tmp_path, [path])
    rc, stdout, _ = _run_hook("please fix this", repo)
    assert rc == 0
    assert _ADVISORY_SUBSTRING in stdout, (
        f"path={path!r} should be in scope; stdout={stdout!r}"
    )


# ---------------------------------------------------------------------------
# Negative paths — must be silent
# ---------------------------------------------------------------------------


def test_verb_miss_is_silent(_have_jq, tmp_path) -> None:
    repo = _make_throwaway_repo_with_diff(
        tmp_path, ["tpcore/quality/validation/foo.py"],
    )
    rc, stdout, _ = _run_hook(
        "could you explain what this validator does", repo,
    )
    assert rc == 0
    assert _ADVISORY_SUBSTRING not in stdout


def test_path_miss_is_silent(_have_jq, tmp_path) -> None:
    """Diff in a non-scoped path — even with a fix verb — stays silent."""
    repo = _make_throwaway_repo_with_diff(
        tmp_path, ["docs/some-note.md"],
    )
    rc, stdout, _ = _run_hook("please fix the typo", repo)
    assert rc == 0
    assert _ADVISORY_SUBSTRING not in stdout


def test_no_diff_is_silent(_have_jq, tmp_path) -> None:
    """Empty working tree — silent."""
    repo = _make_throwaway_repo_with_diff(tmp_path, [])
    rc, stdout, _ = _run_hook("please fix the validator", repo)
    assert rc == 0
    assert _ADVISORY_SUBSTRING not in stdout


def test_empty_prompt_is_silent(_have_jq, tmp_path) -> None:
    repo = _make_throwaway_repo_with_diff(
        tmp_path, ["tpcore/quality/validation/foo.py"],
    )
    rc, stdout, _ = _run_hook("", repo)
    assert rc == 0
    assert _ADVISORY_SUBSTRING not in stdout


def test_kill_switch_silences(_have_jq, tmp_path) -> None:
    """STE_SWV_ADVISORY_DISABLE=1 → silent even on a clean hit."""
    repo = _make_throwaway_repo_with_diff(
        tmp_path, ["tpcore/quality/validation/foo.py"],
    )
    rc, stdout, _ = _run_hook(
        "please fix the validator",
        repo,
        extra_env={"STE_SWV_ADVISORY_DISABLE": "1"},
    )
    assert rc == 0
    assert _ADVISORY_SUBSTRING not in stdout


def test_word_boundary_verb_check(_have_jq, tmp_path) -> None:
    """Verb match is word-boundary; a substring inside a longer word
    (e.g. 'fixture', 'prefix') must not trigger."""
    repo = _make_throwaway_repo_with_diff(
        tmp_path, ["tpcore/quality/validation/foo.py"],
    )
    for prompt in (
        "use the fixture for setup",
        "this is the prefix code",
        "no need to patchwork this",
    ):
        rc, stdout, _ = _run_hook(prompt, repo)
        assert rc == 0
        assert _ADVISORY_SUBSTRING not in stdout, (
            f"prompt={prompt!r} should not trigger; stdout={stdout!r}"
        )
