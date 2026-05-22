"""Persona SHA + version sentinel tests — Task #25 §7 + §10.2.

Persona edits MUST bump PERSONA_VERSION + update PERSONA_SHA256.
Editing the persona without either reds the build.
"""
from __future__ import annotations

import re

from tpcore.lab.llm_finder import PERSONA_SHA256, PERSONA_VERSION
from tpcore.lab.llm_finder.persona import (
    _PERSONA_PATH,
    persona_sha256,
    persona_text,
)


def test_persona_file_exists() -> None:
    """Persona file must live at the expected path."""
    assert _PERSONA_PATH.is_file(), f"Persona missing at {_PERSONA_PATH}"


def test_persona_sha_matches_constant() -> None:
    """Drift sentinel: edits to docs/personas/lab_finder_persona.md MUST update PERSONA_SHA256.

    If this fails, you edited the persona without bumping the constant
    in tpcore/lab/llm_finder/__init__.py. Either:
    1. Bump PERSONA_VERSION (e.g., v2.0 → v2.1) and update PERSONA_SHA256
       to the new sha (printed below).
    2. Revert the persona edit.

    Never just update PERSONA_SHA256 without bumping PERSONA_VERSION
    (that's "silently changing the LLM's behaviour" and reds the
    follow-up audit).
    """
    actual = persona_sha256()
    assert actual == PERSONA_SHA256, (
        f"Persona SHA drift detected.\n"
        f"  Recorded: {PERSONA_SHA256}\n"
        f"  Actual:   {actual}\n"
        f"  At version: {PERSONA_VERSION}\n"
        f"  Bump PERSONA_VERSION + update PERSONA_SHA256 in "
        f"tpcore/lab/llm_finder/__init__.py."
    )


def test_persona_version_format() -> None:
    """PERSONA_VERSION follows vN.M (e.g., v2.0)."""
    assert re.match(r"^v\d+\.\d+$", PERSONA_VERSION), (
        f"PERSONA_VERSION='{PERSONA_VERSION}' does not match vN.M"
    )


def test_persona_text_carries_six_mandatory_sections() -> None:
    """Spec §7.2: persona MUST carry the 6 mandatory sections."""
    text = persona_text()
    # Section headers are markdown ## with §-numbering per the doc.
    expected_section_markers = [
        ("§1", "Identity"),
        ("§2", "environment"),
        ("§3", "Regime"),
        ("§4", "Reference bundles"),
        ("§5", "n_trials discipline"),
        ("§6", "Outcome"),
    ]
    for section_id, keyword in expected_section_markers:
        assert section_id in text, f"missing section marker {section_id}"
        # Confirm the keyword appears in the section's heading or first paragraph.
        # Simple check: text contains both the marker AND the keyword somewhere.
        assert keyword.lower() in text.lower(), (
            f"section {section_id} missing keyword '{keyword}'"
        )


def test_persona_carries_outcome_binding() -> None:
    """The Path B 'I know it when I see it' outcome contract must be load-bearing."""
    text = persona_text()
    assert "I know it when I see it" in text
    assert "operator-discretion" in text.lower() or "operator-binding" in text.lower()


def test_persona_carries_cost_honesty_directive() -> None:
    """cost_net_simulation + cost_assumption_bps_roundtrip language present."""
    text = persona_text()
    assert "cost_net_simulation" in text
    assert "cost_assumption_bps_roundtrip" in text
    assert "cost_net_sharpe" in text


def test_persona_carries_regime_first_directive() -> None:
    """The regime-aware §3 directive must explicitly require reading regime FIRST."""
    text = persona_text()
    assert "market_regime" in text
    assert re.search(r"\bregime\s+FIRST\b", text), (
        "Persona §3 must explicitly direct: 'Read market_regime FIRST'"
    )


def test_persona_no_engine_add_invitation() -> None:
    """Persona must NOT invite ENGINE-ADD in v1 (v1.5 scope; spec §4.5)."""
    text = persona_text()
    # Explicit v1.5 marker present; no positive directive to use engine_add_path.
    assert "v1.5" in text
    # Defense against hand-wave promotion to ENGINE-ADD.
    assert not re.search(r"\bcreate\s+a\s+new\s+engine\b", text, re.IGNORECASE)
