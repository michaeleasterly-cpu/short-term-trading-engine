"""Durable enforcement: CLAUDE.md stays slim per the Anthropic memory guidance.

Per <https://code.claude.com/docs/en/memory>: keep the project memory
file short; load detail on demand via path-scoped rules, invocable
skills, named subagent profiles, and enforcement hooks (the `.claude/`
extension surface). The operator's explicit guidance is ≤ 200
non-blank, non-comment lines.

This sentinel reds the build if a future session re-bloats CLAUDE.md.
Mirrors the `tests/test_dev_pipeline_standard_present.py` /
`tests/test_claude_rules_present.py` / etc. anti-rot tripwire pattern.
"""
from __future__ import annotations

from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_CLAUDEMD = _REPO / "CLAUDE.md"

_MAX_LINES = 200


def _non_blank_non_comment_lines(src: str) -> list[str]:
    """Count substantive lines, mirroring how operators read the file:
    skip pure-blank lines and pure HTML/markdown comment lines."""
    out: list[str] = []
    for raw in src.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        # Markdown HTML-comment line (typical "<!-- ... -->" on its own line).
        if stripped.startswith("<!--") and stripped.endswith("-->"):
            continue
        out.append(raw)
    return out


def test_claudemd_is_slim() -> None:
    """CLAUDE.md must be ≤ 200 non-blank, non-comment lines.

    Detail belongs in `.claude/rules/` (path-scoped invariants),
    `.claude/skills/` (invocable workflows), `.claude/agents/`
    (subagent profiles), `.claude/hooks/` (enforcement guarantees),
    `docs/DEV_PIPELINE_STANDARD.md` (the lanes), and the dedicated
    canonical docs/specs. CLAUDE.md is the slim landing page that
    points at them."""
    assert _CLAUDEMD.is_file(), f"missing CLAUDE.md at {_CLAUDEMD}"
    lines = _non_blank_non_comment_lines(_CLAUDEMD.read_text())
    count = len(lines)
    assert count <= _MAX_LINES, (
        f"CLAUDE.md has {count} non-blank, non-comment lines — "
        f"over the ≤{_MAX_LINES} limit. Detail belongs in .claude/rules/, "
        ".claude/skills/, .claude/agents/, .claude/hooks/, or "
        "docs/DEV_PIPELINE_STANDARD.md — per "
        "https://code.claude.com/docs/en/memory keep the project "
        "memory file short.")


def test_claudemd_points_at_the_extension_surface() -> None:
    """CLAUDE.md must reference each layer of the `.claude/` extension
    surface — a slim CLAUDE.md is only useful if a future session can
    find the loaded-on-demand detail. A regression that silently drops
    the pointer to any layer reds CI."""
    assert _CLAUDEMD.is_file()
    src = _CLAUDEMD.read_text()
    for anchor in (
        ".claude/rules/",
        ".claude/skills/",
        ".claude/agents/",
        ".claude/hooks/",
        "docs/DEV_PIPELINE_STANDARD.md",
    ):
        assert anchor in src, (
            f"CLAUDE.md lost the pointer to {anchor} — a slim "
            "CLAUDE.md must point at every extension-surface layer.")
