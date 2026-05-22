"""Persona text loader for the LLM-AAR critic.

The persona file lives at ``docs/llm_aar_persona.md`` (single source of
truth). The Anthropic Managed Agent's server-side ``system`` field is
populated FROM this file at provision time (``scripts/anthropic_aar_critic_provision.py``).

Mirrors ``tpcore/lab/llm_finder/persona.py`` discipline:
- Read FROM the file (no inlined copy).
- SHA-pinned via ``PERSONA_SHA256`` constant; CI sentinel reds drift.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

# Persona path is repo-relative; resolve from this file's location.
_PERSONA_PATH = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "docs"
    / "llm_aar_persona.md"
)


def persona_text() -> str:
    """Return the persona text the LLM-AAR critic uses as system prompt."""
    return _PERSONA_PATH.read_text(encoding="utf-8")


def persona_sha256() -> str:
    """Return SHA256 hex of the persona file.

    Used by:
    - The sentinel test ``test_persona_versioned.py`` to assert
      ``PERSONA_SHA256`` matches the file (drift catcher).
    - The provisioner to pin the agent's server-side persona SHA.
    """
    text = _PERSONA_PATH.read_text(encoding="utf-8")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def persona_path() -> Path:
    """Return the persona file path (for tests + provisioner)."""
    return _PERSONA_PATH


__all__ = ["persona_path", "persona_sha256", "persona_text"]
