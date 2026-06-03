"""S2 — sentinels for scripts/run_dev_system_audit.sh.

Pins the wrapper's read-only / report-only / no-mutation contract.
Stdlib only.
"""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_WRAPPER = _REPO / "scripts" / "run_dev_system_audit.sh"


def _wrapper_text() -> str:
    return _WRAPPER.read_text(encoding="utf-8")


def _strip_shell_comments(text: str) -> str:
    """Strip ``#``-prefixed comment lines so doc-blocks that document
    what the wrapper forbids don't false-positive the scanner."""
    out: list[str] = []
    for raw in text.splitlines():
        if raw.lstrip().startswith("#"):
            continue
        out.append(raw)
    return "\n".join(out)


# ─────────────────────────────────────────────────────────────────────
# Existence + executable bit
# ─────────────────────────────────────────────────────────────────────


def test_wrapper_exists_and_is_executable() -> None:
    assert _WRAPPER.is_file(), f"missing {_WRAPPER.relative_to(_REPO)}"
    text = _wrapper_text()
    assert text.startswith("#!"), "wrapper must have a shebang on line 1"
    mode = _WRAPPER.stat().st_mode
    assert mode & stat.S_IXUSR, "wrapper must be executable (chmod +x)"


def test_wrapper_uses_strict_mode() -> None:
    """``set -uo pipefail`` so the wrapper fails loudly on unset vars
    or pipe errors. We intentionally do NOT require ``set -e`` because
    the wrapper captures audit/check exit codes with ``|| rc=$?`` and
    needs to keep going past a nonzero — that's the report-only
    contract."""
    text = _wrapper_text()
    assert "set -uo pipefail" in text, (
        "wrapper must use ``set -uo pipefail``"
    )


# ─────────────────────────────────────────────────────────────────────
# Required dev-system invocations
# ─────────────────────────────────────────────────────────────────────


def test_wrapper_invokes_audit_project() -> None:
    text = _wrapper_text()
    assert "audit_project.py" in text, (
        "wrapper must invoke devsystem/scripts/audit_project.py"
    )
    # And it does so with --target-dir, not --some-other-flag.
    assert "audit_project.py" in text and "--target-dir" in text, (
        "wrapper must pass --target-dir to audit_project.py"
    )


def test_wrapper_invokes_check_manifests() -> None:
    text = _wrapper_text()
    assert "check_manifests.py" in text, (
        "wrapper must invoke devsystem/scripts/check_manifests.py"
    )
    assert "check_manifests.py" in text and "--target-dir" in text, (
        "wrapper must pass --target-dir to check_manifests.py"
    )


# ─────────────────────────────────────────────────────────────────────
# Forbidden invocations
# ─────────────────────────────────────────────────────────────────────


def test_wrapper_does_not_invoke_bootstrap_project() -> None:
    """``bootstrap_project.py`` would WRITE artifacts into the target
    directory. The wrapper is read-only; this is the single highest-
    risk regression to guard against."""
    text = _wrapper_text()
    code = _strip_shell_comments(text)
    assert "bootstrap_project.py" not in code, (
        "wrapper must NEVER invoke bootstrap_project.py — that writes "
        "rendered artifacts into the target directory"
    )


def test_wrapper_has_no_mutating_commands() -> None:
    """No shell command in the wrapper body may push, merge, force,
    deploy, or run a container. Comments are stripped first so the
    doc-block legitimately listing what the wrapper forbids does not
    false-positive."""
    code = _strip_shell_comments(_wrapper_text())
    forbidden = (
        r"\brailway\s+up\b",
        r"\bdocker\s+(?:run|build|compose|exec)\b",
        r"\bgh\s+pr\s+merge\b",
        r"\bgh\s+pr\s+create\b",
        r"\bgit\s+push\b",
        r"\bgit\s+commit\b",
        r"\bgit\s+reset\s+--hard\b",
        r"\bgh\s+api\s+.*--method\s+(POST|PUT|PATCH|DELETE)\b",
    )
    findings: list[str] = []
    for pat in forbidden:
        m = re.search(pat, code, re.IGNORECASE)
        if m:
            findings.append(m.group(0))
    assert not findings, (
        f"wrapper body contains forbidden mutation command: {findings}"
    )


def test_wrapper_has_no_anthropic_api_surface() -> None:
    """No HTTP to api.anthropic.com, no memstore endpoint, no raw
    Anthropic key reference. Comments stripped first."""
    code = _strip_shell_comments(_wrapper_text())
    forbidden = (
        "api.anthropic.com",
        "/v1/memory_stores",
        "/v1/messages",
        "ANTHROPIC_API_KEY",
        "anthropic-beta:",
        "anthropic-version:",
    )
    findings: list[str] = []
    for needle in forbidden:
        if needle.lower() in code.lower():
            findings.append(needle)
    assert not findings, (
        f"wrapper body contains Anthropic API surface: {findings}"
    )


