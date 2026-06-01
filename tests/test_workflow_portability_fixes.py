"""S5 — sentinels for the workflow portability back-ports.

Pins the two narrow patches applied to STE workflows by S5:

  * ``.github/workflows/secret-scan.yml``
    - permissions: contents:read + security-events:write + actions:read
    - SARIF upload step has ``continue-on-error: true``
    - gitleaks scan step does NOT have continue-on-error (must remain
      blocking — that is the gate)

  * ``.github/workflows/claude-review-heavy-lane.yml``
    - ``Gate on ANTHROPIC_API_KEY presence`` step exists
    - skip-output pattern + notice text
    - downstream Checkout + Claude action steps guarded with
      ``if: steps.gate.outputs.skip != 'true'``
    - allowedTools still restricted; no Edit/Write/MultiEdit/
      git push/gh pr merge/docker/railway added

Stdlib only.
"""
from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_SECRET_SCAN = _REPO / ".github" / "workflows" / "secret-scan.yml"
_CLAUDE_REVIEW = _REPO / ".github" / "workflows" / "claude-review-heavy-lane.yml"


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


# ─────────────────────────────────────────────────────────────────────
# claude-review-heavy-lane.yml
# ─────────────────────────────────────────────────────────────────────


def test_claude_review_has_anthropic_api_key_gate_step() -> None:
    text = _read(_CLAUDE_REVIEW)
    assert "Gate on ANTHROPIC_API_KEY presence" in text, (
        "claude-review-heavy-lane.yml must add the secret-presence "
        "gate step (S5 back-port from D2)"
    )
    assert "id: gate" in text, (
        "gate step must declare ``id: gate`` so downstream steps can "
        "reference its outputs"
    )


def test_claude_review_gate_emits_missing_secret_notice() -> None:
    text = _read(_CLAUDE_REVIEW)
    assert "ANTHROPIC_API_KEY secret is not configured" in text, (
        "gate step must emit the verbatim missing-secret notice "
        "(see D2 validated copy)"
    )
    # And the notice must point at the canonical manual-review fallback.
    assert "manual fresh-context review discipline" in text, (
        "missing-secret notice must direct operator to the manual "
        "heavy-lane review discipline as the dispositive gate"
    )


def test_claude_review_gate_uses_env_secret_pattern() -> None:
    """Per GitHub Actions: secrets context is not allowed in job-level
    ``if:`` conditionals, so the standard pattern is to expose
    ``${{ secrets.ANTHROPIC_API_KEY != '' }}`` via an ``env:`` key on a
    guard step and check it in shell."""
    text = _read(_CLAUDE_REVIEW)
    assert re.search(
        r"HAS_KEY:\s*\$\{\{\s*secrets\.ANTHROPIC_API_KEY\s*!=\s*''\s*\}\}",
        text,
    ), (
        "gate must read secrets.ANTHROPIC_API_KEY via env: HAS_KEY"
    )
    assert re.search(r'skip=true', text) and re.search(r'skip=false', text), (
        "gate must write both ``skip=true`` and ``skip=false`` outputs"
    )


def test_claude_review_checkout_and_action_steps_are_guarded() -> None:
    """Downstream steps (Checkout + Claude Code Action) must be
    conditional on ``steps.gate.outputs.skip != 'true'``, otherwise
    the gate would emit a notice but the action would still try to
    run with an empty secret and crash the job."""
    text = _read(_CLAUDE_REVIEW)
    # Count occurrences — we expect at least 2 (Checkout + Claude action).
    pattern = r"if:\s*steps\.gate\.outputs\.skip\s*!=\s*'true'"
    matches = re.findall(pattern, text)
    assert len(matches) >= 2, (
        f"expected at least 2 steps guarded by "
        f"``if: steps.gate.outputs.skip != 'true'``; got {len(matches)}"
    )


def test_claude_review_allowed_tools_remain_read_only() -> None:
    """The S5 patch must not broaden the action's tool surface."""
    text = _read(_CLAUDE_REVIEW)
    forbidden_tools = (
        "Bash(git commit",
        "Bash(git push",
        "Bash(gh pr create",
        "Bash(gh pr merge",
        "Bash(railway",
        "Bash(docker",
    )
    findings: list[str] = []
    for token in forbidden_tools:
        if token in text:
            findings.append(token)
    assert not findings, (
        f"claude-review allowedTools must not include mutation surface: "
        f"{findings}"
    )
    # Also defense in depth: Edit/Write/MultiEdit/NotebookEdit must not
    # appear in any allowedTools list. (They may appear in the prompt
    # text forbidding them, which is fine — we only check the
    # ``--allowedTools`` block.)
    in_allowed = False
    for raw in text.splitlines():
        if "--allowedTools" in raw:
            in_allowed = True
        if in_allowed:
            for write_tool in ("Edit,", "Write,", "MultiEdit", "NotebookEdit"):
                # Only flag if the token is a bare allowedTools entry,
                # not a Bash() subcommand or a prose mention.
                stripped = raw.strip()
                if stripped.endswith('"') and "allowedTools" not in stripped:
                    # End of the allowedTools value.
                    break
                if (
                    f',{write_tool}' in raw
                    or f'"{write_tool}' in raw
                ):
                    raise AssertionError(
                        f"allowedTools must not include {write_tool!r}: "
                        f"line={raw!r}"
                    )
            # Heuristic exit: the allowedTools value is a single line
            # in the YAML block-scalar; one iteration is enough.
            break


def test_claude_review_permissions_remain_minimal() -> None:
    """The S5 patch did not touch permissions — confirm none of the
    forbidden write scopes leaked in."""
    text = _read(_CLAUDE_REVIEW)
    code = _strip_yaml_comments(text)
    # Existing baseline (do NOT remove):
    assert "contents: read" in code, "must keep contents: read"
    # Newly forbidden writes:
    forbidden_writes = (
        "contents: write",
        "deployments: write",
        "packages: write",
        "actions: write",
    )
    findings = [t for t in forbidden_writes if t in code]
    assert not findings, (
        f"claude-review permissions broadened to forbidden scope(s): "
        f"{findings}"
    )


def test_no_deployment_commands_introduced() -> None:
    """Defense in depth across both files — the S5 patch may not
    introduce any railway/docker/deploy command in either workflow's
    runtime body (comments stripped)."""
    for path in (_SECRET_SCAN, _CLAUDE_REVIEW):
        code = _strip_yaml_comments(_read(path))
        for pat in (
            r"\brailway\s+up\b",
            r"\bdocker\s+(?:run|build|compose|exec)\b",
            r"\bgh\s+pr\s+merge\b",
            r"\bgit\s+push\s+.*--force\b",
        ):
            m = re.search(pat, code, re.IGNORECASE)
            assert m is None, (
                f"{path.name} body contains forbidden command: "
                f"{m.group(0) if m else ''!r}"
            )
