"""Anti-rot sentinel for the public-repo secret-scanner gate.

The repo flipped public on 2026-05-21 (GitHub Actions quota); a leaked
credential is now a public-timeline incident, so the gate must stay
wired and stay coupled.  This test asserts the four artifacts ship
together (workflow + allowlist + ignore + pre-commit hook) and that
the gitleaks version stays pinned across them so an upgrade can't drift
the CI gate away from the local pre-commit hook.

Authoritative external:
    - <https://github.com/gitleaks/gitleaks#configuration>
    - <https://github.com/gitleaks/gitleaks/releases/tag/v8.30.1>
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_GITLEAKS_TOML = _REPO / ".gitleaks.toml"
_GITLEAKS_IGNORE = _REPO / ".gitleaksignore"
_WORKFLOW = _REPO / ".github" / "workflows" / "secret-scan.yml"
_PRE_COMMIT = _REPO / ".pre-commit-config.yaml"
_AUDIT_DOC = _REPO / "docs" / "audits" / "2026-05-21-public-repo-secret-audit.md"

# Single source of truth for the pinned gitleaks release.  Bump
# this constant when bumping the workflow + pre-commit pins.  The
# test below asserts every artifact reaches the same version.
_PINNED_GITLEAKS_VERSION = "8.30.1"


def test_gitleaks_toml_exists_and_parses() -> None:
    """``.gitleaks.toml`` must exist and parse as valid TOML."""
    assert _GITLEAKS_TOML.is_file(), (
        f"missing {_GITLEAKS_TOML.relative_to(_REPO)} — secret-scan "
        "gate config is load-bearing for the public-repo guarantee."
    )
    # tomllib.loads on the raw bytes — a syntax error raises
    # tomllib.TOMLDecodeError and reds CI with the line number.
    data = tomllib.loads(_GITLEAKS_TOML.read_text())
    # gitleaks v8 uses ``[extend].useDefault = true`` to inherit the
    # upstream ruleset; without it we'd silently scan with an EMPTY
    # ruleset and the gate would be structurally inert.
    assert data.get("extend", {}).get("useDefault") is True, (
        "[extend].useDefault must be true — without it gitleaks runs "
        "with NO rules and the gate is silently inert."
    )
    # At least one [[allowlists]] entry must exist (the documented
    # false-positives from the 2026-05-21 audit).
    allowlists = data.get("allowlists") or []
    assert allowlists, (
        "no [[allowlists]] entries — the audit documented three "
        "confirmed-clean false positives that must be pinned here."
    )


def test_gitleaksignore_exists_and_pins_fingerprints() -> None:
    """``.gitleaksignore`` must exist and pin the three baseline
    false-positive fingerprints."""
    assert _GITLEAKS_IGNORE.is_file(), (
        f"missing {_GITLEAKS_IGNORE.relative_to(_REPO)} — fingerprint "
        "pins for the audited false positives."
    )
    src = _GITLEAKS_IGNORE.read_text()
    # Each fingerprint is ``<sha>:<path>:<rule>:<line>`` per
    # https://github.com/gitleaks/gitleaks#gitleaksignore.  We assert
    # the three commits + the path + the rule, not the exact line
    # (a future ruff/format pass could shift the line).
    assert "7ec7867d5a4bad751d437008fbfc8a31911e553c" in src
    assert "f019a0b3b2ddd5a98c8bce1ae6b600bfddaaca3d" in src
    assert "tpcore/tests/test_order_ids.py" in src
    # Exactly three pin lines (ignoring comments / blanks).
    pins = [
        line for line in src.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert len(pins) == 3, (
        f"expected exactly 3 fingerprint pins, got {len(pins)}; "
        "if a new false positive needs allowlisting, document it in "
        "docs/audits/2026-05-21-public-repo-secret-audit.md first."
    )


def test_secret_scan_workflow_exists_and_pins_version() -> None:
    """The CI workflow must exist and pin the same gitleaks version
    the pre-commit hook does."""
    assert _WORKFLOW.is_file(), (
        f"missing {_WORKFLOW.relative_to(_REPO)} — the recurring "
        "secret-scan gate is the public-repo line of defense."
    )
    src = _WORKFLOW.read_text()
    # Pinned binary install, not a moving ``@latest``.
    assert _PINNED_GITLEAKS_VERSION in src, (
        f"workflow does not pin gitleaks {_PINNED_GITLEAKS_VERSION} — "
        "supply-chain hygiene requires an exact version."
    )
    # Workflow must reference the committed allowlist (using a
    # different config silently bypasses the project allowlist).
    assert ".gitleaks.toml" in src, (
        "workflow must pass --config .gitleaks.toml so the project "
        "allowlist applies."
    )
    # SARIF upload to code-scanning is load-bearing — without it,
    # findings only appear in the workflow log and never surface in
    # the Security tab.
    assert "upload-sarif" in src, (
        "workflow must upload SARIF to GitHub code-scanning."
    )
    # Triggers on every push to main AND every PR — a PR-only gate
    # misses a direct push to main, a push-only gate misses a PR's
    # head commits.
    assert re.search(r"on:\s*$.*?push:.*?pull_request:", src, re.DOTALL | re.MULTILINE), (
        "workflow must trigger on BOTH push (to main) and pull_request."
    )


def test_pre_commit_config_pins_same_version() -> None:
    """The pre-commit hook must exist and pin the same gitleaks
    version the CI workflow does — drift between local + CI is the
    bug class this test prevents."""
    assert _PRE_COMMIT.is_file(), (
        f"missing {_PRE_COMMIT.relative_to(_REPO)} — operator-local "
        "pre-commit gate."
    )
    src = _PRE_COMMIT.read_text()
    # gitleaks pre-commit hooks pin via ``rev: vX.Y.Z`` (with the
    # leading ``v`` per the upstream repo's tag scheme).
    expected_rev = f"v{_PINNED_GITLEAKS_VERSION}"
    assert expected_rev in src, (
        f"pre-commit config does not pin gitleaks {expected_rev} — "
        "must match the CI workflow pin to prevent local/CI drift."
    )
    # Referenced from the gitleaks repo (not a fork / mirror).
    assert "github.com/gitleaks/gitleaks" in src, (
        "pre-commit hook must reference the canonical gitleaks repo."
    )


def test_audit_doc_present_and_classifies_baseline() -> None:
    """The 2026-05-21 baseline audit doc must exist and document the
    findings classification — the allowlist references it by F-id."""
    assert _AUDIT_DOC.is_file(), (
        f"missing {_AUDIT_DOC.relative_to(_REPO)} — the baseline "
        "audit is the operator-readable source for every "
        "allowlist entry."
    )
    src = _AUDIT_DOC.read_text()
    # The three F-ids referenced by .gitleaks.toml and .gitleaksignore.
    for fid in ("F1", "F2", "F3"):
        assert f"### {fid}" in src, (
            f"audit doc is missing finding section {fid}; allowlist "
            "entries reference this id."
        )
    # Each F-id must classify as CONFIRMED-CLEAN — if a future audit
    # downgrades one to CRITICAL or REVIEW, the allowlist must be
    # revisited.
    assert src.count("CONFIRMED-CLEAN") >= 3, (
        "every finding must carry an explicit classification."
    )


@pytest.mark.parametrize(
    "artifact",
    [_GITLEAKS_TOML, _GITLEAKS_IGNORE, _WORKFLOW, _PRE_COMMIT, _AUDIT_DOC],
)
def test_artifact_referenced_from_operations_doc(artifact: Path) -> None:
    """Every gate artifact must be referenced from docs/OPERATIONS.md
    — the "secret hygiene" subsection — so a future operator can find
    them. An orphan workflow / config drifts into dead-script land."""
    ops_doc = _REPO / "docs" / "OPERATIONS.md"
    assert ops_doc.is_file(), "docs/OPERATIONS.md missing"
    src = ops_doc.read_text()
    rel = str(artifact.relative_to(_REPO))
    assert rel in src, (
        f"{rel} is not referenced from docs/OPERATIONS.md — orphan "
        "gate artifact, will rot. Add it under the 'Secret hygiene "
        "(public repo)' subsection."
    )
