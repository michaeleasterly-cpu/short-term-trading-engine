"""2026-06-03 — Claude Code workflow controls audit presence sentinel.

Pins the load-bearing claims of
``docs/audits/2026-06-03-claude-code-workflow-controls.md`` and the
"no implementation included" boundary.

The audit is docs-only and design-only. This sentinel enforces:
  1. The audit doc exists, is non-empty, and is referenced from TODO.md.
  2. The audit names Anthropic Claude Code documentation as the primary
     authority.
  3. The audit states the database failure is a case study, NOT the
     audit scope.
  4. The audit includes the System-Wide Verification gate design.
  5. The audit includes the Change-Impact Classification gate design.
  6. The audit states no targeted fix may be proposed before
     system-wide verification.
  7. The audit states local fixes are blocked when the defect is
     systemic.
  8. The audit includes the DISCOVERY_REQUIRED control verdict.
  9. The audit includes the subagent branch-base verification control.
 10. The audit includes the Claude review credit-spend controls.
 11. The audit states docs-only PRs should not trigger paid Claude
     review.
 12. The audit states no implementation is included.
 13. The audit states no DB writes, no migrations, no table
     creation, no code changes, no .claude changes, no workflow
     changes are included in the PR.

Substring-presence checks (not section-anchored parsing) on purpose —
phrasing can evolve, but the load-bearing concepts must remain
mentioned. A future revision that removes one of these mentions
reds CI so the operator gets a chance to re-confirm the boundary
survives the rewrite.

Per ``.claude/rules/tests-and-ci.md``: this test runs no ``git``,
``gh``, or DB access — pure filesystem reads of tracked docs.
"""
from __future__ import annotations

from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_AUDIT = _REPO / "docs" / "audits" / "2026-06-03-claude-code-workflow-controls.md"
_TODO = _REPO / "TODO.md"


