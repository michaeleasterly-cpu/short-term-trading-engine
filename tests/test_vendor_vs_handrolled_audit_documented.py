"""2026-06-03 PM — Vendor-vs-hand-rolled audit presence sentinel.

Pins the load-bearing claims of
``docs/audits/2026-06-03-vendor-vs-handrolled.md`` and the
"no implementation included" boundary.

The audit is docs-only and design-only. This sentinel enforces:
  1. The audit doc exists, is non-empty, and is referenced from TODO.md.
  2. The audit names Anthropic public repos as the secondary authority
     after the official docs (per the morning audit's authority chain).
  3. The audit covers all 6 hand-rolled STE surfaces enumerated in the
     morning audit's "What the original audit got wrong" section:
     security-guidance, pr-review-toolkit, feature-dev, hookify,
     financial-services managed-agent cookbooks, commit-commands.
  4. The audit produces a per-surface VENDOR / HYBRID / DIVERGED
     recommendation.
  5. The audit names the operator-decisions queue so the operator
     authorization is required before any control becomes live.
  6. The audit states no implementation is included.
  7. The audit states no DB writes, no migrations, no table creation,
     no code changes, no .claude changes, no workflow changes are
     included in the PR.

Substring-presence checks (not section-anchored parsing) on purpose —
phrasing can evolve, but the load-bearing concepts must remain
mentioned. A future revision that removes one of these mentions reds
CI so the operator can re-confirm the boundary survives the rewrite.

Per ``.claude/rules/tests-and-ci.md``: this test runs no ``git``,
``gh``, or DB access — pure filesystem reads of tracked docs.
"""
from __future__ import annotations

from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_AUDIT = _REPO / "docs" / "audits" / "2026-06-03-vendor-vs-handrolled.md"
_TODO = _REPO / "TODO.md"


def _audit_text() -> str:
    assert _AUDIT.is_file(), (
        f"missing {_AUDIT.relative_to(_REPO)} — the docs-only vendor "
        "audit from the 2026-06-03 PM pass"
    )
    text = _AUDIT.read_text(encoding="utf-8")
    assert text.strip(), f"{_AUDIT.relative_to(_REPO)} is empty"
    return text


def _todo_text() -> str:
    assert _TODO.is_file(), f"missing {_TODO.relative_to(_REPO)}"
    text = _TODO.read_text(encoding="utf-8")
    assert text.strip(), f"{_TODO.relative_to(_REPO)} is empty"
    return text


# ---------------------------------------------------------------------------
# 1. The audit doc exists and is referenced from TODO.md
# ---------------------------------------------------------------------------


def test_audit_doc_present() -> None:
    _audit_text()


def test_todo_points_at_audit_doc() -> None:
    todo = _todo_text()
    assert "docs/audits/2026-06-03-vendor-vs-handrolled.md" in todo, (
        "TODO.md must reference the vendor audit doc by path so "
        "'what's next' decisions can see the operator-decision queue"
    )


# ---------------------------------------------------------------------------
# 2. Authority chain extends the morning audit's
# ---------------------------------------------------------------------------


def test_audit_extends_morning_audit_authority_chain() -> None:
    text = _audit_text()
    assert "2026-06-03-claude-code-workflow-controls.md" in text, (
        "audit must cite the morning audit it follows up from"
    )
    assert "code.claude.com" in text or "Anthropic Claude Code documentation" in text, (
        "audit must cite Anthropic Claude Code documentation as the "
        "primary authority"
    )
    for repo in (
        "anthropics/claude-code",
        "anthropics/claude-code-action",
        "anthropics/financial-services",
        "anthropics/claude-plugins-official",
        "anthropics/skills",
    ):
        assert repo in text, (
            f"audit must cite {repo} so the evidence chain is "
            "reproducible"
        )


# ---------------------------------------------------------------------------
# 3. Six STE-hand-rolled surfaces are each covered
# ---------------------------------------------------------------------------


