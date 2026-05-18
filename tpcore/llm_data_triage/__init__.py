"""LLM data triage agent (#187, Ladder rung 5).

The ONLY LLM-backed agent in the platform. Advisory: for a genuinely
NOVEL data escalation it produces a draft, human-merge-only PR (an
additive, mechanism-free HealSpec binding + dossier). It never
mutates data, never trades, never merges; fenced by deterministic CI
checks (provenance + hard-denied paths) + a post-merge canary. The
persona governs output quality only — NOT a safety boundary.
"""

PERSONA_VERSION = "v1"

__all__ = ["PERSONA_VERSION"]
