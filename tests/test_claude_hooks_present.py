"""Anti-rot sentinel for ``.claude/hooks/`` enforcement scripts.

Each named hook script must exist, be executable, and be referenced
from ``.claude/settings.json`` (so it's actually wired into the
runtime — a hook file on disk without a settings entry is silently
dead). Plus a behavioural unit test that the ``block-git-checkout.sh``
hook genuinely refuses a ``git checkout main`` command.

Authoritative external: <https://code.claude.com/docs/en/hooks-guide>.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_HOOKS_DIR = _REPO / ".claude" / "hooks"
_SETTINGS = _REPO / ".claude" / "settings.json"

_HOOKS = (
    "block-git-checkout.sh",
    "block-pytest-subset-when-ops.sh",
    "gate-ecr-dfcr-edits.sh",
    "risk-path-reminder.sh",
    "session-start.sh",
    # 2026-06-04 — vendor-audit §2 + §9 #1: Anthropic plugins/security-
    # guidance Layer 1 (pattern-rules) vendored. Layer 2 (Stop-hook LLM
    # diff review) + Layer 3 (agentic commit/push review) intentionally
    # NOT vendored — they call the Anthropic API per turn / per commit
    # and the cost has not been measured. Re-evaluate after Layer 1
    # adoption demonstrates value.
    "security_pattern_scan.sh",
)


@pytest.mark.parametrize("name", _HOOKS)
def test_hook_present_executable_and_referenced(name: str) -> None:
    """Every named hook exists on disk, is executable, and is wired
    into ``.claude/settings.json`` — a hook file without a settings
    entry is silently dead."""
    path = _HOOKS_DIR / name
    assert path.is_file(), f"missing hook script: {path}"
    mode = path.stat().st_mode
    assert mode & stat.S_IXUSR, f"hook is not executable (chmod +x): {path}"
    assert _SETTINGS.is_file(), f"missing project settings: {_SETTINGS}"
    settings_src = _SETTINGS.read_text()
    assert name in settings_src, (
        f"hook {name} is on disk but not referenced from "
        f"{_SETTINGS.name} — silently dead. Wire it via "
        "`hooks.<EventName>[].hooks[].command`.")


def test_settings_json_is_valid_json() -> None:
    """``.claude/settings.json`` is valid JSON (a syntax bug would
    silently disable every hook)."""
    assert _SETTINGS.is_file(), f"missing project settings: {_SETTINGS}"
    payload = json.loads(_SETTINGS.read_text())
    # Quick shape probe: each top-level hooks section is a list.
    hooks = payload.get("hooks", {})
    assert isinstance(hooks, dict), "'hooks' must be a JSON object"
    for event, matchers in hooks.items():
        assert isinstance(matchers, list), (
            f"hooks.{event} must be a JSON array of matchers")


def test_required_hook_set_is_present() -> None:
    """Subset-not-equality: every required hook is present; plugin or
    user-machine hooks may live alongside (we do not gitignore the
    .claude/hooks/ dir narrowly, so extras would still need to be
    intentional — flag a missing required hook only)."""
    on_disk = {p.name for p in _HOOKS_DIR.glob("*.sh")}
    missing = set(_HOOKS) - on_disk
    assert not missing, f"missing required hooks: {missing}"


def test_block_git_checkout_actually_blocks_branch_switch() -> None:
    """Behavioural sanity: the ``block-git-checkout.sh`` hook MUST
    refuse a bare ``git checkout main`` command (the canonical case
    the operator named as the verification target). Allows the
    file-restore form ``git checkout -- <path>``."""
    script = _HOOKS_DIR / "block-git-checkout.sh"
    assert script.is_file()

    # Bare branch switch — must block (exit 2).
    payload = json.dumps({"tool_input": {"command": "git checkout main"}})
    result = subprocess.run(
        [str(script)],
        input=payload,
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": os.environ.get("PATH", "")},
        check=False,
    )
    assert result.returncode == 2, (
        f"hook should BLOCK `git checkout main` (exit 2) but exited "
        f"{result.returncode}; stderr={result.stderr!r}")
    assert "git switch" in result.stderr, (
        f"hook stderr should point at git switch; got {result.stderr!r}")

    # File restore — must allow (exit 0).
    payload = json.dumps(
        {"tool_input": {"command": "git checkout -- src/foo.py"}})
    result = subprocess.run(
        [str(script)],
        input=payload,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"hook should ALLOW `git checkout -- <path>` (file restore) but "
        f"exited {result.returncode}; stderr={result.stderr!r}")

    # `git checkout -b` (operator wants `git switch -c`) — must block.
    payload = json.dumps(
        {"tool_input": {"command": "git checkout -b feature/x"}})
    result = subprocess.run(
        [str(script)],
        input=payload,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2, (
        f"hook should BLOCK `git checkout -b` (exit 2) but exited "
        f"{result.returncode}; stderr={result.stderr!r}")
