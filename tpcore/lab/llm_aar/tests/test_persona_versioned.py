"""Sentinel test — PERSONA_SHA256 constant must match docs/llm_aar_persona.md.

If this reds, the persona file was edited without bumping PERSONA_VERSION
+ PERSONA_SHA256 in tpcore/lab/llm_aar/__init__.py.

Mirrors tpcore/lab/llm_finder/tests/test_persona_versioned.py.
"""
from __future__ import annotations

from tpcore.lab.llm_aar import PERSONA_SHA256, PERSONA_VERSION
from tpcore.lab.llm_aar.persona import persona_sha256, persona_text


def test_persona_sha_matches_constant() -> None:
    """Drift catcher: persona file SHA must equal the pinned constant."""
    actual = persona_sha256()
    assert actual == PERSONA_SHA256, (
        f"Persona SHA drift detected. docs/llm_aar_persona.md SHA = "
        f"{actual} but tpcore.lab.llm_aar.PERSONA_SHA256 = {PERSONA_SHA256}. "
        f"Either: (a) revert the persona edit, OR (b) bump PERSONA_VERSION "
        f"+ update PERSONA_SHA256 to match the file."
    )


def test_persona_text_loads() -> None:
    text = persona_text()
    assert text.startswith("# LLM-AAR Critic Persona")
    assert PERSONA_VERSION in text


def test_persona_text_has_six_mandatory_sections() -> None:
    """Spec §8.2 requires six mandatory sections (numbered §1 - §9 in v1.0)."""
    text = persona_text()
    # Headings present (per persona v1.0 — 9 sections total).
    assert "## §1 Identity" in text
    assert "## §2 AAR substrate framing" in text
    assert "## §3 Theme vocabulary" in text
    assert "## §4 Evidence discipline" in text
    assert "## §5 Suggested emission axis discipline" in text
    assert "## §6 What you do NOT do" in text


def test_persona_documents_bright_lines() -> None:
    """Persona MUST forbid prescriptive engine-redesign output (spec §1.2)."""
    text = persona_text()
    assert "advisory-only" in text.lower()
    assert "never" in text.lower()


def test_persona_documents_canary_exclusion() -> None:
    """Persona MUST tell the LLM canary is excluded (spec §2.1)."""
    text = persona_text()
    assert "canary" in text.lower()
