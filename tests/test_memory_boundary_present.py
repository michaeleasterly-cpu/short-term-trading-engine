"""C0.1 (2026-06-01) — memory boundary policy presence sentinel.

Pins that ``docs/MEMSTORE_HANDOFF.md`` carries the four-tier memory
boundary model and that ``CLAUDE.md`` points at it. The behavior
under the policy is enforced by tests/hooks/CI elsewhere; this
sentinel only guarantees the policy doc cannot be silently deleted
or gutted.

The sentinel uses substring presence (not section-anchored parsing)
on purpose — phrasing can evolve, but the load-bearing concepts
must remain mentioned. If a future revision removes one of these
mentions, the test reds CI and the operator has a chance to
re-confirm the boundary model survives the rewrite.
"""
from __future__ import annotations

from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_HANDOFF = _REPO / "docs" / "MEMSTORE_HANDOFF.md"
_CLAUDE_MD = _REPO / "CLAUDE.md"


def _handoff_text() -> str:
    assert _HANDOFF.is_file(), (
        f"missing {_HANDOFF.relative_to(_REPO)} — the canonical "
        "memory-boundary doc per C0.1"
    )
    text = _HANDOFF.read_text(encoding="utf-8")
    assert text.strip(), f"{_HANDOFF.relative_to(_REPO)} is empty"
    return text


def _claude_md_text() -> str:
    assert _CLAUDE_MD.is_file(), f"missing {_CLAUDE_MD.relative_to(_REPO)}"
    text = _CLAUDE_MD.read_text(encoding="utf-8")
    assert text.strip(), f"{_CLAUDE_MD.relative_to(_REPO)} is empty"
    return text


def test_memory_handoff_present() -> None:
    """docs/MEMSTORE_HANDOFF.md exists and is non-empty."""
    _handoff_text()


def test_handoff_names_claude_md() -> None:
    """The handoff doc must name CLAUDE.md as tier 1."""
    text = _handoff_text()
    assert "CLAUDE.md" in text, (
        "MEMSTORE_HANDOFF.md must name CLAUDE.md as tier 1 of the "
        "memory boundary"
    )


def test_handoff_names_memory_md() -> None:
    """The handoff doc must name MEMORY.md (the Claude Code local
    memory index) as tier 2."""
    text = _handoff_text()
    assert "MEMORY.md" in text, (
        "MEMSTORE_HANDOFF.md must name MEMORY.md as the Claude Code "
        "local memory tier (tier 2)"
    )


def test_handoff_distinguishes_anthropic_memstore() -> None:
    """The handoff doc must distinguish the Anthropic API beta
    memstores from the Claude Code local memory (tier 3 vs tier 2).
    A repo that conflates them inevitably writes secrets / raw logs
    into the wrong tier."""
    text = _handoff_text()
    for marker in ("Anthropic", "memstore"):
        assert marker in text, (
            f"MEMSTORE_HANDOFF.md must mention {marker!r} so the "
            "Anthropic API tier is explicitly distinguished from the "
            "Claude Code local memory tier"
        )


def test_handoff_names_enforcement_floor() -> None:
    """The handoff doc must name tests / hooks / CI as the
    enforcement floor — memory is context, not enforcement."""
    text = _handoff_text()
    lowered = text.lower()
    for marker in ("tests", "hooks", "ci"):
        assert marker in lowered, (
            f"MEMSTORE_HANDOFF.md must mention {marker!r} so the "
            "'enforcement belongs in tests/hooks/CI, not memory' "
            "principle is explicit"
        )


def test_handoff_states_source_of_truth_hierarchy() -> None:
    """The handoff doc must state that repo code/tests/docs override
    every memory tier — the dispositive-source-of-truth rule."""
    text = _handoff_text()
    lowered = text.lower()
    has_source_of_truth = "source of truth" in lowered
    has_override = "override" in lowered or "dispositive" in lowered
    assert has_source_of_truth and has_override, (
        "MEMSTORE_HANDOFF.md must state that code / tests / docs are "
        "the source of truth and override memory — 'source of truth' "
        "+ 'override' or 'dispositive' must both appear"
    )


def test_handoff_forbids_secrets_and_raw_dumps() -> None:
    """The handoff doc must explicitly forbid secrets, private
    account/financial data, raw logs, and raw backtest dumps in
    memory of any tier."""
    text = _handoff_text()
    lowered = text.lower()
    for marker in (
        "secret",
        "credential",
        "raw log",
        "raw backtest",
    ):
        assert marker in lowered, (
            f"MEMSTORE_HANDOFF.md must explicitly forbid {marker!r} "
            "across memory tiers"
        )


def test_claude_md_points_to_handoff() -> None:
    """CLAUDE.md must carry a short pointer to
    docs/MEMSTORE_HANDOFF.md so the session-start surface routes
    operators to the canonical boundary doc on first read."""
    text = _claude_md_text()
    assert "docs/MEMSTORE_HANDOFF.md" in text, (
        "CLAUDE.md must include a pointer to docs/MEMSTORE_HANDOFF.md "
        "(the canonical memory-boundary doc)"
    )
