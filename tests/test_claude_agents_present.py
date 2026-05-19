"""Anti-rot sentinel for ``.claude/agents/`` named subagent profiles.

Each named profile must exist with YAML frontmatter containing
``name:`` + ``description:`` + ``tools:`` keys and a non-empty body.
Mirrors the rules+skills sentinel pattern; subset-not-equality on the
on-disk set so plugin-installed agents (if any) don't trip it.

Authoritative external: <https://code.claude.com/docs/en/sub-agents>.
"""
from __future__ import annotations

from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_AGENTS_DIR = _REPO / ".claude" / "agents"

_AGENTS = (
    "spec-reviewer",
    "code-quality-reviewer",
    "engine-implementer",
    "adapter-implementer",
    "lab-target-runner",
)


@pytest.mark.parametrize("name", _AGENTS)
def test_agent_file_present_and_well_formed(name: str) -> None:
    """Every named agent profile exists, has the required frontmatter
    keys, and carries a non-empty body."""
    path = _AGENTS_DIR / f"{name}.md"
    assert path.is_file(), f"missing agent profile: {path}"
    src = path.read_text()
    assert src.startswith("---\n"), f"{path} missing YAML frontmatter open"
    parts = src.split("---\n", 2)
    assert len(parts) >= 3, f"{path} missing YAML frontmatter close"
    frontmatter, body = parts[1], parts[2]
    assert f"name: {name}" in frontmatter, (
        f"{path} missing or wrong 'name:' key")
    assert "description:" in frontmatter, f"{path} missing 'description:'"
    assert "tools:" in frontmatter, (
        f"{path} missing 'tools:' key — per "
        "https://code.claude.com/docs/en/sub-agents subagent profiles "
        "must declare their tool budget")
    assert body.strip(), f"{path} has empty body"


def test_required_agent_set_is_present() -> None:
    """Every required project-authored agent profile is present.

    Subset-not-equality (mirrors the skills sentinel) so plugin-installed
    agents don't trip the tripwire."""
    on_disk = {p.stem for p in _AGENTS_DIR.glob("*.md")}
    expected = set(_AGENTS)
    missing = expected - on_disk
    assert not missing, f"missing project-authored agents: {missing}"
