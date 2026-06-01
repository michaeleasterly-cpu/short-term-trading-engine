"""Anti-rot sentinel for ``.claude/skills/`` invocable wrappers.

Each named skill must exist as ``.claude/skills/<name>/SKILL.md`` with
YAML frontmatter containing ``name:`` and ``description:`` keys + a
non-empty body. Mirrors the SP-D ``test_vocabulary_is_exactly_pinned``
exhaustive-vocabulary pattern.

Authoritative external: <https://code.claude.com/docs/en/skills>.
"""
from __future__ import annotations

from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_SKILLS_DIR = _REPO / ".claude" / "skills"

# (name, model_invocable) — readiness checklists are intentionally
# model-invocable so Claude auto-triggers them on engine/adapter work;
# wrapper skills are slash-only (``disable-model-invocation: true``).
_SKILLS = (
    ("engine-readiness", True),
    ("adapter-readiness", True),
    ("lab-target-run", False),
    # 2026-05-25 RETIRED — `lab-spec-emit` (SP-G LAB-EMITTER) +
    # `lab-edge-find` (Task #25 EDGE-FINDER) removed alongside the
    # operator-local LLM lab/finder/monitor stack ("it is out",
    # Railway-readiness retirement).
    ("ecr", False),
    ("dfcr", False),
    ("audit-data-pipeline", False),
    ("run-data-ops", False),
    ("weekly-digest", False),
    ("defect-register", False),
    # C0.4 — model-invocable security-review skill walked by the
    # security-guidance rule on security-sensitive diffs.
    ("security-review", True),
)


@pytest.mark.parametrize(("name", "model_invocable"), _SKILLS)
def test_skill_file_present_and_well_formed(name: str, model_invocable: bool) -> None:
    """Every named skill exists, has ``name:`` + ``description:`` keys
    in YAML frontmatter, a non-empty body, and the
    ``disable-model-invocation`` value matches the expected
    invocability (model-invocable for readiness checklists; slash-only
    for wrappers)."""
    path = _SKILLS_DIR / name / "SKILL.md"
    assert path.is_file(), f"missing skill: {path}"
    src = path.read_text()
    assert src.startswith("---\n"), f"{path} missing YAML frontmatter open"
    parts = src.split("---\n", 2)
    assert len(parts) >= 3, f"{path} missing YAML frontmatter close"
    frontmatter, body = parts[1], parts[2]
    assert f"name: {name}" in frontmatter, f"{path} missing or wrong 'name:'"
    assert "description:" in frontmatter, f"{path} missing 'description:'"
    if model_invocable:
        assert "disable-model-invocation: true" not in frontmatter, (
            f"{path} is a readiness checklist; must stay model-invocable "
            "(no disable-model-invocation: true)")
    else:
        assert "disable-model-invocation: true" in frontmatter, (
            f"{path} is a wrapper skill; must carry "
            "'disable-model-invocation: true' (slash-only)")
    assert body.strip(), f"{path} has empty body"


def test_required_skill_set_is_present() -> None:
    """Every required project-authored skill dir is present on disk.

    Subset-not-equality: plugin-installed skills (Supabase, Vercel, etc.)
    naturally live alongside project-authored skills in
    ``.claude/skills/`` after a plugin install — they must not trip the
    sentinel. The `.gitignore` whitelist (parent) is what keeps the
    project-authored set in version control; this test is the
    presence-of-required-set tripwire."""
    on_disk = {p.parent.name for p in _SKILLS_DIR.glob("*/SKILL.md")}
    expected = {name for name, _ in _SKILLS}
    missing = expected - on_disk
    assert not missing, f"missing project-authored skills: {missing}"
