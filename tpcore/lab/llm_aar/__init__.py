"""LLM-AAR critic package — post-trade pattern recognition for the autonomous loop.

Per spec ``docs/superpowers/specs/2026-05-22-llm-aar-critic-design.md``.

Engine-FREE: stdlib + pydantic v2 + structlog + tpcore.aar + tpcore.engine_profile.
Mirrors ``tpcore/lab/llm_finder/`` package shape.

Bright lines (§1.2 — non-negotiable, advisory-only contract):
- Never mutates engines, _PROFILE, LAB_TARGET, or credibility.
- Never opens PRs (finder is the sole emission surface).
- Never bypasses SP-A gate or autonomous Lab criteria.
- Findings are advisory text only; pattern observations, not engine redesigns.
"""
from __future__ import annotations

# ───────────────────────── Caps + quotas (spec §6 fence stack) ───────────

MAX_AAR_PAYLOAD_BYTES: int = 256 * 1024
"""Serialised AAR-payload byte cap; fail-loud on overflow (spec §2.3)."""

AAR_CRITIC_WINDOW_SESSIONS: int = 90
"""Rolling-window length for EnginePerformanceWindow construction (spec §2.2)."""

MAX_AAR_CRITIC_RUNS_PER_DAY: int = 2
"""Defense against runaway invocation (spec §5.4); counted on application_log."""

MAX_FINDINGS_PER_ENGINE_PER_RUN: int = 5
"""Cap per-engine emissions per run; defense against high-volume low-signal output."""

MIN_EVIDENCE_AAR_COUNT: int = 3
"""Minimum AARs supporting a pattern claim (spec §3.1)."""

FINDINGS_PER_ENGINE_LRU_CAP: int = 30
"""Per-engine memstore /findings/<engine>/ namespace cap (spec §4.2)."""

RECENT_RUNS_LRU_CAP: int = 20
"""/recent-runs/ namespace LRU cap (spec §4.2)."""

# ───────────────────────── Persona ───────────────────────────────────────

PERSONA_VERSION: str = "v1.0"
"""Initial AAR-critic persona; bump alongside PERSONA_SHA256 on any edit."""

PERSONA_SHA256: str = "bc403a1647248e0c421833d29d827a5566fa908b5ec4d83c5aceb9cfb9263388"
"""SHA256 of docs/llm_aar_persona.md at PERSONA_VERSION='v1.0'.

Persona edits MUST update both PERSONA_VERSION AND this constant.
The sentinel test test_persona_versioned.py reds the build on drift.
"""

# ───────────────────────── Memstore + Anthropic constants ───────────────

MEMSTORE_NAME: str = "aar-llm-critic-context"
"""Human-readable name for the dedicated AAR critic memstore."""

MEMSTORE_MOUNT_PATH: str = "/mnt/memory/aar-llm-critic"
"""Mount path inside the agent's session container (slug derived from name)."""

AGENT_NAME: str = "lab-aar-critic"
"""Anthropic Managed Agent name."""

ENVIRONMENT_NAME: str = "lab-aar-critic-env"
"""Anthropic environment name."""

ANTHROPIC_MODEL: str = "claude-sonnet-4-6"
"""Pinned model — same as finder for consistency."""

ANTHROPIC_MAX_TOKENS: int = 4096
"""Output budget per turn — smaller than finder (findings are small)."""


from tpcore.lab.llm_aar.models import (  # noqa: E402
    AARCriticRun,
    AARFinding,
    AARRowSummary,
    EnginePerformanceWindow,
)

__all__ = [
    "AARCriticRun",
    "AARFinding",
    "AARRowSummary",
    "AAR_CRITIC_WINDOW_SESSIONS",
    "AGENT_NAME",
    "ANTHROPIC_MAX_TOKENS",
    "ANTHROPIC_MODEL",
    "ENVIRONMENT_NAME",
    "EnginePerformanceWindow",
    "FINDINGS_PER_ENGINE_LRU_CAP",
    "MAX_AAR_CRITIC_RUNS_PER_DAY",
    "MAX_AAR_PAYLOAD_BYTES",
    "MAX_FINDINGS_PER_ENGINE_PER_RUN",
    "MEMSTORE_MOUNT_PATH",
    "MEMSTORE_NAME",
    "MIN_EVIDENCE_AAR_COUNT",
    "PERSONA_SHA256",
    "PERSONA_VERSION",
    "RECENT_RUNS_LRU_CAP",
]