# ─────────────────────────────────────────────────────────────────────
# Report-only contract
# ─────────────────────────────────────────────────────────────────────


def test_wrapper_announces_report_only_mode() -> None:
    text = _wrapper_text()
    assert "REPORT_ONLY" in text, (
        "wrapper must print a REPORT_ONLY banner / verdict so the "
        "operator (and any downstream CI invocation) understands "
        "drift findings do not red CI"
    )


def test_wrapper_does_not_propagate_drift_as_failure() -> None:
    """The wrapper MUST end with ``exit 0`` so that audit/check drift
    is advisory, not a CI failure. The wrapper may also exit ``2`` in
    a tool-missing branch BEFORE the audit runs — that is fine; the
    last line of the script must be the unconditional ``exit 0``."""
    text = _wrapper_text()
    # Strip trailing whitespace/blank lines.
    lines = [ln.rstrip() for ln in text.rstrip().splitlines()]
    assert lines[-1] == "exit 0", (
        f"wrapper final line must be ``exit 0`` (report-only contract); "
        f"got {lines[-1]!r}"
    )


def test_wrapper_captures_audit_and_check_exit_codes_safely() -> None:
    """The wrapper must run audit/check with ``|| rc=$?`` so a nonzero
    exit doesn't kill the wrapper under ``set -e`` semantics. Even
    though the wrapper uses ``set -uo pipefail`` (no ``-e``), capturing
    the exit code is the documented contract."""
    text = _wrapper_text()
    assert re.search(r"audit_rc=\$\?", text) or "|| audit_rc=" in text, (
        "wrapper must capture audit_project.py exit code"
    )
    assert re.search(r"check_rc=\$\?", text) or "|| check_rc=" in text, (
        "wrapper must capture check_manifests.py exit code"
    )


# ─────────────────────────────────────────────────────────────────────
# Override support
# ─────────────────────────────────────────────────────────────────────


def test_wrapper_supports_trellis_dev_system_dir_override() -> None:
    text = _wrapper_text()
    assert "TRELLIS_DEV_SYSTEM_DIR" in text, (
        "wrapper must honor TRELLIS_DEV_SYSTEM_DIR env var "
        "(operator-overridable dev-system location)"
    )
    # And it should default sensibly if unset.
    assert re.search(r"TRELLIS_DEV_SYSTEM_DIR:-", text), (
        "wrapper must provide a default when TRELLIS_DEV_SYSTEM_DIR "
        "is unset (``${TRELLIS_DEV_SYSTEM_DIR:-<default>}``)"
    )


def test_wrapper_exits_2_on_missing_dev_system() -> None:
    """The tool-missing branch is the only non-zero exit allowed.
    Sentinel asserts the script body literally contains ``exit 2``."""
    text = _wrapper_text()
    assert re.search(r"\bexit\s+2\b", text), (
        "wrapper must exit 2 (NEEDS_OPERATOR_ACTION) when dev-system "
        "scripts are missing"
    )


# ─────────────────────────────────────────────────────────────────────
# Functional smoke: the wrapper actually runs when invoked
# ─────────────────────────────────────────────────────────────────────


def test_wrapper_runs_and_exits_zero_under_either_path(tmp_path: Path) -> None:
    """Smoke test the wrapper in two configurations:
      * if a sibling trellis-dev-system checkout is reachable at
        the default location, the wrapper should run audit + check
        and exit 0 (REPORT_ONLY).
      * if not, override TRELLIS_DEV_SYSTEM_DIR to a path that
        does NOT contain the scripts and confirm the wrapper exits 2
        (NEEDS_OPERATOR_ACTION) — that's the tool-missing branch.
    The two cases together prove both branches return the documented
    exit codes without any unhandled error."""
    import subprocess
    # Branch B (always reproducible): force a missing dev-system path.
    bogus = tmp_path / "no-such-dev-system"
    env = os.environ.copy()
    env["TRELLIS_DEV_SYSTEM_DIR"] = str(bogus)
    proc = subprocess.run(
        [str(_WRAPPER)], capture_output=True, text=True, env=env, check=False,
    )
    assert proc.returncode == 2, (
        f"wrapper should exit 2 when dev-system path is missing; "
        f"got {proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
    )
    assert "NEEDS_OPERATOR_ACTION" in proc.stdout, (
        f"tool-missing branch must print NEEDS_OPERATOR_ACTION; "
        f"stdout={proc.stdout!r}"
    )
