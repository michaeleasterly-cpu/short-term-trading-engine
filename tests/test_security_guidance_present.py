"""C0.4 (2026-06-01) — security-guidance cascade presence sentinels.

Pins the operator-facing security policy doc, the path-loaded
security rule, and the model-invocable ``/security-review`` skill
against silent deletion or drift. Mirrors the H0 / C0.1 / C0.3
pattern: substring-presence checks on load-bearing concepts, not
semantic parsing — phrasing can evolve while the structure stays.

Stdlib-only (pathlib + re).
"""
from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_DOC = _REPO / "docs" / "SECURITY_GUIDANCE.md"
_RULE = _REPO / ".claude" / "rules" / "security-guidance.md"
_SKILL = _REPO / ".claude" / "skills" / "security-review" / "SKILL.md"

_AGENT_SKILL_FORBIDDEN_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bauto[- ]?merge\b", "auto-merge"),
    (r"\bauto[- ]?fix\b", "auto-fix"),
    (r"\bauto[- ]?rebase\b", "auto-rebase"),
    (r"\bgh\s+pr\s+merge\b", "gh pr merge"),
    (r"\bgit\s+push\s+[^\n]*(--force|-f\s)", "git push --force"),
    (r"\bdocker\s+(run|build|compose|exec|push|pull)\b", "docker invocation"),
    (r"\brailway\s+(up|deploy)\b", "railway deploy"),
    (r"/memory_stores/[^\s]+/memories", "memstore API mutation endpoint"),
)

_NEGATION_WINDOW = 80
_NEGATION_TERMS = (
    "do not", "don't", "never", "must not", "must NOT", "MUST NOT",
    "cannot", "can not", "can't", "isn't", "doesn't", "won't",
    "prohibit", "prohibited", "forbid", "forbidden",
    "block", "blocks", "blocked", "refuse", "refuses",
    "reject", "rejects", "without", "no ", "NEVER",
    "out of scope",
    # The doc, rule, and skill discuss what the layers *must not* do
    # in checklist form; substring-presence of these review-rubric
    # terms in the 80-char window counts as enforcing-not-authorizing.
    "review-only", "review only", "redact", "redacted", "redacting",
    "example",
)


def _doc_text() -> str:
    assert _DOC.is_file(), f"missing {_DOC.relative_to(_REPO)}"
    text = _DOC.read_text(encoding="utf-8")
    assert text.strip(), f"{_DOC.relative_to(_REPO)} is empty"
    return text


def _rule_text() -> str:
    assert _RULE.is_file(), f"missing {_RULE.relative_to(_REPO)}"
    text = _RULE.read_text(encoding="utf-8")
    assert text.strip(), f"{_RULE.relative_to(_REPO)} is empty"
    return text


def _skill_text() -> str:
    assert _SKILL.is_file(), f"missing {_SKILL.relative_to(_REPO)}"
    text = _SKILL.read_text(encoding="utf-8")
    assert text.strip(), f"{_SKILL.relative_to(_REPO)} is empty"
    return text


def _markdown_body(text: str) -> str:
    if not text.startswith("---"):
        return text
    closing = re.search(r"\n---\s*\n", text)
    return text if closing is None else text[closing.end():]


def _has_negation_nearby(text: str, idx: int) -> bool:
    window = text[max(0, idx - _NEGATION_WINDOW):idx].lower()
    return any(term.lower() in window for term in _NEGATION_TERMS)


def _has_unguarded_forbidden_match(text: str) -> tuple[str, str] | None:
    for pattern, label in _AGENT_SKILL_FORBIDDEN_PATTERNS:
        for m in re.finditer(pattern, text, flags=re.IGNORECASE):
            if _has_negation_nearby(text, m.start()):
                continue
            return label, m.group(0)
    return None


# ─────────────────────────────────────────────────────────────────────
# Doc presence + structure
# ─────────────────────────────────────────────────────────────────────

def test_security_guidance_doc_present() -> None:
    """docs/SECURITY_GUIDANCE.md exists and is non-empty."""
    _doc_text()


def test_doc_names_three_layer_cascade() -> None:
    """The doc must name all three layers of the cascade."""
    text = _doc_text()
    lowered = text.lower()
    for marker in ("static", "claude review", "operator gate"):
        assert marker in lowered, (
            f"SECURITY_GUIDANCE.md must name {marker!r} as a layer "
            "of the 3-layer cascade"
        )


def test_doc_names_finding_classification_taxonomy() -> None:
    """The doc must enumerate the three finding classes."""
    text = _doc_text()
    for klass in ("BLOCKING", "NEEDS_OPERATOR_REVIEW", "ADVISORY"):
        assert klass in text, (
            f"SECURITY_GUIDANCE.md must include the {klass!r} "
            "finding class"
        )


