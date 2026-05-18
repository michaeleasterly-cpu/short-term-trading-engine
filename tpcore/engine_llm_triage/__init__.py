"""Engine-lane LLM triage agent (Epic E / Engine Ladder R5).

Symmetric mirror of the shipped data-lane #187 — symmetry-of-approach,
NOT a clone (`feedback_symmetry_not_copy`). Advisory + human-gated
only: for a genuinely NOVEL engine-lane escalation it produces a
draft, human-merge-only PR (an additive, mechanism-free
DISPOSITION_POLICIES binding to an EXISTING EngineEscalationDisposition
verb + dossier). It never mutates the engine, never trades, never
disposes, never merges. Fenced by the SHARED deterministic
`tpcore.llm_data_triage.fence` (one fence object, injected engine
registries — FORK-A resolved). The persona governs output quality
only — NOT a safety boundary.

DARK in Phase 1: no daemon/agent imports this package yet.
"""

PERSONA_VERSION = "v1"

__all__ = ["PERSONA_VERSION"]
