"""Persona doc version pin + hard-guardrail phrase checks."""
from __future__ import annotations

from pathlib import Path

import tpcore.llm_data_triage as pkg

_PERSONA = Path(__file__).parents[2] / "docs" / "llm_data_triage_persona.md"


def test_version_line_matches_package() -> None:
    text = _PERSONA.read_text()
    first_line = text.splitlines()[0]
    assert first_line.startswith("version: ")
    doc_version = first_line.split("version: ", 1)[1].strip()
    assert doc_version == pkg.PERSONA_VERSION


def test_hard_guardrail_phrases_present() -> None:
    text = _PERSONA.read_text()
    assert "never propose a new" in text
    assert "NOT a safety boundary" in text
