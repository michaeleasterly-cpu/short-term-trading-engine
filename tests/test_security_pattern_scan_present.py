"""Behavioral + presence sentinel for security_pattern_scan.

Tier-1 #1 of the vendor-vs-hand-rolled audit (PR #461 + this PR):
vendored Anthropic's security-guidance Layer 1 (pattern rules) +
added STE-specific patterns (no yfinance, no Discord, no inline
``# noqa: SLF001``, no hardcoded Postgres URL with embedded creds,
no raw ``os.environ["DATABASE_URL"] =`` in tests).

This sentinel pins:

  1. The three hook files exist on disk and are well-formed.
  2. The kill-switch env var actually disables the scan.
  3. A representative Anthropic-vendored pattern (yaml_unsafe_load)
     fires.
  4. Each STE-specific pattern fires on a canonical positive case.
  5. Each STE-specific pattern stays silent on a canonical negative
     case (no false-positive on test placeholders, etc.).

Behavior tests exercise the hook by piping a synthetic PostToolUse
JSON to the bash wrapper — same path the Claude Code runtime uses.

Per ``.claude/rules/tests-and-ci.md``: this test runs no ``git``,
``gh``, or DB access. The subprocess invocations are hermetic — no
network, no DB.

Authoritative external:
  - https://code.claude.com/docs/en/hooks
  - https://github.com/anthropics/claude-code (plugins/security-guidance)
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_HOOKS_DIR = _REPO / ".claude" / "hooks"
_WRAPPER = _HOOKS_DIR / "security_pattern_scan.sh"
_ENTRYPOINT = _HOOKS_DIR / "security_pattern_scan.py"
_PATTERNS_VENDORED = _HOOKS_DIR / "security_patterns_vendored.py"
_PATTERNS_STE = _HOOKS_DIR / "security_patterns_ste.py"


def _run_hook(
    tool_name: str,
    file_path: str,
    content: str,
    extra_env: dict | None = None,
) -> tuple[int, str, str]:
    """Invoke the hook bash wrapper with a synthetic PostToolUse JSON.
    Returns (returncode, stdout, stderr)."""
    payload = json.dumps({
        "tool_name": tool_name,
        "tool_input": {
            "file_path": file_path,
            "content": content,
            "new_string": content,
        },
    })
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(_REPO)}
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(
        [str(_WRAPPER)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=10,
    )
    return result.returncode, result.stdout, result.stderr


# ---------------------------------------------------------------------------
# 1. Presence + structural
# ---------------------------------------------------------------------------


def test_three_hook_files_exist() -> None:
    """All three files (wrapper, entrypoint, two pattern files) are present."""
    for path in (_WRAPPER, _ENTRYPOINT, _PATTERNS_VENDORED, _PATTERNS_STE):
        assert path.is_file(), f"missing hook file: {path}"


def test_wrapper_is_executable() -> None:
    import stat as st
    mode = _WRAPPER.stat().st_mode
    assert mode & st.S_IXUSR, (
        f"hook wrapper is not executable (chmod +x): {_WRAPPER}"
    )


def test_kill_switch_disables_scan() -> None:
    """STE_SECURITY_PATTERN_SCAN_DISABLE=1 → hook exits 0 with no
    output, even when content contains a known-bad pattern."""
    rc, stdout, _ = _run_hook(
        tool_name="Write",
        file_path="tpcore/foo.py",
        content="import yfinance as yf\n",
        extra_env={"STE_SECURITY_PATTERN_SCAN_DISABLE": "1"},
    )
    assert rc == 0
    assert stdout.strip() == "", (
        f"kill switch should suppress output; got stdout={stdout!r}"
    )


# ---------------------------------------------------------------------------
# 2. Anthropic-vendored pattern fires (representative)
# ---------------------------------------------------------------------------


def test_anthropic_pickle_pattern_fires() -> None:
    """The Anthropic-vendored `pickle_deserialization` pattern fires
    on a `pickle.loads` call. Representative test of the vendored
    matcher."""
    rc, stdout, _ = _run_hook(
        tool_name="Write",
        file_path="tpcore/foo.py",
        content="import pickle\ndata = pickle.loads(blob)\n",
    )
    assert rc == 0
    assert stdout.strip(), "hook should emit additionalContext on match"
    output = json.loads(stdout)
    additional = output["hookSpecificOutput"]["additionalContext"]
    assert "pickle" in additional.lower()


# ---------------------------------------------------------------------------
# 3. STE-specific patterns — positive cases
# ---------------------------------------------------------------------------


def test_ste_no_yfinance_fires() -> None:
    rc, stdout, _ = _run_hook(
        tool_name="Write",
        file_path="tpcore/data/handlers.py",
        content="import yfinance as yf\n",
    )
    assert rc == 0
    assert stdout.strip()
    output = json.loads(stdout)
    additional = output["hookSpecificOutput"]["additionalContext"]
    assert "ste_no_yfinance" in additional


def test_ste_no_discord_fires() -> None:
    rc, stdout, _ = _run_hook(
        tool_name="Write",
        file_path="ops/notify.py",
        content="import discord\n",
    )
    assert rc == 0
    output = json.loads(stdout)
    additional = output["hookSpecificOutput"]["additionalContext"]
    assert "ste_no_discord" in additional


def test_ste_inline_noqa_slf001_fires_in_production_code() -> None:
    rc, stdout, _ = _run_hook(
        tool_name="Edit",
        file_path="tpcore/risk/governor.py",
        content="    pool = engine._pool  # noqa: SLF001\n",
    )
    assert rc == 0
    output = json.loads(stdout)
    additional = output["hookSpecificOutput"]["additionalContext"]
    assert "ste_no_inline_noqa_slf001" in additional


def test_ste_hardcoded_postgres_url_with_creds_fires() -> None:
    rc, stdout, _ = _run_hook(
        tool_name="Write",
        file_path="tpcore/foo.py",
        content='DB_URL = "postgresql://operator:s3cret@db.host:5432/ste"\n',
    )
    assert rc == 0
    output = json.loads(stdout)
    additional = output["hookSpecificOutput"]["additionalContext"]
    assert "ste_hardcoded_postgres_url" in additional


def test_ste_bare_os_environ_database_url_in_test_fires() -> None:
    rc, stdout, _ = _run_hook(
        tool_name="Edit",
        file_path="tests/test_something.py",
        content='os.environ["DATABASE_URL"] = "postgres://fake/db"\n',
    )
    assert rc == 0
    output = json.loads(stdout)
    additional = output["hookSpecificOutput"]["additionalContext"]
    assert "ste_bare_os_environ_database_url" in additional


# ---------------------------------------------------------------------------
# 4. STE-specific patterns — negative cases (no false positives)
# ---------------------------------------------------------------------------


def test_placeholder_postgres_url_does_not_fire() -> None:
    """A test placeholder URL without embedded credentials must NOT
    trigger ste_hardcoded_postgres_url — the regex requires user:pass@
    to fire."""
    rc, stdout, _ = _run_hook(
        tool_name="Write",
        file_path="tests/conftest.py",
        content='STUB_URL = "postgresql://localhost/test"\n',
    )
    assert rc == 0
    # No matches expected → no output.
    if stdout.strip():
        output = json.loads(stdout)
        additional = output["hookSpecificOutput"]["additionalContext"]
        assert "ste_hardcoded_postgres_url" not in additional, (
            f"placeholder URL should NOT trigger; got: {additional!r}"
        )


def test_noqa_slf001_in_test_does_not_fire() -> None:
    """Inline noqa: SLF001 in a test file is allowed per the standing
    pyproject scoped-ignore convention — ste_no_inline_noqa_slf001
    must skip test paths."""
    rc, stdout, _ = _run_hook(
        tool_name="Edit",
        file_path="tpcore/tests/test_pool_internals.py",
        content="    pool = engine._pool  # noqa: SLF001\n",
    )
    assert rc == 0
    if stdout.strip():
        output = json.loads(stdout)
        additional = output["hookSpecificOutput"]["additionalContext"]
        assert "ste_no_inline_noqa_slf001" not in additional


def test_monkeypatch_setenv_database_url_does_not_fire() -> None:
    """monkeypatch.setenv is the standing convention — the regex must
    require `os.environ[...]` to fire."""
    rc, stdout, _ = _run_hook(
        tool_name="Edit",
        file_path="tests/test_something.py",
        content='monkeypatch.setenv("DATABASE_URL", "postgres://fake/db")\n',
    )
    assert rc == 0
    if stdout.strip():
        output = json.loads(stdout)
        additional = output["hookSpecificOutput"]["additionalContext"]
        assert "ste_bare_os_environ_database_url" not in additional


def test_empty_content_no_output() -> None:
    """An Edit/Write with empty content emits no output."""
    rc, stdout, _ = _run_hook(
        tool_name="Edit",
        file_path="tpcore/foo.py",
        content="",
    )
    assert rc == 0
    assert stdout.strip() == ""


def test_unrelated_tool_name_no_output() -> None:
    """A Bash tool input does NOT trigger the scan (the matcher's
    settings.json filter limits to Edit|Write|MultiEdit|NotebookEdit)."""
    rc, stdout, _ = _run_hook(
        tool_name="Bash",
        file_path="",
        content="import yfinance",
    )
    assert rc == 0
    assert stdout.strip() == ""