def test_doc_lists_security_sensitive_diff_classes() -> None:
    """The doc must enumerate the security-sensitive diff classes —
    at least the high-signal ones from the C0.4 spec."""
    text = _doc_text()
    lowered = text.lower()
    required_markers = (
        "workflow",
        "secret",
        "auth",
        "credential",
        "mcp",
        "deployment",
        "dependency",
        "memory",
    )
    missing = [m for m in required_markers if m not in lowered]
    assert not missing, (
        "SECURITY_GUIDANCE.md must mention every security-sensitive "
        f"diff class; missing: {missing}"
    )


def test_doc_references_rule_and_skill() -> None:
    """The doc must point at the path-loaded rule + manual skill so a
    reader can find both companion artifacts."""
    text = _doc_text()
    for ref in (
        ".claude/rules/security-guidance.md",
        ".claude/skills/security-review/SKILL.md",
    ):
        assert ref in text, (
            f"SECURITY_GUIDANCE.md must reference {ref}"
        )


# ─────────────────────────────────────────────────────────────────────
# Rule presence + structure
# ─────────────────────────────────────────────────────────────────────

def test_security_rule_frontmatter_present() -> None:
    """.claude/rules/security-guidance.md exists and starts with YAML
    frontmatter declaring a paths: glob."""
    text = _rule_text()
    assert text.startswith("---"), (
        "security-guidance.md must start with YAML frontmatter"
    )
    # Frontmatter must declare a paths: list — that's the path-scoped
    # rule mechanism's whole point.
    closing = re.search(r"\n---\s*\n", text)
    assert closing is not None, (
        "security-guidance.md frontmatter must close with `---`"
    )
    frontmatter = text[: closing.start()]
    assert "paths:" in frontmatter, (
        "security-guidance.md frontmatter must declare a paths: glob"
    )


def test_rule_body_points_to_doc() -> None:
    """Rule body must point at the canonical doc."""
    body = _markdown_body(_rule_text())
    assert "docs/SECURITY_GUIDANCE.md" in body, (
        "security-guidance rule body must reference "
        "docs/SECURITY_GUIDANCE.md"
    )


# ─────────────────────────────────────────────────────────────────────
# Skill presence + structure
# ─────────────────────────────────────────────────────────────────────

def test_security_review_skill_frontmatter_present() -> None:
    """.claude/skills/security-review/SKILL.md exists with YAML
    frontmatter."""
    text = _skill_text()
    assert text.startswith("---"), (
        "SKILL.md must start with YAML frontmatter"
    )
    closing = re.search(r"\n---\s*\n", text)
    assert closing is not None, (
        "SKILL.md frontmatter must close with `---`"
    )
    frontmatter = text[: closing.start()]
    assert "name:" in frontmatter, (
        "SKILL.md frontmatter must declare a name"
    )
    assert "description:" in frontmatter, (
        "SKILL.md frontmatter must declare a description"
    )


def test_skill_is_model_invocable() -> None:
    """The skill is intentionally model-invocable so the rule body
    can direct Claude to suggest invoking it on a security-sensitive
    diff. ``disable-model-invocation: true`` would make the skill
    slash-only and break that path."""
    text = _skill_text()
    closing = re.search(r"\n---\s*\n", text)
    assert closing is not None
    frontmatter = text[: closing.start()]
    assert "disable-model-invocation: true" not in frontmatter, (
        "security-review skill must remain model-invocable; remove "
        "`disable-model-invocation: true` from the frontmatter"
    )


def test_skill_body_is_review_only() -> None:
    """The skill body must explicitly state review-only intent."""
    body = _markdown_body(_skill_text()).lower()
    assert "review-only" in body or "review only" in body, (
        "security-review SKILL.md must state review-only intent"
    )


# ─────────────────────────────────────────────────────────────────────
# Cross-tier forbidden-action scan (negation-aware)
# ─────────────────────────────────────────────────────────────────────

def test_doc_does_not_authorize_forbidden_actions() -> None:
    """The doc must not authorize auto-fix, auto-merge, force-push,
    docker / railway deploy, or memstore mutations. Negation-aware."""
    finding = _has_unguarded_forbidden_match(_doc_text())
    assert finding is None, (
        f"SECURITY_GUIDANCE.md authorizes forbidden action "
        f"{finding[0]!r}: {finding[1]!r} (no negation nearby)"
    )


def test_rule_does_not_authorize_forbidden_actions() -> None:
    """The rule body must not authorize auto-fix / auto-merge / etc.
    Negation-aware so `do not auto-merge` survives the scan."""
    finding = _has_unguarded_forbidden_match(_markdown_body(_rule_text()))
    assert finding is None, (
        f"security-guidance.md rule body authorizes forbidden action "
        f"{finding[0]!r}: {finding[1]!r} (no negation nearby)"
    )


def test_skill_does_not_authorize_forbidden_actions() -> None:
    """The skill body must not authorize auto-fix / auto-merge / etc.
    Negation-aware."""
    finding = _has_unguarded_forbidden_match(_markdown_body(_skill_text()))
    assert finding is None, (
        f"security-review SKILL.md authorizes forbidden action "
        f"{finding[0]!r}: {finding[1]!r} (no negation nearby)"
    )
