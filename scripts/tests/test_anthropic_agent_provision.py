"""Tests for ``scripts/anthropic_agent_provision.py`` — the one-time
provisioner that creates / looks up the Anthropic Agent + Environment
backing the LLM edge-finder's Sessions API path (Task #25 + 2026-05-22
memory-store wiring).

The script is operator-invoked (not a CI stage) but we hold it under the
orphan-script anti-rot test by importing it here; the suite asserts the
public surface + the safety guard that refuses to run without an API key.
"""
from __future__ import annotations

import os
from unittest import mock

import pytest

# Module import must succeed without an API key set (constants live at
# import time; the API client is constructed inside `_provision`).
# We import the inner names directly so the orphan-script detector's
# ``from scripts.anthropic_agent_provision import ...`` pattern fires.
from scripts.anthropic_agent_provision import (
    AGENT_NAME,
    ENVIRONMENT_NAME,
    IDS_MODULE_PATH,
    MANAGED_AGENTS_BETA,
    MODEL,
    PERSONA_PATH,
    _read_persona,
    main,
)


def test_constants_present() -> None:
    """Public surface expected by callers + downstream tests."""
    assert AGENT_NAME == "lab-edge-finder"
    assert ENVIRONMENT_NAME == "lab-edge-finder-env"
    assert MODEL == "claude-sonnet-4-6"
    assert MANAGED_AGENTS_BETA == "managed-agents-2026-04-01"
    # Paths point at real files (defensive — catches accidental moves).
    assert IDS_MODULE_PATH.name == "llm_finder_anthropic_ids.py"
    assert PERSONA_PATH.name == "lab_finder_persona.md"
    assert PERSONA_PATH.exists()


def test_read_persona_returns_text_and_sha() -> None:
    text, sha = _read_persona()
    assert "# LLM Edge-Finder Persona" in text
    # SHA-256 hex digest length.
    assert len(sha) == 64
    assert all(c in "0123456789abcdef" for c in sha)


def test_main_refuses_without_api_key() -> None:
    """Refuses to proceed if ANTHROPIC_API_KEY is unset (safety guard)."""
    with mock.patch.dict(os.environ, {}, clear=True), pytest.raises(SystemExit) as excinfo:
        main([])
    # SystemExit message carries the safety reason.
    assert "ANTHROPIC_API_KEY" in str(excinfo.value)