def _audit_text() -> str:
    assert _AUDIT.is_file(), (
        f"missing {_AUDIT.relative_to(_REPO)} — the docs-only audit "
        "from the 2026-06-03 Claude Code workflow controls pass"
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
    """The audit doc exists at its canonical path."""
    _audit_text()


def test_todo_points_at_audit_doc() -> None:
    """TODO.md references the audit doc by path."""
    todo = _todo_text()
    assert "docs/audits/2026-06-03-claude-code-workflow-controls.md" in todo, (
        "TODO.md must reference the audit doc by path so 'what's next' "
        "decisions can see the moratoria and operator-decision queue"
    )


# ---------------------------------------------------------------------------
# 2. Anthropic documentation is the primary authority
# ---------------------------------------------------------------------------


def test_audit_names_anthropic_docs_as_primary_authority() -> None:
    """The audit's authority chain leads with Anthropic Claude Code docs."""
    text = _audit_text()
    assert "code.claude.com/docs" in text, (
        "audit must cite code.claude.com/docs as the primary authority"
    )
    assert "claude-code-action" in text, (
        "audit must cite anthropics/claude-code-action (source + examples + "
        "docs/security.md) as the secondary authority"
    )


# ---------------------------------------------------------------------------
# 3. Database failure is a CASE STUDY, not the audit scope
# ---------------------------------------------------------------------------


def test_audit_scopes_database_failure_as_case_study_only() -> None:
    """The audit must declare the DB failure as a case study, not its scope."""
    text = _audit_text()
    assert "case study" in text.lower(), (
        "audit must declare the 2026-06-02 identity-substrate failure as a "
        "CASE STUDY used to design controls, not the audit subject"
    )


# ---------------------------------------------------------------------------
# 4. System-Wide Verification gate is designed in the audit
# ---------------------------------------------------------------------------


def test_audit_includes_system_wide_verification_gate() -> None:
    """The SWV gate design is present."""
    text = _audit_text()
    assert "System-Wide Verification" in text, (
        "audit must include the System-Wide Verification (SWV) gate design"
    )
    assert "SWV" in text, "audit must use the SWV abbreviation consistently"


# ---------------------------------------------------------------------------
# 5. Change-Impact Classification gate is designed in the audit
# ---------------------------------------------------------------------------


def test_audit_includes_change_impact_classification_gate() -> None:
    """The CIC gate design is present."""
    text = _audit_text()
    assert "Change-Impact Classification" in text, (
        "audit must include the Change-Impact Classification (CIC) gate design"
    )
    assert "CIC" in text, "audit must use the CIC abbreviation consistently"


# ---------------------------------------------------------------------------
# 6. No targeted fix before system-wide verification
# ---------------------------------------------------------------------------


def test_audit_blocks_targeted_fix_before_swv() -> None:
    """The audit must state that no targeted fix is allowed before SWV."""
    text = _audit_text()
    assert "before any targeted fix" in text.lower() or "before a fix" in text.lower(), (
        "audit must state that no targeted fix may be proposed before "
        "system-wide verification"
    )


# ---------------------------------------------------------------------------
# 7. Local fixes are blocked when the defect is systemic
# ---------------------------------------------------------------------------


def test_audit_blocks_local_fix_when_defect_is_systemic() -> None:
    """The audit must state that local fixes are blocked when the defect is systemic."""
    text = _audit_text()
    assert "local but the defect is systemic" in text, (
        "audit must state: 'the proposed fix is local but the defect is "
        "systemic' as a blocking condition (returns DISCOVERY_REQUIRED)"
    )


# ---------------------------------------------------------------------------
# 8. DISCOVERY_REQUIRED verdict is part of the control vocabulary
# ---------------------------------------------------------------------------


def test_audit_includes_discovery_required_verdict() -> None:
    """The audit must define DISCOVERY_REQUIRED as a control verdict."""
    text = _audit_text()
    assert "DISCOVERY_REQUIRED" in text, (
        "audit must define DISCOVERY_REQUIRED as a verdict returned by the "
        "SWV and CIC gates when discovery is incomplete"
    )


# ---------------------------------------------------------------------------
# 9. Subagent branch-base verification control is designed
# ---------------------------------------------------------------------------


def test_audit_includes_subagent_branch_base_control() -> None:
    """The audit must design a subagent branch-base verification control."""
    text = _audit_text()
    assert "branch-base" in text.lower() or "branch base" in text.lower(), (
        "audit must design a subagent branch-base verification control "
        "(the recent failure where a subagent PR was on the wrong base)"
    )
    assert "baseRef" in text, (
        "audit must name the Anthropic-documented worktree.baseRef setting "
        "as the canonical mechanism for branch-base control"
    )


# ---------------------------------------------------------------------------
# 10. Claude review / credit-spend controls are designed
# ---------------------------------------------------------------------------


def test_audit_includes_claude_review_credit_controls() -> None:
    """The audit must design Claude review credit-spend controls."""
    text = _audit_text()
    assert "--max-turns" in text, (
        "audit must propose --max-turns as a Claude review credit-spend cap"
    )
    assert "credit" in text.lower(), (
        "audit must name credit-spend as a control axis"
    )


# ---------------------------------------------------------------------------
# 11. Docs-only PRs should not trigger paid Claude review
# ---------------------------------------------------------------------------


def test_audit_states_docs_only_prs_should_not_trigger_paid_review() -> None:
    """The audit must propose a docs-only PR carve-out for the paid review."""
    text = _audit_text()
    assert "docs-only" in text.lower(), (
        "audit must name the docs-only PR carve-out as a credit-spend control"
    )
    # The proposed mechanism is GitHub Actions paths-ignore — canonical, not invented.
    assert "paths-ignore" in text, (
        "audit must propose paths-ignore (GitHub Actions canonical lever) "
        "as the docs-only carve-out mechanism, NOT an invented mechanism"
    )


# ---------------------------------------------------------------------------
# 12. No implementation is included in this PR
# ---------------------------------------------------------------------------


def test_audit_states_no_implementation_included() -> None:
    """The audit must state explicitly that no implementation is included."""
    text = _audit_text()
    assert "No implementation is included" in text, (
        "audit must contain a §14 statement: 'No implementation is included "
        "in this PR.' This is the boundary the operator relies on"
    )


# ---------------------------------------------------------------------------
# 13. No DB writes, migrations, table creation, code changes,
#     .claude changes, or workflow changes are included
# ---------------------------------------------------------------------------


def test_audit_states_no_db_writes() -> None:
    """No DB writes."""
    text = _audit_text()
    assert "No DB writes" in text, "audit must state: No DB writes"


def test_audit_states_no_migrations() -> None:
    """No migrations."""
    text = _audit_text()
    assert "No migrations" in text, "audit must state: No migrations"


def test_audit_states_no_table_creation_or_drops() -> None:
    """No table creation or drops."""
    text = _audit_text()
    assert "No table creation" in text, (
        "audit must state: No table creation"
    )
    assert "drops" in text or "drop" in text.lower(), (
        "audit must state: No table drops"
    )


def test_audit_states_no_code_changes() -> None:
    """No code changes."""
    text = _audit_text()
    assert "No code changes" in text, "audit must state: No code changes"


def test_audit_states_no_claude_changes() -> None:
    """No .claude/ changes."""
    text = _audit_text()
    assert "No `.claude/` changes" in text, (
        "audit must state: No `.claude/` changes (no new rules, skills, "
        "agents, hooks)"
    )


def test_audit_states_no_workflow_changes() -> None:
    """No .github/workflows/ changes."""
    text = _audit_text()
    assert "No `.github/workflows/` changes" in text, (
        "audit must state: No `.github/workflows/` changes"
    )


# ---------------------------------------------------------------------------
# 14. The operator-decisions queue is enumerated
# ---------------------------------------------------------------------------


def test_audit_enumerates_operator_decisions() -> None:
    """The audit must enumerate the deferred operator decisions."""
    text = _audit_text()
    assert "operator decisions required" in text.lower() or "operator must decide" in text.lower(), (
        "audit must enumerate the deferred operator-decision queue (§13). "
        "Implementation cannot start until those decisions are recorded"
    )


def test_todo_carries_operator_decisions() -> None:
    """TODO.md must carry the operator-decisions queue so it survives memory audits."""
    todo = _todo_text()
    assert "Operator decisions required" in todo, (
        "TODO.md must carry the operator-decisions queue from the audit so "
        "the queue survives memory audits (TODO.md is the canonical work tracker)"
    )


# ---------------------------------------------------------------------------
# 15. Authority order is documented
# ---------------------------------------------------------------------------


def test_audit_documents_authority_order() -> None:
    """The audit must document the authority order so future audits can reproduce it."""
    text = _audit_text()
    assert "Authority order" in text, (
        "audit must document the authority order (Anthropic docs → public "
        "repos → this repo → lived practice) so future audits inherit the "
        "same evidence chain"
    )
