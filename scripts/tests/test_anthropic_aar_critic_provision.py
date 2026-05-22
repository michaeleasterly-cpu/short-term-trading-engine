"""Tests for ``scripts/anthropic_aar_critic_provision.py`` — the one-time
provisioner that creates / looks up the Anthropic Agent + Environment +
Memstore backing the LLM-AAR critic's Sessions API path (spec
``docs/superpowers/specs/2026-05-22-llm-aar-critic-design.md`` §11 T5).

The script is operator-invoked (not a CI stage) but we hold it under the
orphan-script anti-rot test by importing it here; the suite asserts the
public surface + the safety guard that refuses to run without an API key.

Mirrors ``scripts/tests/test_anthropic_agent_provision.py``.
"""
from __future__ import annotations

import os
from unittest import mock

import pytest

# Module import must succeed without an API key set (constants live at
# import time; the API client is constructed inside `_provision`).
from scripts.anthropic_aar_critic_provision import (
    AGENT_NAME,
    ENVIRONMENT_NAME,
    FINDER_MEMSTORE_ID,
    IDS_MODULE_PATH,
    MANAGED_AGENTS_BETA,
    MEMSTORE_DESCRIPTION,
    MEMSTORE_NAME,
    MODEL,
    PERSONA_PATH,
    _read_persona,
    main,
)


def test_constants_present() -> None:
    """Public surface expected by callers + downstream tests."""
    assert AGENT_NAME == "lab-aar-critic"
    assert ENVIRONMENT_NAME == "lab-aar-critic-env"
    assert MEMSTORE_NAME == "aar-llm-critic-context"
    assert MODEL == "claude-sonnet-4-6"
    assert MANAGED_AGENTS_BETA == "managed-agents-2026-04-01"
    # Memstore description per spec §4.1
    assert "Post-trade pattern recognition" in MEMSTORE_DESCRIPTION
    # Finder memstore is the operator-seeded one (2026-05-22 handoff)
    assert FINDER_MEMSTORE_ID == "memstore_01MzLun3AfRf2viPmDqJvsWi"
    # Paths point at real files
    assert IDS_MODULE_PATH.name == "llm_aar_anthropic_ids.py"
    assert PERSONA_PATH.name == "llm_aar_persona.md"
    assert PERSONA_PATH.exists()


def test_read_persona_returns_text_and_sha() -> None:
    text, sha = _read_persona()
    assert "# LLM-AAR Critic Persona" in text
    # SHA-256 hex digest length.
    assert len(sha) == 64
    assert all(c in "0123456789abcdef" for c in sha)


def test_main_refuses_without_api_key() -> None:
    """Refuses to proceed if ANTHROPIC_API_KEY is unset (safety guard)."""
    with mock.patch.dict(os.environ, {}, clear=True), pytest.raises(SystemExit) as excinfo:
        main([])
    # SystemExit message carries the safety reason.
    assert "ANTHROPIC_API_KEY" in str(excinfo.value)
