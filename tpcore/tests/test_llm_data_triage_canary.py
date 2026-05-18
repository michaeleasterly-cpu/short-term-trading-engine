"""Tests for the canary predicate — pure, no DB."""
from __future__ import annotations

from tpcore.llm_data_triage.canary import is_promoted, shadow_decision


def test_not_promoted_returns_shadow() -> None:
    assert shadow_decision("spec-abc", []) == "shadow"


def test_matching_promotion_event_returns_active() -> None:
    events = [
        {"event_type": "LLM_SPEC_PROMOTED", "data": {"spec_key": "spec-abc"}},
    ]
    assert shadow_decision("spec-abc", events) == "active"
    assert is_promoted(events, "spec-abc") is True


def test_promotion_for_different_key_stays_shadow() -> None:
    events = [
        {"event_type": "LLM_SPEC_PROMOTED", "data": {"spec_key": "spec-xyz"}},
    ]
    assert shadow_decision("spec-abc", events) == "shadow"
    assert is_promoted(events, "spec-abc") is False
