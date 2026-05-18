"""Engine persona doc version pin + hard-guardrail phrase checks
(mirrors tpcore/tests/test_llm_data_triage_persona.py)."""
from __future__ import annotations

from pathlib import Path

import tpcore.engine_llm_triage as pkg

_PERSONA = (Path(__file__).parents[2] / "docs"
            / "engine_llm_triage_persona.md")


def test_version_line_matches_package() -> None:
    text = _PERSONA.read_text()
    first_line = text.splitlines()[0]
    assert first_line.startswith("version: ")
    doc_version = first_line.split("version: ", 1)[1].strip()
    assert doc_version == pkg.PERSONA_VERSION


def test_engine_output_contract_present() -> None:
    text = _PERSONA.read_text()
    # additive DISPOSITION_POLICIES binding to an EXISTING verb
    assert "DISPOSITION_POLICIES" in text
    assert "converted" in text and "structural" in text and "removed" in text
    assert "dossier" in text.lower()
    assert "confidence" in text.lower()
    assert "could not determine" in text.lower()


def test_hard_guardrail_phrases_present() -> None:
    text = _PERSONA.read_text()
    assert "never propose a new" in text
    assert "NOT a safety boundary" in text
    # engine-native guardrails: defer to the R3 human; no authority;
    # never invent internals; never a new mechanism/disposition member
    assert "R3" in text
    assert "no authority" in text.lower() or "NO authority" in text
    assert "EngineEscalationDisposition" in text
