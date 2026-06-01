"""S2 override acceptance addendum sentinel.

Pins the doc-only addendum on
``docs/superpowers/plans/2026-06-01-ste-round-trip-dev-system-adoption-plan.md``
that closes the S2 audit loop by accepting the 20 audit_project
drift findings as intentional STE_OVERRIDE artifacts.

The sentinel asserts the load-bearing claims of that addendum stay
in the plan: a future "let me tidy the plan" refactor must not
silently drop the audit-acceptance text, the check_manifests CLEAN
statement, the no-overwrite re-assertion, or the enumerated
override surfaces.

Stdlib only.
"""
from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_PLAN = _REPO / "docs" / "superpowers" / "plans" / (
    "2026-06-01-ste-round-trip-dev-system-adoption-plan.md"
)


def _plan_text() -> str:
    assert _PLAN.is_file(), f"missing {_PLAN.relative_to(_REPO)}"
    text = _PLAN.read_text(encoding="utf-8")
    assert text.strip(), "plan doc is empty"
    return text


# ─────────────────────────────────────────────────────────────────────
# Required addendum surface
# ─────────────────────────────────────────────────────────────────────


def test_addendum_heading_present() -> None:
    text = _plan_text()
    assert "## S2 audit override acceptance" in text, (
        "plan must contain the S2 audit override acceptance heading"
    )


def test_addendum_states_check_manifests_clean() -> None:
    text = _plan_text()
    # Accept either "CLEAN" capitalisation form so a minor copy-edit
    # cannot silently invert the claim.
    assert re.search(
        r"check_manifests\.py\s*--target-dir\s*is\s*\*\*CLEAN\*\*",
        text,
    ) or re.search(
        r"check_manifests.*?\*\*CLEAN\*\*",
        text,
    ), (
        "addendum must explicitly state check_manifests --target-dir "
        "is CLEAN"
    )


def test_addendum_states_drift_is_advisory() -> None:
    text = _plan_text()
    assert "advisory" in text and (
        "do not authorize" in text.lower()
        or "do not authorise" in text.lower()
    ), (
        "addendum must declare audit drift advisory and forbid "
        "automatic overwrite authorization"
    )


def test_addendum_forbids_blind_overwrite() -> None:
    text = _plan_text()
    assert "bootstrap_project.py --target-dir" in text, (
        "addendum must reference the forbidden bootstrap regen path"
    )
    assert "--force" in text, (
        "addendum must explicitly forbid the --force pathway against STE"
    )


def test_addendum_uses_ste_override_label() -> None:
    """The shared classification vocabulary must remain consistent."""
    text = _plan_text()
    assert "STE_OVERRIDE" in text, (
        "addendum must use the STE_OVERRIDE classification label"
    )


def test_addendum_enumerates_major_override_surfaces() -> None:
    """The 20-finding table must call out the major STE-canonical
    surfaces by name so the addendum stays grounded in the same
    enumeration audit_project produces."""
    text = _plan_text()
    required_surfaces = (
        ".claude/rules/heavy-lane.md",
        ".claude/rules/security-guidance.md",
        ".claude/skills/security-review/SKILL.md",
        ".claude/hooks/block-git-checkout.sh",
        ".claude/hooks/session-start.sh",
        ".claude/agents/code-quality-reviewer.md",
        ".claude/agents/spec-reviewer.md",
        ".github/workflows/secret-scan.yml",
        ".github/workflows/ci.yml",
        ".github/workflows/claude-review-heavy-lane.yml",
        ".github/pull_request_template.md",
        ".gitleaks.toml",
        "docs/MEMSTORE_HANDOFF.md",
        "docs/SECURITY_GUIDANCE.md",
        ".claude/path_registry.yaml",
        ".claude/settings.json",
    )
    missing: list[str] = []
    for surface in required_surfaces:
        if surface not in text:
            missing.append(surface)
    assert not missing, (
        f"addendum must enumerate the following STE-canonical override "
        f"surfaces by name: {missing}"
    )


def test_addendum_cross_references_prior_pr_chain() -> None:
    """The addendum grounds itself in the previously-merged chain so
    a fresh reader can audit the trail."""
    text = _plan_text()
    for pr_ref in ("#416", "#417", "#418", "#419", "#420"):
        assert pr_ref in text, (
            f"addendum must reference prior PR {pr_ref} for traceability"
        )


# ─────────────────────────────────────────────────────────────────────
# Safety surface — the doc-only addendum must not introduce any
# secret-shape literal or Anthropic API write surface.
# ─────────────────────────────────────────────────────────────────────


def test_addendum_introduces_no_raw_memstore_id() -> None:
    """The plan doc must never contain a 20+-char memstore-shape
    literal. Reference is documented via path pointer only — see
    PROJECT_PROFILE.yaml ``memstore_reference`` /
    ``docs/MEMSTORE_HANDOFF.md`` discipline."""
    text = _plan_text()
    memstore_id_re = re.compile(r"\bmemstore_[A-Za-z0-9]{20,}\b")
    matches = memstore_id_re.findall(text)
    assert not matches, (
        f"plan doc must not contain raw memstore-ID literal(s): "
        f"count={len(matches)}"
    )


def test_addendum_no_anthropic_api_write_surface() -> None:
    """No HTTP-to-Anthropic call shape in the plan doc — it discusses
    the boundary policy, but never authorises an API call."""
    text = _plan_text()
    forbidden_patterns = (
        r"curl\s+-?[^\n]*api\.anthropic\.com",
        r"-X\s+(?:POST|PUT|PATCH|DELETE)[^\n]*/v1/memory_stores",
    )
    findings: list[str] = []
    for pat in forbidden_patterns:
        if re.search(pat, text, re.IGNORECASE):
            findings.append(pat)
    assert not findings, (
        f"plan doc must not authorise an Anthropic API write: "
        f"{findings}"
    )