def test_audit_covers_security_guidance_surface() -> None:
    text = _audit_text()
    assert "security-guidance" in text, (
        "audit must cover the security-guidance surface comparison"
    )


def test_audit_covers_pr_review_toolkit_surface() -> None:
    text = _audit_text()
    assert "pr-review-toolkit" in text, (
        "audit must cover the pr-review-toolkit surface comparison"
    )


def test_audit_covers_feature_dev_surface() -> None:
    text = _audit_text()
    assert "feature-dev" in text, (
        "audit must cover the feature-dev surface comparison"
    )


def test_audit_covers_hookify_surface() -> None:
    text = _audit_text()
    assert "hookify" in text, (
        "audit must cover the hookify surface comparison"
    )


def test_audit_covers_finsvc_managed_agent_cookbooks_surface() -> None:
    text = _audit_text()
    assert "managed-agent-cookbooks" in text, (
        "audit must cover the financial-services managed-agent-cookbooks "
        "surface comparison"
    )


def test_audit_covers_commit_commands_surface() -> None:
    text = _audit_text()
    assert "commit-commands" in text, (
        "audit must cover the commit-commands surface comparison"
    )


# ---------------------------------------------------------------------------
# 4. Per-surface recommendation vocabulary
# ---------------------------------------------------------------------------


def test_audit_uses_per_surface_recommendation_vocabulary() -> None:
    """The audit must use a clear per-surface recommendation
    vocabulary so the operator can read disposition at a glance."""
    text = _audit_text()
    # At least these three dispositions must appear — the audit makes
    # different recommendations per surface.
    assert "Vendor" in text, "audit must use 'Vendor' as a disposition"
    assert "Stay diverged" in text or "stay diverged" in text, (
        "audit must use 'Stay diverged' as a disposition"
    )
    assert "Hybrid" in text or "Vendor 2 of" in text, (
        "audit must use 'Hybrid' (or equivalent) as a disposition for "
        "partial-vendoring surfaces"
    )


# ---------------------------------------------------------------------------
# 5. Operator decisions enumerated
# ---------------------------------------------------------------------------


def test_audit_enumerates_operator_decisions() -> None:
    text = _audit_text()
    assert "Operator decisions required" in text or "operator must decide" in text.lower(), (
        "audit must enumerate the deferred operator-decision queue. "
        "Implementation cannot start until those decisions are recorded"
    )


def test_todo_carries_operator_decisions() -> None:
    todo = _todo_text()
    assert "vendor" in todo.lower(), (
        "TODO.md must reference the vendor-audit operator decisions"
    )


# ---------------------------------------------------------------------------
# 6. No implementation is included
# ---------------------------------------------------------------------------


def test_audit_states_no_implementation_included() -> None:
    text = _audit_text()
    assert "No implementation is included" in text, (
        "audit must contain a §10 statement: 'No implementation is "
        "included in this PR.' This is the boundary the operator "
        "relies on"
    )


# ---------------------------------------------------------------------------
# 7. Scope guards
# ---------------------------------------------------------------------------


def test_audit_states_no_db_writes() -> None:
    text = _audit_text()
    assert "No DB writes" in text, "audit must state: No DB writes"


def test_audit_states_no_migrations() -> None:
    text = _audit_text()
    assert "No migrations" in text, "audit must state: No migrations"


def test_audit_states_no_code_changes() -> None:
    text = _audit_text()
    assert "No code changes" in text, "audit must state: No code changes"


def test_audit_states_no_claude_changes() -> None:
    text = _audit_text()
    assert "No `.claude/` changes" in text, (
        "audit must state: No `.claude/` changes (no new rules, "
        "skills, agents, hooks)"
    )


def test_audit_states_no_workflow_changes() -> None:
    text = _audit_text()
    assert "No `.github/workflows/` changes" in text, (
        "audit must state: No `.github/workflows/` changes"
    )
