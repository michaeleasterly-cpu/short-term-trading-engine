"""Persona file SHA sentinel — Task #25 §7.

The LLM's system prompt is ``docs/personas/lab_finder_persona.md``. Edits MUST
bump ``PERSONA_VERSION`` in ``tpcore.lab.llm_finder.__init__``. The
SHA-pin sentinel (``test_persona_versioned.py``) reds the build if
the file's SHA256 doesn't match the recorded constant for the current
PERSONA_VERSION — same mechanism as SP-G's ``_persona_sha`` per spec
§7.1.

The recorded SHA is updated alongside any persona edit + PERSONA_VERSION
bump (operator-staged; the LLM cannot edit either via diff-scope fence).
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Final

from tpcore.lab.llm_finder import PERSONA_VERSION

_PERSONA_PATH: Final[Path] = (
    Path(__file__).resolve().parents[3]
    / "docs" / "personas" / "lab_finder_persona.md"
)


def persona_sha256() -> str:
    """Compute SHA256 of the current persona file content (utf-8)."""
    if not _PERSONA_PATH.is_file():
        raise FileNotFoundError(f"persona file not found at {_PERSONA_PATH}")
    return hashlib.sha256(_PERSONA_PATH.read_bytes()).hexdigest()


def persona_text() -> str:
    """Read the persona content (utf-8). For embedding into the system prompt."""
    if not _PERSONA_PATH.is_file():
        raise FileNotFoundError(f"persona file not found at {_PERSONA_PATH}")
    return _PERSONA_PATH.read_text(encoding="utf-8")


__all__ = ["PERSONA_VERSION", "persona_sha256", "persona_text"]
