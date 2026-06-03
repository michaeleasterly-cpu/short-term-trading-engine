"""S5 — sentinels for the workflow portability back-ports.

Pins the narrow patch applied to ``.github/workflows/secret-scan.yml``
by S5:

  * permissions: contents:read + security-events:write + actions:read
  * SARIF upload step has ``continue-on-error: true``
  * gitleaks scan step does NOT have continue-on-error (must remain
    blocking — that is the gate)

The S5 patches that previously also covered
``.github/workflows/claude-review-heavy-lane.yml`` (ANTHROPIC_API_KEY
presence gate, downstream-step guards, allowedTools surface) were
removed 2026-06-03 alongside the workflow itself. The
``test_paid_claude_review_workflow_absent`` sentinel in
``tests/test_claude_surface_contract.py`` is the dispositive guard
that the workflow stays gone.

Stdlib only.
"""
from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_SECRET_SCAN = _REPO / ".github" / "workflows" / "secret-scan.yml"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _strip_yaml_comments(text: str) -> str:
    """Strip ``#`` comment lines so prose forbidding patterns doesn't
    false-positive the scanner. Inline ``# ...`` trailing comments
    are also stripped."""
    out: list[str] = []
    for raw in text.splitlines():
        if raw.lstrip().startswith("#"):
            continue
        if " #" in raw:
            raw = raw.split(" #", 1)[0]
        out.append(raw)
    return "\n".join(out)


# ─────────────────────────────────────────────────────────────────────
# secret-scan.yml
# ─────────────────────────────────────────────────────────────────────


def test_secret_scan_has_actions_read_permission() -> None:
    text = _read(_SECRET_SCAN)
    assert "actions: read" in text, (
        "secret-scan.yml must grant ``actions: read`` (required by "
        "upload-sarif@v3 for workflow-runs fingerprinting)"
    )


def test_secret_scan_preserves_required_permissions() -> None:
    text = _read(_SECRET_SCAN)
    assert "contents: read" in text, "must keep contents: read"
    assert "security-events: write" in text, (
        "must keep security-events: write (SARIF upload)"
    )


def test_secret_scan_does_not_grant_writes_we_forbid() -> None:
    """Defense in depth — the S5 patch may not introduce any write
    permission beyond the SARIF-required ``security-events: write``."""
    code = _strip_yaml_comments(_read(_SECRET_SCAN))
    forbidden_writes = (
        "contents: write",
        "pull-requests: write",
        "issues: write",
        "deployments: write",
        "packages: write",
        "id-token: write",
        "actions: write",
    )
    findings: list[str] = []
    for token in forbidden_writes:
        if token in code:
            findings.append(token)
    assert not findings, (
        f"secret-scan.yml grants forbidden write permission(s): {findings}"
    )


def test_secret_scan_sarif_upload_has_continue_on_error() -> None:
    """The SARIF upload step must mask its own failure so a code-
    scanning hiccup doesn't red the gitleaks gate."""
    text = _read(_SECRET_SCAN)
    # Locate the "Upload SARIF" step block and confirm
    # ``continue-on-error: true`` appears within it (before the next
    # ``- name:`` line).
    lines = text.splitlines()
    start = None
    for i, raw in enumerate(lines):
        if "name: Upload SARIF to code-scanning" in raw:
            start = i
            break
    assert start is not None, (
        "SARIF upload step not found in secret-scan.yml"
    )
    # Find next sibling step.
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].lstrip().startswith("- name:"):
            end = j
            break
    block = "\n".join(lines[start:end])
    assert "continue-on-error: true" in block, (
        "Upload SARIF step must have ``continue-on-error: true`` so "
        "code-scanning unavailability does not fail the gitleaks gate"
    )


def test_secret_scan_gitleaks_scan_remains_blocking() -> None:
    """The gitleaks SCAN step is the gate — it MUST NOT have
    continue-on-error. Otherwise a real secret leak would not red CI."""
    text = _read(_SECRET_SCAN)
    lines = text.splitlines()
    start = None
    for i, raw in enumerate(lines):
        if "name: Run gitleaks" in raw:
            start = i
            break
    assert start is not None, (
        "gitleaks scan step not found in secret-scan.yml"
    )
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].lstrip().startswith("- name:"):
            end = j
            break
    block = "\n".join(lines[start:end])
    assert "continue-on-error: true" not in block, (
        "gitleaks scan step must NOT have continue-on-error — it is "
        "the gate; masking its exit code would defeat the entire "
        "secret-scan workflow"
    )


def test_secret_scan_still_pins_gitleaks_version() -> None:
    """Supply-chain hygiene preserved."""
    text = _read(_SECRET_SCAN)
    m = re.search(r"GITLEAKS_VERSION=(\d+\.\d+\.\d+)", text)
    assert m is not None, (
        "secret-scan.yml must pin GITLEAKS_VERSION=<semver>"
    )
    assert m.group(1).startswith("8."), (
        f"unexpected gitleaks major version drift: {m.group(1)}"
    )


def test_no_deployment_commands_introduced_in_secret_scan() -> None:
    """Defense in depth on secret-scan.yml — the S5 patch may not
    introduce any railway/docker/deploy command in the workflow's
    runtime body (comments stripped)."""
    code = _strip_yaml_comments(_read(_SECRET_SCAN))
    for pat in (
        r"\brailway\s+up\b",
        r"\bdocker\s+(?:run|build|compose|exec)\b",
        r"\bgh\s+pr\s+merge\b",
        r"\bgit\s+push\s+.*--force\b",
    ):
        m = re.search(pat, code, re.IGNORECASE)
        assert m is None, (
            f"{_SECRET_SCAN.name} body contains forbidden command: "
            f"{m.group(0) if m else ''!r}"
        )
