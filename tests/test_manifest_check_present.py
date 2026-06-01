"""Anti-rot sentinel for the manifest-linter (``scripts/check_manifests.py``).

Mirrors the existing presence sentinels: file exists, runs cleanly on
the current repo, and is referenced from the pre-commit config.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO / "scripts" / "check_manifests.py"
_PRECOMMIT = _REPO / ".pre-commit-config.yaml"


def test_manifest_check_script_present() -> None:
    assert _SCRIPT.is_file(), f"missing manifest linter: {_SCRIPT}"
    src = _SCRIPT.read_text(encoding="utf-8")
    assert src.strip(), "manifest linter is empty"
    assert "def main(" in src, "manifest linter must expose a main() entry"
    # Stdlib-only invariant — guard against creeping new deps. The
    # only third-party-looking import we tolerate is the project's own
    # convention. Allowlist common stdlib modules; flag anything else.
    import re
    imported = re.findall(
        r"^(?:import|from)\s+([a-zA-Z_][a-zA-Z0-9_]*)",
        src, re.MULTILINE,
    )
    stdlib = {
        "__future__", "json", "re", "sys", "pathlib", "ast", "os",
        "subprocess", "typing",
    }
    extras = set(imported) - stdlib
    assert not extras, (
        f"manifest linter must be stdlib-only; found extras: {sorted(extras)}"
    )


def test_manifest_check_exits_zero_on_current_repo() -> None:
    """Run the linter against the current repo. A green run on every
    commit is the invariant the linter exists to defend."""
    result = subprocess.run(
        [sys.executable, str(_SCRIPT)],
        capture_output=True, text=True, cwd=str(_REPO),
        check=False, timeout=60,
    )
    assert result.returncode == 0, (
        f"manifest linter failed on current repo "
        f"(rc={result.returncode}):\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


def test_manifest_check_output_is_readable() -> None:
    """On success, stdout is a one-line acknowledgement — operator
    can spot-check the pre-commit run output."""
    result = subprocess.run(
        [sys.executable, str(_SCRIPT)],
        capture_output=True, text=True, cwd=str(_REPO),
        check=False, timeout=60,
    )
    assert result.returncode == 0
    assert "OK" in result.stdout, (
        "expected an 'OK' marker on success; "
        f"got stdout={result.stdout!r}"
    )


def test_pre_commit_config_references_check_manifests() -> None:
    """If a pre-commit config exists, it must reference the linter
    so a fresh checkout's ``pre-commit install`` wires it in."""
    if not _PRECOMMIT.is_file():
        # No pre-commit config = nothing to check. The linter is
        # still runnable manually via the script path.
        return
    src = _PRECOMMIT.read_text(encoding="utf-8")
    assert "check-manifests" in src, (
        ".pre-commit-config.yaml exists but does not reference "
        "the 'check-manifests' local hook — add a local hook so "
        "fresh-checkout 'pre-commit install' wires the linter in"
    )
    assert "scripts/check_manifests.py" in src, (
        "pre-commit hook must invoke scripts/check_manifests.py"
    )
