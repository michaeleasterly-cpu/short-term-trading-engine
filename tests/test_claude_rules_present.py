"""Anti-rot sentinel for ``.claude/rules/`` path-scoped invariants.

Every rules file must exist, have a non-empty body, and carry a
``paths:`` key in its YAML frontmatter — the path-scoping is the
mechanism's whole point. Mirrors the
``tests/test_dev_pipeline_standard_present.py`` presence-not-behaviour
pattern.

Authoritative external: <https://code.claude.com/docs/en/extend>.
"""
from __future__ import annotations

from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_RULES_DIR = _REPO / ".claude" / "rules"

_RULES = (
    "heavy-lane",
    "engine-build",
    "data-adapter",
    "risk-path",
    "selfheal-auditheal",
    "migrations",
    "daemons",
    "engine-roster",
    "data-feed-roster",
    "llm-triage",
    "dashboard",
    "tests-and-ci",
)


@pytest.mark.parametrize("name", _RULES)
def test_rule_file_present_and_path_scoped(name: str) -> None:
    """Every named rule file exists, has a frontmatter ``paths:`` key,
    and carries a non-empty body after the frontmatter."""
    path = _RULES_DIR / f"{name}.md"
    assert path.is_file(), f"missing path-scoped rule: {path}"
    src = path.read_text()
    # YAML frontmatter present (between two ``---`` fences at top)
    assert src.startswith("---\n"), f"{path} missing YAML frontmatter open"
    parts = src.split("---\n", 2)
    assert len(parts) >= 3, f"{path} missing YAML frontmatter close"
    frontmatter, body = parts[1], parts[2]
    assert "paths:" in frontmatter, (
        f"{path} missing 'paths:' key — path-scoping is the mechanism's "
        "whole point")
    assert body.strip(), f"{path} has empty body"


def test_rule_set_is_exhaustive() -> None:
    """The on-disk rule set is exactly the pinned vocabulary — a stray
    or missing rule reds CI. Mirrors the SP-D
    ``test_vocabulary_is_exactly_pinned`` precedent."""
    on_disk = {p.stem for p in _RULES_DIR.glob("*.md")}
    expected = set(_RULES)
    assert on_disk == expected, (
        f"rule set drifted: extra={on_disk - expected} "
        f"missing={expected - on_disk}")
